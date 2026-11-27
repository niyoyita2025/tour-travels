"""
Six-digit email codes for sign-up verification and forgot-password.

* Codes come from `secrets`, the operating system's cryptographic random source. They always have
  six digits, leading zeros included (for example 000417).
* Only a keyed hash of each code is stored. The code itself is never written to the database.
* A code expires after settings.OTP_LIFETIME_SECONDS, works once, and is locked after
  settings.OTP_MAX_ATTEMPTS wrong entries.
* Requesting a new code cancels every older code for the same purpose. If the new code cannot be
  emailed, the older code stays valid.
* A new code can be requested only after settings.OTP_RESEND_SECONDS.
* No more than settings.OTP_ACCOUNT_MAX_SENDS codes are emailed to one account for one purpose in
  settings.OTP_ACCOUNT_SEND_WINDOW_SECONDS. This applies across devices, unlike the per-device limit.
"""

import logging
import secrets
from datetime import timedelta
from math import ceil
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import EmailOTP

logger = logging.getLogger(__name__)

SIGNUP = EmailOTP.Purpose.SIGNUP
RESET = EmailOTP.Purpose.RESET
EMAIL_CHANGE = EmailOTP.Purpose.EMAIL_CHANGE
CODE_DIGITS = 6
_KEY_SALT = 'Tour_travels.email-otp'

# Outcomes of check_code()
OK = 'ok'
WRONG = 'wrong'          # incorrect; attempts_left says how many tries remain
EXPIRED = 'expired'      # no live code: it expired, was used, or was never issued
LOCKED = 'locked'        # too many wrong entries on this code


class CodeResult(NamedTuple):
    status: str
    attempts_left: int = 0
    target: str = ''  # for OK: the address the accepted code was sent to, when it was an email change


class ResendTooSoon(Exception):
    """A new code was requested before OTP_RESEND_SECONDS had passed. `seconds` is the wait left."""

    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


class DeliveryFailed(Exception):
    """The email service did not accept the message. No code was issued."""


class TooManyCodes(Exception):
    """This account has used up its codes for the time window. No code was issued."""


def generate_code():
    """A cryptographically secure six-digit code, zero-padded from 000000 to 999999."""
    return f'{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}'


def mask_email(address):
    """'niyoyita@gmail.com' becomes 'n***@gmail.com', so the page reveals only part of the address."""
    local, _, domain = (address or '').partition('@')
    if not domain:
        return ''
    return f'{local[:1]}***@{domain}'


def _keyed_hash(user_id, purpose, code):
    # HMAC keyed with SECRET_KEY: a copy of the database alone is not enough to test guesses offline.
    return salted_hmac(_KEY_SALT, f'{user_id}:{purpose}:{code}', algorithm='sha256').hexdigest()


def _send_count_key(user, purpose):
    return f'otp-account-sends:{user.pk}:{purpose}'


def _count_send(user, purpose):
    """Add one to the account's send count. The window starts with the first code in it."""
    key = _send_count_key(user, purpose)
    window = settings.OTP_ACCOUNT_SEND_WINDOW_SECONDS
    cache.add(key, 0, window)
    try:
        cache.incr(key)
    except ValueError:  # the window ended between the two calls above
        cache.set(key, 1, window)


def timers(user, purpose):
    """Return (seconds left on the live code, seconds until a new code may be requested)."""
    now = timezone.now()
    live = EmailOTP.objects.filter(
        user=user, purpose=purpose, used_at__isnull=True, expires_at__gt=now,
    ).order_by('-created_at').first()
    expires_in = ceil((live.expires_at - now).total_seconds()) if live else 0
    latest = EmailOTP.objects.filter(user=user, purpose=purpose).order_by('-created_at').first()
    resend_in = 0
    if latest is not None:
        resend_in = max(0, ceil(settings.OTP_RESEND_SECONDS - (now - latest.created_at).total_seconds()))
    return expires_in, resend_in


_EMAIL_TEXT = {
    SIGNUP: {
        'subject': 'Your Account Verification Code',
        'intro': 'Thank you for registering on our website.',
        'label': 'Your six-digit verification code is:',
        'closing': 'If you did not register, you can ignore this email.',
    },
    RESET: {
        'subject': 'Your Password Reset Code',
        'intro': 'We received a request to reset the password for your account.',
        'label': 'Your six-digit password reset code is:',
        'closing': 'If you did not ask for this, you can ignore this email and your password will stay the same.',
    },
    EMAIL_CHANGE: {
        'subject': 'Confirm your new email address',
        'intro': 'You asked to use this address for your Explore Rwanda account.',
        'label': 'Your six-digit confirmation code is:',
        'closing': 'If you did not ask for this change, you can ignore this email. Your account keeps its current address.',
    },
}


def _send_code_email(purpose, code, to_address):
    text = _EMAIL_TEXT[purpose]
    context = {
        'intro': text['intro'],
        'label': text['label'],
        'closing': text['closing'],
        'code': code,
        'minutes': settings.OTP_LIFETIME_SECONDS // 60,
    }
    body = render_to_string('accounts/otp_email.txt', context)
    html = render_to_string('accounts/otp_email.html', context)
    send_mail(text['subject'], body, settings.DEFAULT_FROM_EMAIL, [to_address], fail_silently=False,
              html_message=html)


def describe_email_error(error):
    """A plain explanation of why an email was not sent, for logs and the send_test_email command."""
    import smtplib
    import socket

    if settings.EMAIL_BACKEND.endswith('smtp.EmailBackend') and not settings.EMAIL_HOST_PASSWORD:
        return ('EMAIL_HOST_PASSWORD is empty. Gmail refuses to send without the 16-character '
                'Google app password for EMAIL_HOST_USER.')
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (f'Gmail rejected the sign-in for {settings.EMAIL_HOST_USER or "(EMAIL_HOST_USER is empty)"} '
                f'(SMTP {error.smtp_code}). Use a Google app password, not the normal Gmail password, '
                'and check that 2-Step Verification is on for that account.')
    if isinstance(error, smtplib.SMTPSenderRefused):
        return f'Gmail refused the sender {settings.DEFAULT_FROM_EMAIL!r} (SMTP {error.smtp_code}).'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'Gmail refused the recipient address.'
    if isinstance(error, (socket.timeout, TimeoutError)):
        return (f'Timed out connecting to {settings.EMAIL_HOST}:{settings.EMAIL_PORT}. The network or host may '
                'block outgoing SMTP.')
    if isinstance(error, (ConnectionError, socket.gaierror, OSError)):
        return f'Could not connect to {settings.EMAIL_HOST}:{settings.EMAIL_PORT} ({type(error).__name__}).'
    return f'{type(error).__name__}: {error}'


def issue_code(user, purpose, to_email=''):
    """Create a new code, email it, and only then cancel the older codes.

    An email change sends the code to `to_email`, the new address, and records that address on the code.
    Raises ResendTooSoon if a code was requested recently, TooManyCodes if the account has used its
    allowance for the window, or DeliveryFailed if the email was not accepted. In each case the previous
    code, if any, keeps working.
    """
    wait = timers(user, purpose)[1]
    if wait:
        raise ResendTooSoon(wait)
    if cache.get(_send_count_key(user, purpose), 0) >= settings.OTP_ACCOUNT_MAX_SENDS:
        raise TooManyCodes

    now = timezone.now()
    EmailOTP.objects.filter(created_at__lt=now - timedelta(days=1)).delete()  # tidy up old rows
    code = generate_code()
    otp = EmailOTP.objects.create(
        user=user,
        purpose=purpose,
        code_hash=_keyed_hash(user.pk, purpose, code),
        target_email=to_email,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.OTP_LIFETIME_SECONDS),
    )
    try:
        _send_code_email(purpose, code, to_email or user.email)
    except Exception as error:  # noqa: BLE001 - any SMTP, network or configuration error is a delivery failure
        otp.delete()
        # The reason names the setting to fix. It never includes the code, the message body or the password.
        logger.error('Could not send a %s code to user %s: %s', purpose, user.pk, describe_email_error(error))
        raise DeliveryFailed from None

    _count_send(user, purpose)
    EmailOTP.objects.filter(user=user, purpose=purpose, used_at__isnull=True).exclude(pk=otp.pk).update(used_at=now)
    return otp


def check_code(user, purpose, code):
    """Check an entered code. A correct code is used up, so it cannot be used again."""
    code = (code or '').strip()
    now = timezone.now()
    with transaction.atomic():
        live = (
            EmailOTP.objects.select_for_update()
            .filter(user=user, purpose=purpose, used_at__isnull=True, expires_at__gt=now)
            .order_by('-created_at').first()
        )
        if live is None:
            return CodeResult(EXPIRED)
        if live.attempts >= settings.OTP_MAX_ATTEMPTS:
            return CodeResult(LOCKED)

        if constant_time_compare(live.code_hash, _keyed_hash(user.pk, purpose, code)):
            # The conditional update means that of two simultaneous correct entries, only one wins.
            if EmailOTP.objects.filter(pk=live.pk, used_at__isnull=True).update(used_at=now):
                return CodeResult(OK, target=live.target_email)
            return CodeResult(EXPIRED)

        EmailOTP.objects.filter(pk=live.pk).update(attempts=F('attempts') + 1)
        attempts_left = settings.OTP_MAX_ATTEMPTS - live.attempts - 1
        if attempts_left <= 0:
            return CodeResult(LOCKED)
        return CodeResult(WRONG, attempts_left)


def pending_target(user, purpose):
    """The address of the newest unused code for this purpose (an email change), or '' if there is none.

    An expired code still counts as pending, so the person is told it expired and can ask for a new one,
    rather than being told that nothing is waiting.
    """
    latest = EmailOTP.objects.filter(user=user, purpose=purpose, used_at__isnull=True).order_by('-created_at').first()
    return latest.target_email if latest else ''


def cancel_codes(user, purpose):
    """Make every outstanding code for this purpose unusable, for example once a password is changed."""
    EmailOTP.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(used_at=timezone.now())
