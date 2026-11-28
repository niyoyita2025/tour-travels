"""
Checks for the Email centre. They never send an email, and they never show or store the password.

`check_sign_in()` opens the same secure connection to Gmail that a real send uses, signs in with the
configured account, and closes it again. If Gmail accepts the sign-in, sending will work. If it refuses,
the answer is the same plain reason the delivery log shows.
"""

import smtplib

from django.conf import settings

from .otp import describe_email_error


def check_sign_in(timeout=20):
    """Return (ok, message). The message is safe to show to an administrator."""
    if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
        return False, ('EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is empty, so there is no sign-in to check. '
                       'Set both in .env (or in the Render Environment tab).')
    try:
        with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT, timeout=timeout) as server:
            server.ehlo()
            if settings.EMAIL_USE_TLS:
                server.starttls()
                server.ehlo()
            server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
    except Exception as error:  # noqa: BLE001 - every failure is reported in plain words
        return False, describe_email_error(error)
    return True, (f'Gmail accepted the sign-in for {settings.EMAIL_HOST_USER}. '
                  'No email was sent, so this check cannot prove the inbox received anything.')
