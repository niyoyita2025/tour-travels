"""
Notifications and activity logging.

Every important event does three things:
  1. writes an activity-log entry (shown in the admin "Activity" page),
  2. creates in-app notifications (shown in the client and admin notification pages),
  3. sends an email (through emails.deliver) where the customer or staff need one.

Email functions return True only when Gmail accepted the message, so views can tell the visitor the truth.
A mail outage never breaks a booking or form submission: failures are logged in EmailLog and the server log.
"""

from django.urls import reverse

from .emails import Message, absolute, deliver
from .models import ActivityLog, Notification


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


def _site():
    from .models import SiteSettings
    return SiteSettings.load()


def _staff_email(subject, title, paragraphs, details=(), link=''):
    """Email the staff notification address, if one is set and staff emails are switched on."""
    site = _site()
    if not (site.staff_notification_email and site.email_staff_on_booking):
        return False
    return deliver(Message(kind='staff_alert', to=site.staff_notification_email, subject=subject, title=title,
                           paragraphs=list(paragraphs), details=list(details),
                           button_url=absolute(link) if link else '', button_text='Open in the dashboard'))


def _booking_details(booking):
    rows = [('Reference', booking.reference), ('Tour', booking.package.title),
            ('Date', f'{booking.travel_date:%A %d %B %Y}')]
    if booking.schedule_id:
        rows.append(('Starts at', f'{booking.schedule.start_time:%H:%M}'))
    rows += [('Guests', booking.guests), ('Total', f'USD {booking.total_price}'),
             ('Status', booking.get_status_display())]
    if booking.package.start_location:
        rows.append(('Meeting point', booking.package.start_location))
    return rows


def _booking_email(booking, kind, subject, title, paragraphs, closing=''):
    return deliver(Message(kind=kind, to=booking.email, subject=subject, title=title,
                           paragraphs=[f'Dear {booking.full_name},'] + list(paragraphs),
                           details=_booking_details(booking),
                           button_url=absolute(booking.get_absolute_url()), button_text='View my booking',
                           closing=closing))


# ---------- accounts ----------

def account_verified(user):
    """Welcome email after the sign-up code is accepted and the account is active."""
    site = _site()
    sent = deliver(Message(
        kind='welcome', to=user.email, subject=f'Welcome to {site.site_name}',
        title='Your account is ready',
        paragraphs=[f'Hello {user.first_name or user.username},',
                    'Your email address is verified and your account is active. You can now book tours, '
                    'follow your bookings and payments, and review the tours you take.'],
        button_url=absolute(reverse('dashboard')), button_text='Go to my account',
        closing='If you did not create this account, please contact us.'))
    log_activity(user, 'account.verified', f'Email verified: {user.username}')
    return sent


def password_changed(user, how='reset'):
    """Security notice after a password reset or change. Never contains the password."""
    detail = ('with an emailed code' if how == 'reset' else 'from your account settings')
    return deliver(Message(
        kind='password_changed', to=user.email, subject='Your password was changed',
        title='Your password was changed',
        paragraphs=[f'Hello {user.first_name or user.username},',
                    f'The password for your account was just changed {detail}.'],
        button_url=absolute(reverse('login')), button_text='Log in',
        closing='If you did not do this, reset your password straight away and contact us.'))


# ---------- bookings ----------

def booking_received(booking):
    """Returns True when the customer's confirmation email was accepted by the mail server."""
    sent = _booking_email(
        booking, 'booking_received', f'Booking request {booking.reference} received',
        'We received your booking request',
        ['Thank you for booking with us. Your request is pending confirmation: we will confirm availability '
         'and arrangements with you shortly.',
         'No payment is needed to submit this request. Payment is arranged directly with us, and your payment '
         'status is shown on your booking page.'])
    _staff_email(f'New booking {booking.reference}', 'New booking request',
                 [f'{booking.full_name} ({booking.email}, {booking.phone}) sent a booking request.'],
                 _booking_details(booking), f'/manage/bookings/?q={booking.reference}')
    notify_user(booking.user, 'Booking request received',
                f'Reference {booking.reference} for {booking.package.title} is pending confirmation.',
                booking.get_absolute_url())
    notify_admins('New booking', f'{booking.reference}: {booking.package.title} by {booking.full_name}.',
                  f'/manage/bookings/?q={booking.reference}')
    log_activity(booking.user, 'booking.created',
                 f'{booking.reference}: {booking.package.title}, {booking.guests} guest(s)')
    return sent


def booking_status_changed(booking, actor=None):
    if booking.status == booking.Status.CONFIRMED:
        kind, headline = 'booking_confirmed', 'Your booking is confirmed.'
        extra = ['We look forward to welcoming you. Please arrive a few minutes before the start time.']
    elif booking.status == booking.Status.CANCELLED:
        kind, headline = 'booking_cancelled', 'Your booking has been cancelled.'
        extra = ['If you have questions about this cancellation, reply to this email or contact us.']
    else:
        kind, headline = 'booking_status', f'Your booking status is now: {booking.get_status_display()}.'
        extra = []
    _booking_email(booking, kind, f'Booking {booking.reference}: {booking.get_status_display()}', headline,
                   extra)
    notify_user(booking.user, f'Booking {booking.get_status_display().lower()}',
                headline + f' ({booking.reference})', booking.get_absolute_url())
    log_activity(actor, 'booking.status',
                 f'{booking.reference} set to {booking.get_status_display().lower()}')


def booking_cancelled_by_customer(booking):
    _booking_email(booking, 'booking_cancelled', f'Booking {booking.reference} cancelled',
                   'Your booking has been cancelled',
                   ['You cancelled this booking. The seats have been released.'],
                   closing='You are welcome to book again at any time.')
    _staff_email(f'Booking {booking.reference} cancelled by customer', 'A customer cancelled a booking',
                 [f'{booking.full_name} cancelled booking {booking.reference}.'], _booking_details(booking),
                 f'/manage/bookings/?q={booking.reference}')
    notify_admins('Booking cancelled by customer', f'{booking.reference} ({booking.package.title}).',
                  f'/manage/bookings/?q={booking.reference}')
    log_activity(booking.user, 'booking.cancelled_by_customer', f'{booking.reference}: {booking.package.title}')


def booking_updated(booking, actor=None):
    """An administrator changed a booking's date, time, duration, guests or details (a reschedule)."""
    _booking_email(booking, 'booking_updated', f'Booking {booking.reference} updated',
                   'Your booking has been changed',
                   ['We updated your booking. The new details are below.'],
                   closing='If the new details do not suit you, contact us and we will help.')
    notify_user(booking.user, 'Your booking was updated',
                f'{booking.reference}: {booking.travel_date:%d %b %Y}, {booking.duration_hours} hour(s).',
                booking.get_absolute_url())
    log_activity(actor, 'booking.updated', f'{booking.reference} updated')


def booking_reminder(booking):
    sent = _booking_email(booking, 'booking_reminder', f'Reminder: {booking.package.title} is coming up',
                          'Your tour is coming up',
                          ['This is a friendly reminder of your upcoming tour.']
                          + ([booking.package.travel_instructions] if booking.package.travel_instructions else []),
                          closing='Need to change something? Contact us as soon as possible.')
    if sent:
        notify_user(booking.user, 'Tour reminder',
                    f'{booking.package.title} on {booking.travel_date:%d %b %Y}.', booking.get_absolute_url())
    return sent


def schedule_changed(booking, old_description, actor=None):
    """A start time with this booking on it was moved or closed by staff."""
    _booking_email(booking, 'schedule_changed', f'Schedule change for booking {booking.reference}',
                   'The schedule for your tour changed',
                   [f'The tour time you booked ({old_description}) was changed. Your booking now reads as below.'],
                   closing='If the new time does not suit you, contact us and we will find another.')
    notify_user(booking.user, 'Tour schedule changed', f'{booking.reference}: please check the new time.',
                booking.get_absolute_url())
    log_activity(actor, 'booking.schedule_changed', f'{booking.reference}: was {old_description}')


# ---------- payments and messages ----------

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
    _booking_email(booking, 'payment_status', f'Payment status for booking {booking.reference}: {status}',
                   f'Payment status: {status}',
                   [text, 'Payment is arranged directly with us. Contact us if you have questions.'])
    notify_user(booking.user, f'Payment status: {status}', text, booking.get_absolute_url())
    log_activity(actor, 'payment.status', f'{booking.reference} payment status set to {status}')


def inquiry_received(inquiry):
    _staff_email(f'New inquiry from {inquiry.name}', 'New message from the contact form',
                 [inquiry.message],
                 [('From', inquiry.name), ('Email', inquiry.email), ('Phone', inquiry.phone or '—'),
                  ('Interested in', str(inquiry.package or 'not specified'))],
                 '/manage/inquiries/')
    notify_admins('New enquiry', f'{inquiry.name} ({inquiry.email}) sent a message.', '/manage/inquiries/')
    log_activity(None, 'inquiry.received', f'Message from {inquiry.name} <{inquiry.email}>')
