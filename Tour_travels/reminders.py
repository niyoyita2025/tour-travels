"""
Booking reminder emails.

`send_due_reminders()` emails every pending or confirmed booking whose tour starts within
SiteSettings.reminder_hours_before and that has not had its reminder yet. It runs from:

* `python manage.py send_booking_reminders` (for a scheduled job), and
* ReminderMiddleware, at most once every CHECK_EVERY seconds while the site receives visits. Render's
  free plan has no scheduled jobs, and the keep-alive workflow visits the site every 10 minutes.
"""

import logging
from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

CHECK_EVERY = 15 * 60
BATCH = 25  # at most this many emails per run, so a visit is never held up for long
_LAST_RUN_KEY = 'booking-reminders-last-run'


def tour_start(booking):
    start = booking.schedule.start_time if booking.schedule_id else time(9, 0)
    return timezone.make_aware(datetime.combine(booking.travel_date, start))


def send_due_reminders(now=None):
    """Send the reminders that are due. Returns how many emails Gmail accepted."""
    from . import notifications
    from .models import Booking, SiteSettings

    site = SiteSettings.load()
    if not site.send_booking_reminders:
        return 0
    now = now or timezone.now()
    window_end = now + timedelta(hours=site.reminder_hours_before)
    candidates = (Booking.objects
                  .filter(status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED], reminder_sent_at__isnull=True,
                          travel_date__gte=now.astimezone(timezone.get_current_timezone()).date(),
                          travel_date__lte=window_end.astimezone(timezone.get_current_timezone()).date())
                  .select_related('package', 'schedule', 'user'))
    sent = 0
    for booking in candidates[:BATCH * 4]:
        if sent >= BATCH:
            break
        if not now < tour_start(booking) <= window_end:
            continue
        with transaction.atomic():
            # Claim the booking first, so two runs at once never send the same reminder twice.
            claimed = Booking.objects.filter(pk=booking.pk, reminder_sent_at__isnull=True).update(reminder_sent_at=now)
        if not claimed:
            continue
        if notifications.booking_reminder(booking):
            sent += 1
        else:
            Booking.objects.filter(pk=booking.pk).update(reminder_sent_at=None)  # try again on the next run
    return sent


def run_if_due():
    """Called on visits. Cheap when nothing is due: one cache lookup."""
    if not cache.add(_LAST_RUN_KEY, True, CHECK_EVERY):
        return
    try:
        send_due_reminders()
    except Exception:  # noqa: BLE001 - a reminder problem must never break a page
        logger.exception('Booking reminders failed')


class ReminderMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        from django.conf import settings

        if (settings.BOOKING_REMINDERS_ON_VISIT and request.method == 'GET'
                and not request.path.startswith(('/static/', '/media/'))):
            run_if_due()
        return response
