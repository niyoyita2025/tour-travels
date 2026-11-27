"""Startup checks. They run with runserver, migrate and `manage.py check`, so a missing Gmail setting is
reported when the site starts instead of when a visitor's code fails to send."""

from email.utils import parseaddr

from django.conf import settings
from django.core.checks import Warning, register

SMTP_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'


@register()
def email_settings_check(app_configs, **kwargs):
    if settings.EMAIL_BACKEND != SMTP_BACKEND:
        return []
    problems = []
    if not settings.EMAIL_HOST_USER:
        problems.append(Warning(
            'EMAIL_HOST_USER is empty, so no sign-up or password-reset codes can be emailed.',
            hint='Set EMAIL_HOST_USER to the Gmail address that sends the codes.',
            id='Tour_travels.W001',
        ))
    if not settings.EMAIL_HOST_PASSWORD:
        problems.append(Warning(
            'EMAIL_HOST_PASSWORD is empty, so Gmail will refuse every email (codes and reset links).',
            hint='Create a Google app password (myaccount.google.com > Security > App passwords) and set '
                 'EMAIL_HOST_PASSWORD to it in .env, or in the Render Environment tab for the live site.',
            id='Tour_travels.W002',
        ))
    sender = parseaddr(settings.DEFAULT_FROM_EMAIL)[1].lower()
    if settings.EMAIL_HOST_USER and sender and sender != settings.EMAIL_HOST_USER.lower():
        problems.append(Warning(
            f'DEFAULT_FROM_EMAIL ({sender}) is not EMAIL_HOST_USER. Gmail replaces it with the signed-in '
            'account, and some inboxes treat the mismatch as spam.',
            hint='Use the same Gmail address in DEFAULT_FROM_EMAIL, e.g. "Explore Rwanda <you@gmail.com>".',
            id='Tour_travels.W003',
        ))
    return problems
