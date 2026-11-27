"""
Automatic timetables. A tour with a ScheduleRule gets its start times made for it:

* generate_window  opens the next `days_ahead` days on the rule's operating days, at every start
                   time from default_start to last_start, every interval_hours. Each start time
                   takes one group (group_only), and the group size is the tour's max_guests.
* sync_rule        runs after an administrator saves the rule: unbooked times that no longer match
                   the rule are removed, capacities are aligned, and the window is opened again.
* top_up           keeps a tour bookable when visitors open its calendar (as automatic tours do).
* first_fitting_slot  the earliest start time on a date that can take a group of this size now.

Booked times are never removed or changed here. Conflicts, daily capacity and seats are still
decided by bookings.py inside a transaction when a booking is saved.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .calendar_service import effective_remaining, schedules_in_range
from .models import ScheduleRule, TourSchedule


def rule_for(package):
    return ScheduleRule.objects.filter(package=package).first()


def generate_window(package, rule=None, today=None):
    """Open the rule's dates ahead. Times already in the database are left as they are. Returns how many were added."""
    rule = rule or rule_for(package)
    if rule is None:
        return 0
    now = timezone.localtime()
    today = today or now.date()
    weekdays = rule.weekdays
    starts = rule.start_times()
    rows = []
    for offset in range(rule.days_ahead):
        day = today + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        for start in starts:
            if day == now.date() and start <= now.time():
                continue  # that start time has already passed today
            rows.append(TourSchedule(package=package, date=day, start_time=start,
                                     capacity=package.max_guests, group_only=True))
    if not rows:
        return 0
    before = TourSchedule.objects.filter(package=package).count()
    with transaction.atomic():
        TourSchedule.objects.bulk_create(rows, ignore_conflicts=True)
    return TourSchedule.objects.filter(package=package).count() - before


def prune_unbooked(package, rule=None, today=None):
    """
    Remove future timetable times that nobody has booked and that no longer match the rule
    (another start time, or a day the tour no longer runs). With no rule, every unbooked
    future timetable time is removed. Returns how many were removed.
    """
    today = today or timezone.localdate()
    candidates = (TourSchedule.objects
                  .filter(package=package, group_only=True, date__gte=today, bookings__isnull=True)
                  .only('pk', 'date', 'start_time'))
    keep_times = set(rule.start_times()) if rule else set()
    weekdays = rule.weekdays if rule else set()
    stale = [
        slot.pk for slot in candidates
        if rule is None or slot.start_time not in keep_times or slot.date.weekday() not in weekdays
    ]
    if stale:
        TourSchedule.objects.filter(pk__in=stale).delete()
    return len(stale)


def sync_rule(package):
    """Bring the timetable in line with the tour's rule after an administrator saves it. Returns (added, removed)."""
    rule = rule_for(package)
    removed = prune_unbooked(package, rule)
    TourSchedule.objects.filter(package=package, group_only=True, seats_booked__lte=package.max_guests) \
        .update(capacity=package.max_guests)
    added = generate_window(package, rule)
    return added, removed


def turn_off(package):
    """Stop automatic timetabling: unbooked future times are removed and the rule is deleted."""
    removed = prune_unbooked(package, None)
    ScheduleRule.objects.filter(package=package).delete()
    return removed


def top_up(package):
    """Open the next window of times when the timetable has run short. Called when visitors open the calendar."""
    rule = rule_for(package)
    if rule is None:
        return 0
    horizon = timezone.localdate() + timedelta(days=rule.days_ahead // 2)
    if TourSchedule.objects.filter(package=package, group_only=True, date__gte=horizon).exists():
        return 0
    return generate_window(package, rule)


def first_fitting_slot(package, travel_date, guests):
    """The earliest start time on this date that can take a group of `guests` now, or None."""
    slots = schedules_in_range(travel_date, travel_date, package)
    for slot in sorted(slots, key=lambda s: s.start_time):
        if (slot.is_bookable and not slot.reserved_by_time
                and effective_remaining(slot) >= guests):
            return slot
    return None
