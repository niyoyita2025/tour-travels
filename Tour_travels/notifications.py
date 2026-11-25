"""
Notifications and activity logging.

Every important event does three things:
  1. writes an activity-log entry (shown in the admin "Activity" page),
  2. creates in-app notifications (shown in the client and admin notification pages),
  3. sends an email where the customer or staff need one.

Failures are logged rather than raised, so a mail outage never breaks a
customer's booking or form submission.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

from .models import ActivityLog, Notification

logger = logging.getLogger(__name__)


# ---------- building blocks ----------

def log_activity(actor, action, description):
    actor = actor if (actor and getattr(actor, 'is_authenticated', False)) else None
    ActivityLog.objects.create(actor=actor, action=action, description=description[:255])


def notify_user(user, title, message='', link=''):
    if user is None:
        return
    Notification.objects.create(recipient=user, title=title[:160], message=message, link=link)


def notify_admins(title, message='', link=''):
    from django.contrib.auth import get_user_model
    admins = get_user_model().objects.filter(is_staff=True, is_active=True)
    Notification.objects.bulk_create([
        Notification(recipient=admin, title=title[:160], message=message, link=link) for admin in admins
    ])


def _send(subject, body, recipients):
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    try:
        send_mail(
            subject=f'{settings.SITE_NAME}: {subject}',
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
        )
    except Exception:  # noqa: BLE001 - logged, never shown to the user
        logger.exception('Could not send email "%s" to %s', subject, recipients)


def _staff_email_recipients():
    from .models import SiteSettings
    return [SiteSettings.load().staff_notification_email]


# ---------- events ----------

def booking_received(booking):
    body = (
        f'Dear {booking.full_name},\n\n'
        f'Thank you for your booking request {booking.reference}.\n\n'
        f'Tour: {booking.package.title}\n'
        f'Travel date: {booking.travel_date:%d %B %Y}\n'
        f'Guests: {booking.guests}\n'
        f'Total: USD {booking.total_price}\n\n'
        'Your booking is pending confirmation. Our team will confirm availability '
        'and arrangements with you shortly.\n\n'
        'No payment is needed to submit this request. Payment is arranged directly '
        'with the tour operator; your payment status is shown on your booking page.\n\n'
        f'Booking details: {settings.SITE_URL}{booking.get_absolute_url()}\n'
    )
    _send(f'Booking request {booking.reference} received', body, [booking.email])
    _send(f'New booking {booking.reference}',
          f'{booking.full_name} ({booking.email}) booked {booking.package.title} '
          f'for {booking.guests} guest(s) on {booking.travel_date}.',
          _staff_email_recipients())
    notify_user(booking.user, 'Booking request received',
                f'Reference {booking.reference} for {booking.package.title} is pending confirmation.',
                booking.get_absolute_url())
    notify_admins('New booking', f'{booking.reference}: {booking.package.title} by {booking.full_name}.',
                  f'/manage/bookings/?q={booking.reference}')
    log_activity(booking.user, 'booking.created',
                 f'{booking.reference}: {booking.package.title}, {booking.guests} guest(s)')


def booking_status_changed(booking, actor=None):
    if booking.status == booking.Status.CONFIRMED:
        headline = 'Your booking is confirmed.'
    elif booking.status == booking.Status.CANCELLED:
        headline = 'Your booking has been cancelled.'
    else:
        headline = f'Your booking status is now: {booking.get_status_display()}.'
    body = (
        f'Dear {booking.full_name},\n\n{headline}\n\n'
        f'Reference: {booking.reference}\n'
        f'Tour: {booking.package.title}\n'
        f'Travel date: {booking.travel_date:%d %B %Y}\n\n'
        f'View your booking: {settings.SITE_URL}{booking.get_absolute_url()}\n'
    )
    _send(f'Booking {booking.reference}: {booking.get_status_display()}', body, [booking.email])
    notify_user(booking.user, f'Booking {booking.get_status_display().lower()}',
                headline + f' ({booking.reference})', booking.get_absolute_url())
    log_activity(actor, 'booking.status',
                 f'{booking.reference} set to {booking.get_status_display().lower()}')


def booking_cancelled_by_customer(booking):
    _send(f'Booking {booking.reference} cancelled by customer',
          f'{booking.full_name} cancelled booking {booking.reference} ({booking.package.title}).',
          _staff_email_recipients())
    notify_admins('Booking cancelled by customer', f'{booking.reference} ({booking.package.title}).',
                  f'/manage/bookings/?q={booking.reference}')
    log_activity(booking.user, 'booking.cancelled_by_customer', f'{booking.reference}: {booking.package.title}')


def payment_recorded(payment, actor=None):
    """Staff recorded money received for a booking. Recorded for the activity log only."""
    booking = payment.booking
    log_activity(actor, 'payment.recorded',
                 f'USD {payment.amount} ({payment.get_method_display()}) recorded for {booking.reference}')


def payment_status_changed(booking, actor=None):
    """Staff changed a booking's payment status. The client is told by email and in the website."""
    status = booking.get_payment_status_display()
    if booking.payment_status == booking.PaymentStatus.VERIFIED:
        text = 'Your payment has been verified. Thank you.'
    elif booking.payment_status == booking.PaymentStatus.PAID:
        text = 'Your booking is fully paid. Thank you.'
    elif booking.payment_status == booking.PaymentStatus.PARTIAL:
        text = f'We have recorded part of your payment. The balance due is USD {booking.balance_due}.'
    else:
        text = f'Your booking is currently unpaid. The balance due is USD {booking.balance_due}.'
    body = (
        f'Dear {booking.full_name},\n\n{text}\n\n'
        f'Booking: {booking.reference}\nPayment status: {status}\n'
        f'Payment is arranged directly with the tour operator. Contact us if you have questions.\n'
    )
    _send(f'Payment status for booking {booking.reference}: {status}', body, [booking.email])
    notify_user(booking.user, f'Payment status: {status}', text, booking.get_absolute_url())
    log_activity(actor, 'payment.status', f'{booking.reference} payment status set to {status}')


def inquiry_received(inquiry):
    _send(f'New inquiry from {inquiry.name}',
          f'From: {inquiry.name} <{inquiry.email}> {inquiry.phone}\n'
          f'Interested in: {inquiry.package or "not specified"}\n\n{inquiry.message}',
          _staff_email_recipients())
    notify_admins('New enquiry', f'{inquiry.name} ({inquiry.email}) sent a message.', '/manage/inquiries/')
    log_activity(None, 'inquiry.received', f'Message from {inquiry.name} <{inquiry.email}>')


def booking_updated(booking, actor=None):
    """An administrator changed a booking's date, time, duration, guests or details."""
    body = (
        f'Dear {booking.full_name},\n\nYour booking {booking.reference} has been updated.\n\n'
        f'Tour: {booking.package.title}\n'
        f'Date: {booking.travel_date:%d %B %Y}'
        + (f' at {booking.schedule.start_time:%H:%M}' if booking.schedule_id else '') + '\n'
        f'Duration: {booking.duration_hours} hour(s)\n'
        f'Guests: {booking.guests}\n'
        f'Total: USD {booking.total_price}\n\n'
        f'View your booking: {settings.SITE_URL}{booking.get_absolute_url()}\n'
    )
    _send(f'Booking {booking.reference} updated', body, [booking.email])
    notify_user(booking.user, 'Your booking was updated',
                f'{booking.reference}: {booking.travel_date:%d %b %Y}, {booking.duration_hours} hour(s).',
                booking.get_absolute_url())
    log_activity(actor, 'booking.updated', f'{booking.reference} updated')
