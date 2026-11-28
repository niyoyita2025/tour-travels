"""
Every email the website sends goes through `deliver()`.

* Each kind of email has its own HTML template in templates/emails/, all built on emails/base.html
  (logo, website name, title, details, code or button, contact details and footer). A plain-text
  version is built from the same content, so every message is readable in any mail program.
* `deliver()` returns True only after the mail server (Gmail) accepted the message. Callers decide what
  to tell the visitor from that result, so the site never claims an email was sent when it was not.
* Every attempt is recorded in EmailLog with its outcome and a plain reason on failure. The log never
  holds codes, passwords or message bodies.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

# kind -> template. Each template extends emails/base.html and adds its own heading style and content.
TEMPLATES = {
    'signup_code': 'emails/code.html',
    'reset_code': 'emails/code.html',
    'staff_reset_code': 'emails/code.html',
    'email_change_code': 'emails/code.html',
    'welcome': 'emails/welcome.html',
    'password_changed': 'emails/password_changed.html',
    'booking_received': 'emails/booking_received.html',
    'booking_confirmed': 'emails/booking_status.html',
    'booking_cancelled': 'emails/booking_status.html',
    'booking_status': 'emails/booking_status.html',
    'booking_updated': 'emails/booking_updated.html',
    'booking_reminder': 'emails/booking_reminder.html',
    'schedule_changed': 'emails/schedule_changed.html',
    'payment_status': 'emails/payment_status.html',
    'staff_alert': 'emails/staff_alert.html',
    'test': 'emails/test.html',
}


@dataclass
class Message:
    kind: str
    to: str
    subject: str
    title: str
    paragraphs: list = field(default_factory=list)
    details: list = field(default_factory=list)  # (label, value) pairs
    code: str = ''
    code_minutes: int = 0
    button_url: str = ''
    button_text: str = ''
    closing: str = ''


def absolute(url):
    """A full https address for a site path or media URL, for use inside emails."""
    if not url or url.startswith(('http://', 'https://')):
        return url
    return settings.SITE_URL.rstrip('/') + '/' + url.lstrip('/')


def _brand():
    from .models import SiteSettings

    site = SiteSettings.load()
    return {
        'site': site,
        'site_name': site.site_name,
        'logo_url': absolute(site.logo_url),
        'site_url': settings.SITE_URL.rstrip('/'),
        'contact_email': site.contact_email,
        'contact_phone': site.contact_phone,
        'contact_address': site.contact_address,
    }


def _plain_text(message, brand):
    parts = [message.title, '']
    parts += [paragraph for paragraph in message.paragraphs for paragraph in (paragraph, '')]
    if message.code:
        parts += [message.code, '']
        if message.code_minutes:
            parts += [f'This code expires after {message.code_minutes} minutes. Do not share it with anyone.', '']
    if message.details:
        parts += [f'{label}: {value}' for label, value in message.details] + ['']
    if message.button_url:
        parts += [f'{message.button_text or "Open"}: {message.button_url}', '']
    if message.closing:
        parts += [message.closing, '']
    contact = ' | '.join(filter(None, [brand['contact_email'], brand['contact_phone']]))
    parts += ['Thank you.', brand['site_name'], brand['site_url']] + ([contact] if contact else [])
    return '\n'.join(parts).strip() + '\n'


def render(message):
    """(subject, text, html) for a message. Used by deliver() and by the email preview page."""
    brand = _brand()
    context = {**brand, 'm': message}
    html = render_to_string(TEMPLATES.get(message.kind, 'emails/base.html'), context)
    return message.subject, _plain_text(message, brand), html


def deliver(message, raise_errors=False):
    """Send one message. Returns True when the mail server accepted it, False otherwise.

    With raise_errors=True a failure is raised after it is logged (one-time codes use this, so the code
    can be withdrawn).
    """
    from .models import EmailLog
    from .otp import describe_email_error

    if not message.to:
        return False
    subject, text, html = render(message)
    email = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [message.to])
    email.attach_alternative(html, 'text/html')
    try:
        email.send(fail_silently=False)
    except Exception as error:  # noqa: BLE001 - any SMTP, network or configuration error is a failure
        reason = describe_email_error(error)
        logger.error('Could not send a %s email to %s: %s', message.kind, message.to, reason)
        EmailLog.objects.create(kind=message.kind, recipient=message.to, subject=subject[:200],
                                status=EmailLog.Status.FAILED, error=reason[:300])
        if raise_errors:
            raise
        return False
    EmailLog.objects.create(kind=message.kind, recipient=message.to, subject=subject[:200],
                            status=EmailLog.Status.SENT)
    return True
