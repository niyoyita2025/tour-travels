"""
Calendar data for the booking calendar. Read-only: this module never changes seats or bookings.

Day statuses (colours on the calendar):
  available – green:  seats are open to book
  limited   – yellow: a few seats left, or pending requests on that day
  full      – red:    every open slot on that day is full or reserved
  past      – grey:   the date has gone
  none      – grey:   no open tour slot on that date
"""

import calendar as pycal
from datetime import date, timedelta

from django.conf import settings
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from .models import HOLDING_STATUSES, Booking, ScheduleRule, TourSchedule

AVAILABLE = 'available'
LIMITED = 'limited'
FULL = 'full'
PAST = 'past'
NONE = 'none'

STATUS_LABELS = {
    AVAILABLE: 'Available',
    LIMITED: 'Limited seats',
    FULL: 'Fully booked',
    PAST: 'Past date',
    NONE: 'Not offered',
}


def month_bounds(year, month):
    first = date(year, month, 1)
    last = date(year, month, pycal.monthrange(year, month)[1])
    return first, last


def _minutes(clock):
    return clock.hour * 60 + clock.minute


def schedules_in_range(start, end, package=None):
    """
    Slots in the date range, each with:
      reserved_by_time – another booking's time runs into this start time, or this tour's full
                         length (from its timetable) would run into it;
      day_left         – guests still allowed on this date under the tour's daily capacity
                         (None when the tour has no timetable).
    """
    qs = TourSchedule.objects.filter(date__range=(start, end)).select_related('package')
    if package is not None:
        qs = qs.filter(package=package)
    slots = list(qs.annotate(pending_seats=Sum('bookings__guests',
                                               filter=Q(bookings__status=Booking.Status.PENDING))))

    reservations = Booking.objects.filter(
        travel_date__range=(start, end), schedule__isnull=False, status__in=HOLDING_STATUSES,
    ).select_related('schedule')
    if package is not None:
        reservations = reservations.filter(package=package)
    by_date = {}
    guests_by_day = {}
    for booking in reservations:
        by_date.setdefault(booking.travel_date, []).append(booking)
        key = (booking.package_id, booking.travel_date)
        guests_by_day[key] = guests_by_day.get(key, 0) + booking.guests

    rules = {rule.package_id: rule for rule in ScheduleRule.objects.filter(
        package_id__in={slot.package_id for slot in slots})}

    for slot in slots:
        rule = rules.get(slot.package_id)
        slot.day_left = None
        if rule is not None:
            used = guests_by_day.get((slot.package_id, slot.date), 0)
            slot.day_left = max(rule.daily_capacity - used, 0)
        slot.reserved_by_time = False
        slot_start = _minutes(slot.start_time)
        slot_end = slot_start + (rule.tour_hours * 60 if rule is not None else 1)
        for booking in by_date.get(slot.date, []):
            if booking.schedule_id == slot.pk or booking.package_id != slot.package_id:
                continue
            window_start, window_end = booking.window_minutes
            if window_start < slot_end and slot_start < window_end:
                slot.reserved_by_time = True
                break
    return slots


def effective_remaining(slot):
    """Guests a client can still take on this slot: its own seats, capped by what the date has left."""
    remaining = slot.seats_remaining
    day_left = getattr(slot, 'day_left', None)
    if day_left is not None:
        remaining = min(remaining, day_left)
    return remaining


def _day_status(day, slots, today):
    if day < today:
        return PAST
    live = [s for s in slots if s.is_open and not s.has_started and not s.reserved_by_time]
    if not live:
        return NONE
    remaining = sum(effective_remaining(s) for s in live)
    pending = sum((s.pending_seats or 0) for s in live)
    if remaining == 0:
        return FULL
    if pending or remaining <= settings.LIMITED_SEATS_THRESHOLD:
        return LIMITED
    return AVAILABLE


def _slot_payload(slot, package_slug=None):
    remaining = effective_remaining(slot)
    bookable = slot.is_bookable and not slot.reserved_by_time and remaining > 0
    payload = {
        'id': slot.pk,
        'date': slot.date.isoformat(),
        'time': slot.start_time.strftime('%H:%M'),
        'capacity': slot.capacity,
        'booked': slot.seats_booked,
        'remaining': remaining,
        'pending': slot.pending_seats or 0,
        'bookable': bookable,
        'closed': not slot.is_open,
        'started': slot.has_started,
        'reserved': slot.reserved_by_time,
        'group': slot.group_only,
        'taken': slot.group_only and slot.seats_booked > 0,
    }
    if package_slug and bookable:
        payload['book_url'] = f"{reverse('book_package', args=[package_slug])}?schedule={slot.pk}"
    return payload


def _booking_row(booking):
    return {
        'id': booking.pk,
        'reference': booking.reference,
        'customer': booking.full_name,
        'email': booking.email,
        'phone': booking.phone,
        'tour': booking.package.title,
        'time': booking.schedule.start_time.strftime('%H:%M') if booking.schedule_id else '',
        'hours': booking.duration_hours,
        'guests': booking.guests,
        'status': booking.status,
        'status_label': booking.get_status_display(),
        'payment': booking.get_payment_status_display(),
    }


def build_month(year, month, package=None, today=None, with_bookings=False):
    """
    Return the month as weeks (Monday first) of day dicts, plus the slots for each day.
    `package` limits the calendar to one tour; None shows every tour.
    `with_bookings` adds each day's bookings (used by the administrator calendar).
    """
    today = today or timezone.localdate()
    first, last = month_bounds(year, month)
    grid = pycal.Calendar(firstweekday=0).monthdatescalendar(year, month)
    flat_start, flat_end = grid[0][0], grid[-1][-1]
    slots = schedules_in_range(flat_start, flat_end, package)
    by_day = {}
    for slot in slots:
        by_day.setdefault(slot.date, []).append(slot)

    bookings_by_day = {}
    if with_bookings:
        bookings = Booking.objects.filter(travel_date__range=(flat_start, flat_end)) \
            .select_related('package', 'schedule', 'user')
        if package is not None:
            bookings = bookings.filter(package=package)
        for booking in bookings.order_by('schedule__start_time', 'created_at'):
            bookings_by_day.setdefault(booking.travel_date, []).append(_booking_row(booking))

    package_slug = package.slug if package is not None else None
    weeks = []
    for week in grid:
        row = []
        for day in week:
            day_slots = by_day.get(day, [])
            status = _day_status(day, day_slots, today)
            live = [s for s in day_slots if s.is_open and not s.has_started and not s.reserved_by_time]
            row.append({
                'date': day,
                'iso': day.isoformat(),
                'day': day.day,
                'in_month': first <= day <= last,
                'is_today': day == today,
                'status': status,
                'label': STATUS_LABELS[status],
                'remaining': sum(effective_remaining(s) for s in live),
                'capacity': sum(s.capacity for s in live),
                'booked': sum(s.seats_booked for s in day_slots),
                'all_capacity': sum(s.capacity for s in day_slots),
                'slots': [_slot_payload(s, package_slug) for s in day_slots],
                'bookings': bookings_by_day.get(day, []),
            })
        weeks.append(row)

    prev_month = date(first.year - 1, 12, 1) if first.month == 1 else date(first.year, first.month - 1, 1)
    next_month = date(last.year + 1, 1, 1) if last.month == 12 else date(last.year, last.month + 1, 1)
    return {
        'weeks': weeks,
        'month_key': first.strftime('%Y-%m'),
        'month_label': first.strftime('%B %Y'),
        'prev': prev_month.strftime('%Y-%m'),
        'next': next_month.strftime('%Y-%m'),
        'can_go_back': first > today.replace(day=1),
    }


def availability_payload(year, month, package=None, today=None):
    """JSON for the live calendar: every day's status and every slot's remaining seats."""
    data = build_month(year, month, package, today)
    days, slots = {}, {}
    for week in data['weeks']:
        for day in week:
            days[day['iso']] = {'status': day['status'], 'label': day['label'],
                                'remaining': day['remaining'], 'capacity': day['capacity']}
            for slot in day['slots']:
                slots[str(slot['id'])] = {'remaining': slot['remaining'], 'bookable': slot['bookable'],
                                          'booked': slot['booked'], 'capacity': slot['capacity']}
    return {
        'month': f'{year:04d}-{month:02d}',
        'days': days,
        'slots': slots,
        'updated_at': timezone.now().isoformat(),
    }


def next_bookable_slots(package_ids, days=180, today=None):
    """
    For each tour, its next time a client can book: {package_id: slot}. Uses the same rules as the
    calendar (open, not started, not covered by another booking, seats left), so the destination
    pages always agree with the calendar. Tours with no bookable date in the window are left out.
    """
    today = today or timezone.localdate()
    package_ids = set(package_ids)
    best = {}
    slots = schedules_in_range(today, today + timedelta(days=days))
    for slot in sorted(slots, key=lambda s: (s.date, s.start_time)):
        if slot.package_id not in package_ids or slot.package_id in best:
            continue
        if slot.is_open and not slot.has_started and not slot.reserved_by_time and effective_remaining(slot) > 0:
            best[slot.package_id] = slot
    return best


def upcoming_bookable_slots(package, limit=10, days=120, today=None):
    """A tour's next open dates and times, in order, using the same rules as the calendar."""
    today = today or timezone.localdate()
    slots = schedules_in_range(today, today + timedelta(days=days), package)
    open_slots = [
        slot for slot in sorted(slots, key=lambda s: (s.date, s.start_time))
        if slot.is_open and not slot.has_started and not slot.reserved_by_time and effective_remaining(slot) > 0
    ]
    return open_slots[:limit]


def slot_status(slot):
    """'limited' when few seats are left or someone is waiting to pay, otherwise 'available'."""
    if (slot.pending_seats or 0) or slot.seats_remaining <= settings.LIMITED_SEATS_THRESHOLD:
        return LIMITED
    return AVAILABLE


def booking_statistics(today=None):
    """Figures for the administrator Control Panel."""
    today = today or timezone.localdate()
    now = timezone.now()
    upcoming = TourSchedule.objects.filter(date__gte=today)
    totals = upcoming.aggregate(booked=Sum('seats_booked'), capacity=Sum('capacity'))
    capacity = totals['capacity'] or 0
    booked = totals['booked'] or 0
    pending = Booking.objects.filter(status=Booking.Status.PENDING)
    return {
        'upcoming_slots': upcoming.count(),
        'full_slots': upcoming.filter(seats_booked__gte=F('capacity')).count(),
        'upcoming_seats_booked': booked,
        'upcoming_seats_free': max(capacity - booked, 0),
        'occupancy_percent': round(100 * booked / capacity) if capacity else 0,
        'pending_holds': pending.count(),
        'holds_expiring_soon': pending.filter(expires_at__lt=now + timedelta(hours=24)).count(),
        'bookings_this_month': Booking.objects.filter(created_at__year=today.year,
                                                      created_at__month=today.month).count(),
        'bookings_today': Booking.objects.filter(travel_date=today).exclude(
            status__in=[Booking.Status.CANCELLED, Booking.Status.EXPIRED]).count(),
    }
