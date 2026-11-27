"""
Sign-up, sign-in and password recovery.

* Sign-up: the account is created inactive. It becomes active when the six-digit code emailed to
  the address is entered. One email address can belong to only one account.
* Sign-in: one page for everyone, accepting an email address or a username. Administrators are sent
  to the Control Panel, and clients to their own dashboard.
* Forgot password: a six-digit code is emailed only to an account that exists. The visitor sees the
  same response either way. After the code is accepted, the visitor chooses a new password.
* Administrators can still email a reset link from the Control Panel. That uses Django's
  link-based views, configured in urls.py.

Protections:
* Repeated failed sign-ins for the same username from the same address are paused for 15 minutes.
* Code requests are limited per device. Each code has a resend interval, a lifetime and an attempt limit.
"""

import re
import secrets
from math import ceil

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import notifications, otp
from .forms import ForgotPasswordForm, LoginForm, NewPasswordForm, RegisterForm
from .models import UserProfile
from .roles import is_admin_user

User = get_user_model()

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
LOCKED_MESSAGE = 'Too many failed sign-in attempts. Please wait 15 minutes, or use Forgot password.'
SEND_LIMIT = 8  # code emails per device within LOCKOUT_SECONDS
SEND_LIMIT_MESSAGE = 'Too many code requests from this device. Please wait 15 minutes and try again.'
# The visitor is told plainly that nothing was sent. The technical reason goes to the server log only.
DELIVERY_FAILED_MESSAGE = ('The email could not be sent, so no code is on its way. Please try again in a few '
                           'minutes. If it keeps failing, contact us.')
RESET_SENT_MESSAGE = ('If an account is registered with that email address, a six-digit code has been sent to it. '
                      'Check your inbox and spam folder.')
RESET_CODE_ERROR = ('That code is not valid. It may be wrong, expired or already used. '
                    'Enter the newest code, or request a new one.')
TOO_MANY_CODES_MESSAGE = ('Too many codes have been sent to this account recently. '
                          'Please wait a while, then try again.')
PASSWORD_CHANGED_MESSAGE = 'Your password has been changed. Log in with your new password.'

# Session keys for the two multi-step flows.
SIGNUP_USER = 'signup_user_id'
SIGNUP_NEXT = 'signup_next'
RESET_EMAIL = 'reset_email'
RESET_REQUESTED_AT = 'reset_requested_at'
RESET_USER = 'reset_user_id'
RESET_VERIFIED_AT = 'reset_verified_at'
RESET_KEYS = (RESET_EMAIL, RESET_REQUESTED_AT, RESET_USER, RESET_VERIFIED_AT)


# ---------- helpers ----------

def _explicit_next(request):
    """The page the visitor was heading for, if it is safe to return to. Otherwise ''."""
    target = request.POST.get('next') or request.GET.get('next') or ''
    if (target and not target.startswith('/manage/')
            and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                require_https=request.is_secure())):
        return target
    return ''


def _safe_next(request, default='dashboard'):
    """Where to send a user after signing in: back to the page they came from, if it is safe."""
    return _explicit_next(request) or reverse(default)


def _send_allowed(request):
    """Count one code email against this device. Returns False once the device has used up its allowance."""
    key = f'otp-send:{request.META.get("REMOTE_ADDR", "")}'
    sent = cache.get(key, 0)
    if sent >= SEND_LIMIT:
        return False
    cache.set(key, sent + 1, LOCKOUT_SECONDS)
    return True


def _entered_code(request):
    """The six digits from the code boxes, or None if any box is empty or not a digit."""
    digits = ''.join(request.POST.get(f'digit_{i}', '').strip() for i in range(otp.CODE_DIGITS))
    return digits if re.fullmatch(r'\d{6}', digits) else None


def _code_page_context(**extra):
    return {
        'digit_boxes': list(range(otp.CODE_DIGITS)),
        'lifetime_minutes': settings.OTP_LIFETIME_SECONDS // 60,
        **extra,
    }


def _active_account(email):
    return User.objects.filter(email__iexact=email, is_active=True).order_by('pk').first()


def _new_username(email):
    """An internal username. People sign in with their email address, so this value is never shown."""
    base = re.sub(r'[^a-z0-9]', '', email.split('@', 1)[0].lower())[:20] or 'client'
    while True:
        candidate = f'{base}{secrets.token_hex(3)}'
        if not User.objects.filter(username=candidate).exists():
            return candidate


def _create_unverified_user(data):
    first, _, last = data['full_name'].partition(' ')
    with transaction.atomic():
        user = User(username=_new_username(data['email']), email=data['email'],
                    first_name=first[:150], last_name=last[:150], is_active=False)
        user.set_password(data['password1'])
        user.save()
        UserProfile.objects.filter(user=user).update(email_verified=False)
    notifications.log_activity(user, 'account.registered', f'New client account: {user.username}')
    return user


def _pending_signup(request):
    """The account waiting for its sign-up code, or None if this browser has none."""
    pk = request.session.get(SIGNUP_USER)
    if pk:
        user = User.objects.filter(pk=pk, is_active=False, profile__email_verified=False).first()
        if user is not None:
            return user
    request.session.pop(SIGNUP_USER, None)
    return None


def _deliver_signup_code(request, user):
    try:
        otp.issue_code(user, otp.SIGNUP)
    except otp.ResendTooSoon as wait:
        messages.info(request, f'A code was sent a moment ago. You can request another in {wait.seconds} seconds.')
    except otp.TooManyCodes:
        messages.error(request, TOO_MANY_CODES_MESSAGE)
    except otp.DeliveryFailed:
        messages.error(request, DELIVERY_FAILED_MESSAGE)
    else:
        messages.success(request, f'Verification code sent to {otp.mask_email(user.email)}. '
                                  f'It expires in {settings.OTP_LIFETIME_SECONDS // 60} minutes.')


def _signup_error(result):
    if result.status == otp.WRONG:
        left = result.attempts_left
        return f'That code is not correct. {left} {"attempt" if left == 1 else "attempts"} left.'
    if result.status == otp.LOCKED:
        return 'Too many incorrect codes, so this code is locked. Request a new code below.'
    return 'That code has expired or has already been used. Request a new code below.'


def _activate_and_sign_in(request, user):
    # A visitor who came from a booking or another page returns there. A new account otherwise goes to
    # its profile, so the person can add a photo and details straight away.
    next_url = request.session.pop(SIGNUP_NEXT, None)
    request.session.pop(SIGNUP_USER, None)
    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=['is_active'])
        UserProfile.objects.filter(user=user).update(email_verified=True)
    login(request, user)
    notifications.log_activity(user, 'account.verified', f'Email verified: {user.username}')
    if next_url:
        messages.success(request, f'Welcome, {user.first_name or user.username}! Your account is verified and ready.')
        return redirect(next_url)
    messages.success(request, f'Welcome, {user.first_name or user.username}! Your account is verified. '
                              'Add a photo and a few details to complete your profile.')
    return redirect(reverse('profile'))


def _session_timers(request):
    """Seconds left on the reset code and before a new one may be requested.

    These come from the session rather than the database, so the page looks the same whether or not
    the address belongs to an account.
    """
    requested = request.session.get(RESET_REQUESTED_AT)
    if not requested:
        return 0, 0
    elapsed = timezone.now().timestamp() - requested
    return (max(0, ceil(settings.OTP_LIFETIME_SECONDS - elapsed)),
            max(0, ceil(settings.OTP_RESEND_SECONDS - elapsed)))


def _send_reset_code(request, email):
    """Email a reset code if the address belongs to an active account.

    The visitor sees the same success message either way. Only a delivery failure differs, and that
    failure is a problem with the mail service, not with the address.
    """
    request.session[RESET_REQUESTED_AT] = timezone.now().timestamp()
    user = _active_account(email)
    if user is None:
        messages.success(request, RESET_SENT_MESSAGE)
        return
    try:
        otp.issue_code(user, otp.RESET)
    except (otp.ResendTooSoon, otp.TooManyCodes):
        # The same reply as a sent code. Reporting the limit would reveal that this address has an account.
        messages.success(request, RESET_SENT_MESSAGE)
    except otp.DeliveryFailed:
        messages.error(request, DELIVERY_FAILED_MESSAGE)
    else:
        messages.success(request, RESET_SENT_MESSAGE)


def _reset_user(request):
    """The account whose reset code was accepted recently, or None."""
    pk = request.session.get(RESET_USER)
    verified_at = request.session.get(RESET_VERIFIED_AT)
    if not pk or not verified_at:
        return None
    if timezone.now().timestamp() - verified_at > settings.PASSWORD_RESET_CHOOSE_SECONDS:
        return None
    return User.objects.filter(pk=pk, is_active=True).first()


def _unverified_sign_in(identifier, password):
    """An unverified sign-up whose correct password was just entered, or None.

    Such a visitor is taken to the code page rather than told the sign-in failed.
    """
    identifier = (identifier or '').strip()
    if not identifier or not password:
        return None
    user = User.objects.filter(Q(username=identifier) | Q(email__iexact=identifier)).order_by('pk').first()
    if user is None or user.is_active:
        return None
    if not UserProfile.objects.filter(user=user, email_verified=False).exists():
        return None
    return user if user.check_password(password) else None


# ---------- sign-in ----------

class ThrottledLoginMixin:
    def post(self, request, *args, **kwargs):
        if cache.get(_throttle_key(request, request.POST.get('username')), 0) >= MAX_FAILED_ATTEMPTS:
            form = self.get_form()
            form.add_error(None, LOCKED_MESSAGE)
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        cache.delete(_throttle_key(self.request, self.request.POST.get('username')))
        return super().form_valid(form)

    def form_invalid(self, form):
        key = _throttle_key(self.request, self.request.POST.get('username'))
        cache.set(key, cache.get(key, 0) + 1, LOCKOUT_SECONDS)
        return super().form_invalid(form)


def _throttle_key(request, username):
    return f'login-fail:{request.META.get("REMOTE_ADDR", "")}:{(username or "").strip().lower()}'


class RoleLoginView(ThrottledLoginMixin, LoginView):
    """The single sign-in page. Sends each user to their own dashboard."""

    template_name = 'accounts/login.html'
    authentication_form = LoginForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Django's LoginView adds site_name = the domain, which would hide the site name set in the dashboard.
        context.pop('site_name', None)
        return context

    def form_invalid(self, form):
        unverified = _unverified_sign_in(self.request.POST.get('username'), self.request.POST.get('password'))
        if unverified is not None:
            return _resume_verification(self.request, unverified)
        return super().form_invalid(form)

    def get_success_url(self):
        if is_admin_user(self.request.user):
            return reverse('admin_dashboard')
        next_url = self.get_redirect_url()
        # Clients may return to the page they came from, but never into the administrator area.
        if (next_url and not next_url.startswith('/manage/')
                and url_has_allowed_host_and_scheme(next_url, allowed_hosts={self.request.get_host()},
                                                    require_https=self.request.is_secure())):
            return next_url
        return reverse('dashboard')


def _resume_verification(request, user):
    request.session[SIGNUP_USER] = user.pk
    request.session[SIGNUP_NEXT] = _explicit_next(request)
    messages.info(request, 'Your account is not verified yet. Enter the code we emailed you to activate it.')
    if _send_allowed(request):
        _deliver_signup_code(request, user)
    else:
        messages.error(request, SEND_LIMIT_MESSAGE)
    return redirect('verify_email')


# ---------- sign-up ----------

def register(request):
    if request.user.is_authenticated:
        return redirect(_safe_next(request))
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            if _send_allowed(request):
                return _start_signup(request, form)
            form.add_error(None, SEND_LIMIT_MESSAGE)
    else:
        form = RegisterForm()
    return render(request, 'accounts/register.html', {'form': form})


def _start_signup(request, form):
    if form.pending_user is not None:
        # The email already has an unfinished sign-up. Verify that account, and keep its details.
        user = form.pending_user
        messages.info(request, 'This email already has an unfinished sign-up, so we will verify that account.')
    else:
        user = _create_unverified_user(form.cleaned_data)
    request.session[SIGNUP_USER] = user.pk
    request.session[SIGNUP_NEXT] = _explicit_next(request)
    _deliver_signup_code(request, user)
    return redirect('verify_email')


def verify_email(request):
    user = _pending_signup(request)
    if user is None:
        messages.info(request, 'Create your account first, and we will email you a code.')
        return redirect('register')

    if request.method == 'POST':
        code = _entered_code(request)
        if code is None:
            messages.error(request, 'Enter all six digits of the code.')
            return redirect('verify_email')
        result = otp.check_code(user, otp.SIGNUP, code)
        if result.status == otp.OK:
            return _activate_and_sign_in(request, user)
        messages.error(request, _signup_error(result))
        return redirect('verify_email')

    expires_in, resend_in = otp.timers(user, otp.SIGNUP)
    return render(request, 'accounts/verify_email.html', _code_page_context(
        masked_email=otp.mask_email(user.email),
        expires_in=expires_in,
        resend_in=resend_in,
        action_url=reverse('verify_email'),
        resend_url=reverse('verify_email_resend'),
        change_url=reverse('register'),
        change_text='Use a different email address',
    ))


@require_POST
def resend_verification(request):
    user = _pending_signup(request)
    if user is None:
        return redirect('register')
    if _send_allowed(request):
        _deliver_signup_code(request, user)
    else:
        messages.error(request, SEND_LIMIT_MESSAGE)
    return redirect('verify_email')


# ---------- forgot password ----------

def forgot_password(request):
    if request.method == 'POST':
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            for key in RESET_KEYS:
                request.session.pop(key, None)
            request.session[RESET_EMAIL] = email
            if _send_allowed(request):
                _send_reset_code(request, email)
            else:
                messages.error(request, SEND_LIMIT_MESSAGE)
            return redirect('password_reset_verify')
    else:
        form = ForgotPasswordForm()
    return render(request, 'accounts/password_reset_form.html', {'form': form})


def reset_verify(request):
    email = request.session.get(RESET_EMAIL)
    if not email:
        messages.info(request, 'Enter your email address and we will send you a code.')
        return redirect('password_reset')

    if request.method == 'POST':
        code = _entered_code(request)
        if code is None:
            messages.error(request, 'Enter all six digits of the code.')
            return redirect('password_reset_verify')
        user = _active_account(email)
        result = otp.check_code(user, otp.RESET, code) if user else otp.CodeResult(otp.EXPIRED)
        if result.status == otp.OK:
            for key in RESET_KEYS:
                request.session.pop(key, None)
            request.session[RESET_USER] = user.pk
            request.session[RESET_VERIFIED_AT] = timezone.now().timestamp()
            return redirect('password_reset_new')
        messages.error(request, RESET_CODE_ERROR)
        return redirect('password_reset_verify')

    expires_in, resend_in = _session_timers(request)
    return render(request, 'accounts/password_reset_verify.html', _code_page_context(
        masked_email=otp.mask_email(email),
        expires_in=expires_in,
        resend_in=resend_in,
        action_url=reverse('password_reset_verify'),
        resend_url=reverse('password_reset_resend'),
        change_url=reverse('password_reset'),
        change_text='Use a different email address',
    ))


@require_POST
def reset_resend(request):
    email = request.session.get(RESET_EMAIL)
    if not email:
        return redirect('password_reset')
    _, resend_in = _session_timers(request)
    if not _send_allowed(request):
        messages.error(request, SEND_LIMIT_MESSAGE)
    elif resend_in:
        messages.info(request, f'Please wait {resend_in} seconds before requesting another code.')
    else:
        _send_reset_code(request, email)
    return redirect('password_reset_verify')


def reset_new_password(request):
    user = _reset_user(request)
    if user is None:
        messages.info(request, 'Your reset session has ended. Please request a new code.')
        return redirect('password_reset')

    if request.method == 'POST':
        form = NewPasswordForm(user, request.POST)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                otp.cancel_codes(user, otp.RESET)
            for key in RESET_KEYS:
                request.session.pop(key, None)
            notifications.log_activity(user, 'account.password_reset',
                                       f'Password reset with an emailed code: {user.username}')
            messages.success(request, PASSWORD_CHANGED_MESSAGE)
            return redirect('login')
    else:
        form = NewPasswordForm(user)
    return render(request, 'accounts/password_reset_new.html', {'form': form})
