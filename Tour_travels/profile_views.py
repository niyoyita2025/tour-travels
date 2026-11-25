"""
The signed-in person's own profile: name, photo, phone, country, bio, and the email address.

Every view works on request.user only. There is no profile id in any URL, so one person cannot open or
change another person's profile here. Changing the email address needs a code sent to the new address.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import notifications, otp
from .accounts_views import SEND_LIMIT_MESSAGE, _code_page_context, _entered_code, _send_allowed
from .forms import EmailChangeForm, ProfileDetailsForm
from .models import UserProfile
from .otp import mask_email

User = get_user_model()


def _profile_for(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _render_profile(request, details_form=None, email_form=None):
    profile = _profile_for(request.user)
    pending = otp.pending_target(request.user, otp.EMAIL_CHANGE)
    return render(request, 'profile.html', {
        'profile': profile,
        'details_form': details_form or ProfileDetailsForm(instance=profile, user=request.user),
        'email_form': email_form or EmailChangeForm(user=request.user),
        'pending_email': mask_email(pending),
    })


@login_required
def profile(request):
    profile_obj = _profile_for(request.user)
    if request.method == 'POST':
        form = ProfileDetailsForm(request.POST, request.FILES, instance=profile_obj, user=request.user)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'profile.updated', f'Updated profile: {request.user.username}')
            messages.success(request, 'Your profile has been saved.')
            return redirect('profile')
        return _render_profile(request, details_form=form)
    return _render_profile(request)


@login_required
@require_POST
def request_email_change(request):
    form = EmailChangeForm(request.POST, user=request.user)
    if not form.is_valid():
        return _render_profile(request, email_form=form)

    new_email = form.cleaned_data['new_email']
    if not _send_allowed(request):
        messages.error(request, SEND_LIMIT_MESSAGE)
        return redirect('profile')
    try:
        otp.issue_code(request.user, otp.EMAIL_CHANGE, to_email=new_email)
    except otp.ResendTooSoon as wait:
        # A code for another address is still live. Ask the person to wait, rather than switch addresses
        # while the first code is out.
        messages.info(request, f'A confirmation code was just sent. You can request another in {wait.seconds} seconds.')
        return redirect('profile_email_verify')
    except otp.DeliveryFailed:
        messages.error(request, 'We could not send the confirmation email right now. Please try again in a moment.')
        return redirect('profile')
    messages.success(request, f'We sent a confirmation code to {mask_email(new_email)}. Enter it to change your address.')
    return redirect('profile_email_verify')


@login_required
def verify_email_change(request):
    target = otp.pending_target(request.user, otp.EMAIL_CHANGE)
    if not target:
        messages.info(request, 'You have no email change waiting for confirmation.')
        return redirect('profile')

    if request.method == 'POST':
        code = _entered_code(request)
        if code is None:
            messages.error(request, 'Enter all six digits of the code.')
            return redirect('profile_email_verify')
        result = otp.check_code(request.user, otp.EMAIL_CHANGE, code)
        if result.status == otp.OK:
            return _finish_email_change(request, result.target)
        if result.status == otp.WRONG:
            left = result.attempts_left
            messages.error(request, f'That code is not correct. {left} {"attempt" if left == 1 else "attempts"} left.')
        elif result.status == otp.LOCKED:
            messages.error(request, 'Too many incorrect codes, so this code is locked. Request a new code below.')
        else:
            messages.error(request, 'That code has expired or has already been used. Request a new code below.')
        return redirect('profile_email_verify')

    expires_in, resend_in = otp.timers(request.user, otp.EMAIL_CHANGE)
    return render(request, 'profile_email_verify.html', _code_page_context(
        masked_email=mask_email(target),
        expires_in=expires_in,
        resend_in=resend_in,
        action_url=reverse('profile_email_verify'),
        resend_url=reverse('profile_email_resend'),
        change_url=reverse('profile'),
        change_text='Back to my profile',
    ))


def _finish_email_change(request, new_email):
    # Re-check: the address may have been taken since the code was sent.
    if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
        messages.error(request, 'Another account already uses that email address, so your address was not changed.')
        return redirect('profile')
    request.user.email = new_email
    request.user.save(update_fields=['email'])
    notifications.log_activity(request.user, 'profile.email_changed',
                               f'Email address changed for {request.user.username}')
    messages.success(request, f'Your email address is now {new_email}.')
    return redirect('profile')


@login_required
@require_POST
def resend_email_change(request):
    target = otp.pending_target(request.user, otp.EMAIL_CHANGE)
    if not target:
        return redirect('profile')
    if not _send_allowed(request):
        messages.error(request, SEND_LIMIT_MESSAGE)
    else:
        try:
            otp.issue_code(request.user, otp.EMAIL_CHANGE, to_email=target)
        except otp.ResendTooSoon as wait:
            messages.info(request, f'Please wait {wait.seconds} seconds before requesting another code.')
        except otp.DeliveryFailed:
            messages.error(request, 'We could not send the confirmation email right now. Please try again in a moment.')
        else:
            messages.success(request, f'A new confirmation code was sent to {mask_email(target)}.')
    return redirect('profile_email_verify')
