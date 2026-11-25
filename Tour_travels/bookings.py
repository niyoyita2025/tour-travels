"""
Seat accounting and time conflicts for tour bookings. This is the only module that changes
seats_booked, and the only place that decides whether a booking may be saved.

Every availability decision is made here, on the server, inside a database transaction.
The calendar and its JavaScript only display what the server decides.

Two rules protect the calendar:

1. Seats. A time slot has a capacity (guests). Reserving seats is one conditional UPDATE:

       UPDATE slot SET seats_booked = seats_booked + n
       WHERE id = X AND is_open AND seats_booked + n <= capacity

   It changes a row only while seats remain, so two simultaneous requests for the last
   seat cannot both succeed, on any database. A database CHECK constraint is a second guard.

2. Time. A booking occupies a window: its start time for its duration in hours. For one
   tour on one date, a new booking may not overlap a booking that starts at a DIFFERENT
   time (that is a conflict: the tour is already reserved). Bookings at the SAME start time
   share that slot and are limited by its capacity instead.

Reservation policy:
* A pending request holds its seats for settings.BOOKING_HOLD_HOURS.
* If it is not confirmed in that time it becomes "expired" and its seats are released.
* Cancelled, expired and completed bookings release their seats and their time.
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from . import notifications
from .models import HOLDING_STATUSES, Booking, TourSchedule


class BookingUnavailable(Exception):
    """The booking cannot be saved. The message is safe to show to the customer."""


FULL_MESSAGE = 'Sorry, this date is now fully booked. Please choose another date or time.'
DUPLICATE_MESSAGE = 'You already have an active booking for this date and time.'
CLOSED_MESSAGE = 'This date is no longer open for booking.'


def hold_deadline():
    return timezone.now() + timedelta(hours=settings.BOOKING_HOLD_HOURS)


def _clock(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def _reserve(schedule_id, seats):
    """Take `seats` on a slot. Returns True only if the slot had room at this exact moment."""
    updated = TourSchedule.objects.filter(
        pk=schedule_id,
        is_open=True,
        seats_booked__lte=F('capacity') - seats,
    ).update(seats_booked=F('seats_booked') + seats)
    return updated == 1


def _release(schedule_id, seats):
    TourSchedule.objects.filter(pk=schedule_id).update(seats_booked=F('seats_booked') - seats)


def check_slot_open(schedule):
    """Raise BookingUnavailable if the slot is closed, has started, or has no seats left."""
    if not schedule.is_open or schedule.has_started:
        raise BookingUnavailable(CLOSED_MESSAGE)
    if schedule.seats_remaining <= 0:
        raise BookingUnavailable(FULL_MESSAGE)


def find_conflict(package, travel_date, start_minutes, duration_hours,
                  exclude_schedule_id=None, exclude_booking_id=None):
    """
    The first other active booking for this tour on this date whose time overlaps the
    requested window at a different start time. Returns None when the time is free.
    """
    end_minutes = start_minutes + duration_hours * 60
    candidates = (Booking.objects
                  .filter(package=package, travel_date=travel_date, schedule__isnull=False,
                          status__in=HOLDING_STATUSES)
                  .select_related('schedule'))
    if exclude_booking_id is not None:
        candidates = candidates.exclude(pk=exclude_booking_id)
    for other in candidates:
        if exclude_schedule_id is not None and other.schedule_id == exclude_schedule_id:
            continue  # the same slot: capacity decides, not time
        other_start, other_end = other.window_minutes
        if start_minutes < other_end and other_start < end_minutes:
            return other
    return None


def _conflict_message(other):
    start, end = other.window_minutes
    return (f'This tour is already reserved from {_clock(start)} to {_clock(end)} on this date. '
            'Please choose a shorter duration or a different time.')


def _slot_start_minutes(schedule):
    return schedule.start_time.hour * 60 + schedule.start_time.minute


def create_booking(*, user, schedule, guests, duration_hours=2, status=Booking.Status.PENDING, **fields):
    """
    Reserve seats, check the time, and save a new booking in one transaction.
    Raises BookingUnavailable if the slot is closed, full, overlaps another reservation,
    or the client already has an active booking for this slot.
    """
    check_slot_open(schedule)
    holds = status in HOLDING_STATUSES
    with transaction.atomic():
        if holds:
            other = find_conflict(schedule.package, schedule.date, _slot_start_minutes(schedule),
                                  duration_hours, exclude_schedule_id=schedule.pk)
            if other is not None:
                raise BookingUnavailable(_conflict_message(other))
            if not _reserve(schedule.pk, guests):
                raise BookingUnavailable(FULL_MESSAGE)
        booking = Booking(
            user=user, package=schedule.package, schedule=schedule, travel_date=schedule.date,
            guests=guests, duration_hours=duration_hours, status=status, **fields,
        )
        if status == Booking.Status.PENDING:
            booking.expires_at = hold_deadline()
        try:
            with transaction.atomic():  # savepoint: a duplicate must not poison the outer transaction
                booking.save()
        except IntegrityError as exc:
            raise BookingUnavailable(DUPLICATE_MESSAGE) from exc
    return booking


def update_booking(booking, *, schedule, guests, duration_hours, **fields):
    """
    Move or change an existing booking (administrators): another slot, guests, or duration.
    Seats and time are re-checked, and the old seats are released only if the change succeeds.
    """
    with transaction.atomic():
        holding = booking.status in HOLDING_STATUSES
        if holding and booking.schedule_id:
            _release(booking.schedule_id, booking.guests)
        if schedule.pk != booking.schedule_id:
            check_slot_open(schedule)
        if holding:
            other = find_conflict(schedule.package, schedule.date, _slot_start_minutes(schedule),
                                  duration_hours, exclude_schedule_id=schedule.pk,
                                  exclude_booking_id=booking.pk)
            if other is not None:
                raise BookingUnavailable(_conflict_message(other))
            if not _reserve(schedule.pk, guests):
                raise BookingUnavailable(FULL_MESSAGE)
        values = {
            'schedule': schedule, 'travel_date': schedule.date, 'guests': guests,
            'duration_hours': duration_hours, 'total_price': booking.package.price * guests,
            'updated_at': timezone.now(), **fields,
        }
        try:
            with transaction.atomic():
                Booking.objects.filter(pk=booking.pk).update(**values)
        except IntegrityError as exc:
            raise BookingUnavailable(DUPLICATE_MESSAGE) from exc
    booking.refresh_from_db()
    return booking


def set_booking_status(booking, new_status):
    """
    Change a booking's status and keep seats in step.
    Reactivating a cancelled or expired booking needs free seats again.
    """
    old_status = booking.status
    if old_status == new_status:
        return booking
    with transaction.atomic():
        if booking.schedule_id:
            was_holding = old_status in HOLDING_STATUSES
            will_hold = new_status in HOLDING_STATUSES
            if will_hold and not was_holding:
                if not _reserve(booking.schedule_id, booking.guests):
                    raise BookingUnavailable('There are no seats left on this date to reopen this booking.')
            elif was_holding and not will_hold:
                _release(booking.schedule_id, booking.guests)

        updates = {'status': new_status, 'updated_at': timezone.now()}
        if new_status == Booking.Status.PENDING:
            updates['expires_at'] = hold_deadline()
        elif new_status not in (Booking.Status.PENDING,):
            updates['expires_at'] = None
        try:
            with transaction.atomic():
                changed = Booking.objects.filter(pk=booking.pk, status=old_status).update(**updates)
        except IntegrityError as exc:
            raise BookingUnavailable(DUPLICATE_MESSAGE) from exc
        if changed != 1:
            raise BookingUnavailable('This booking was changed by someone else. Please refresh and try again.')
    booking.refresh_from_db()
    return booking


def expire_stale_holds(now=None):
    """
    Release pending requests that were not confirmed in time. Returns the number expired.
    Safe to run often and from several places: each booking is changed only once.
    """
    now = now or timezone.now()
    candidate_ids = list(
        Booking.objects.filter(status=Booking.Status.PENDING, expires_at__isnull=False, expires_at__lt=now)
        .values_list('pk', flat=True)
    )
    expired = []
    for pk in candidate_ids:
        with transaction.atomic():
            # The conditional update is the claim: if an administrator confirmed the
            # booking meanwhile, nothing matches and seats are left alone.
            changed = Booking.objects.filter(
                pk=pk, status=Booking.Status.PENDING, expires_at__lt=now,
            ).update(status=Booking.Status.EXPIRED, updated_at=now)
            if not changed:
                continue
            booking = Booking.objects.select_related('schedule', 'package', 'user').get(pk=pk)
            if booking.schedule_id:
                _release(booking.schedule_id, booking.guests)
            expired.append(booking)
    for booking in expired:
        notifications.booking_status_changed(booking)
    return len(expired)
