"""
Every destination has a bookable tour, created automatically.

* Adding a destination creates its tour ("Visit <name>"), linked to that destination, with open dates
  for the next few days. Clients can book it straight away, like any other tour.
* Editing a destination does NOT change its tour. The administrator edits the tour in Tour Management.
* Deleting a destination removes its automatic tour too, but only if nobody has booked it and the tour
  visits no other place. Otherwise the deletion is refused.
* Open dates are kept ahead of time: the calendar tops them up, and `open_destination_tours` does it for all.

Settings that control the defaults: AUTO_TOUR_PRICE, AUTO_TOUR_MAX_GUESTS, AUTO_TOUR_CAPACITY,
AUTO_TOUR_DAYS_AHEAD and AUTO_TOUR_START_TIME.
"""

import os
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

from . import scheduling
from .models import Category, ScheduleRule, TourPackage, TourSchedule

AUTO_CATEGORY = 'Destination visits'


def _short(text, limit=200):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def open_dates(tour, days=None):
    """Open the next `days` dates at the default start time. Dates already open are left as they are."""
    days = days or settings.AUTO_TOUR_DAYS_AHEAD
    start_time = datetime.strptime(settings.AUTO_TOUR_START_TIME, '%H:%M').time()
    today = timezone.localdate()
    TourSchedule.objects.bulk_create(
        [TourSchedule(package=tour, date=today + timedelta(days=offset), start_time=start_time,
                      capacity=settings.AUTO_TOUR_CAPACITY)
         for offset in range(days)],
        ignore_conflicts=True,
    )


def _copy_photo(destination, tour):
    """Give the tour its own copy of the destination photo, so later changes to either do not touch the other."""
    if not destination.image:
        tour.image_url = destination.image_url
        return
    try:
        destination.image.open('rb')
        try:
            data = destination.image.read()
        finally:
            destination.image.close()
    except OSError:
        tour.image_url = destination.image_url
        return
    tour.image.save(os.path.basename(destination.image.name), ContentFile(data), save=False)


def ensure_destination_tour(destination):
    """
    The bookable tour for a destination. It is created the first time, and its open dates are topped up
    on every call. Anything the administrator changes on the tour afterwards is never overwritten here.
    """
    tour = TourPackage.objects.filter(auto_for=destination).first()
    if tour is None:
        category, _ = Category.objects.get_or_create(name=AUTO_CATEGORY)
        description = destination.description.strip() or destination.name
        tour = TourPackage(
            title=f'Visit {destination.name}'[:150],
            summary=_short(description),
            description=description,
            price=Decimal(settings.AUTO_TOUR_PRICE),
            duration_days=1,
            max_guests=settings.AUTO_TOUR_MAX_GUESTS,
            category=category,
            auto_for=destination,
            is_active=True,
        )
        _copy_photo(destination, tour)
        tour.save()
        tour.destinations.add(destination)
    open_dates(tour)
    return tour


def top_up_dates(tour):
    """
    Keep an automatic tour bookable. When less than half of the open-dates window is left ahead,
    a fresh window is opened. Dates the administrator has closed stay closed.
    A tour with its own timetable (ScheduleRule) is topped up by the timetable instead.
    """
    if ScheduleRule.objects.filter(package=tour).exists():
        scheduling.top_up(tour)
        return
    if not tour.auto_for_id:
        return
    horizon = timezone.localdate() + timedelta(days=settings.AUTO_TOUR_DAYS_AHEAD // 2)
    if not TourSchedule.objects.filter(package=tour, date__gte=horizon).exists():
        open_dates(tour)


def auto_tours_blocking(destination):
    """
    Tours that keep this destination from being deleted: a hand-made tour that visits it, an automatic tour
    that also visits other places, or any tour that already has bookings. An empty list means it can go.
    """
    tours = TourPackage.objects.filter(Q(destinations=destination) | Q(auto_for=destination)).distinct()
    return [
        tour for tour in tours
        if tour.auto_for_id != destination.pk
        or tour.destinations.exclude(pk=destination.pk).exists()
        or tour.bookings.exists()
    ]


def remove_auto_tours(destination):
    """Delete the automatic tour made for this destination. Only call this when auto_tours_blocking is empty."""
    TourSchedule.objects.filter(package__auto_for=destination).delete()
    TourPackage.objects.filter(auto_for=destination).delete()
