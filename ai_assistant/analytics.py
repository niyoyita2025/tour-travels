"""
Booking analytics for administrators.

Everything here is an aggregate over the bookings already stored on the website: totals, popular tours and
destinations, demand by weekday, start time and month, and a simple forecast. No names, e-mail addresses or
phone numbers are read or returned.

Demand counts bookings that hold or used a seat (pending, confirmed and completed). Cancelled and expired
bookings are not demand.

Recommendations only ever list dates the booking system reports as bookable, using the same rules as the
calendar. Analytics can suggest, but it never changes capacity or overrides availability.
"""

from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from Tour_travels.calendar_service import effective_remaining, upcoming_bookable_slots
from Tour_travels.models import Booking, TourPackage

DEMAND_STATUSES = (Booking.Status.PENDING, Booking.Status.CONFIRMED, Booking.Status.COMPLETED)
REVENUE_STATUSES = (Booking.Status.CONFIRMED, Booking.Status.COMPLETED)
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
HIGH_DEMAND = 1.25   # a month with 25% more guests than an average month
LOW_DEMAND = 0.75    # a month with 25% fewer
MIN_FORECAST_WEEKS = 6


def demand_bookings(days=180):
    today = timezone.localdate()
    return (Booking.objects
            .filter(status__in=DEMAND_STATUSES, travel_date__gte=today - timedelta(days=days))
            .select_related('package', 'destination', 'schedule'))


def _share(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


def summary(days=180):
    bookings = list(demand_bookings(days))
    guests = sum(b.guests for b in bookings)
    return {
        'bookings': len(bookings),
        'guests': guests,
        'average_group': round(guests / len(bookings), 1) if bookings else 0,
        'revenue': float(sum(b.total_price for b in bookings if b.status in REVENUE_STATUSES)),
    }


def popular_tours(days=180, limit=5):
    rows = defaultdict(lambda: {'name': '', 'bookings': 0, 'guests': 0})
    for booking in demand_bookings(days):
        row = rows[booking.package_id]
        row['name'] = booking.package.title
        row['bookings'] += 1
        row['guests'] += booking.guests
    return sorted(rows.values(), key=lambda r: (-r['guests'], -r['bookings'], r['name']))[:limit]


def popular_destinations(days=180, limit=5):
    """Destinations the clients chose on their booking. Bookings that named none are not ranked."""
    rows = defaultdict(lambda: {'name': '', 'bookings': 0, 'guests': 0})
    for booking in demand_bookings(days):
        if booking.destination_id is None:
            continue
        row = rows[booking.destination_id]
        row['name'] = booking.destination.name
        row['bookings'] += 1
        row['guests'] += booking.guests
    return sorted(rows.values(), key=lambda r: (-r['guests'], -r['bookings'], r['name']))[:limit]


def weekday_pattern(days=180):
    totals = [0] * 7
    counts = [0] * 7
    for booking in demand_bookings(days):
        totals[booking.travel_date.weekday()] += booking.guests
        counts[booking.travel_date.weekday()] += 1
    all_guests = sum(totals)
    return [{'day': WEEKDAYS[i], 'bookings': counts[i], 'guests': totals[i], 'share': _share(totals[i], all_guests)}
            for i in range(7)]


def start_time_pattern(days=180):
    totals = defaultdict(int)
    for booking in demand_bookings(days):
        if booking.schedule_id:
            totals[booking.schedule.start_time.strftime('%H:%M')] += booking.guests
    all_guests = sum(totals.values())
    return [{'start': start, 'guests': guests, 'share': _share(guests, all_guests)}
            for start, guests in sorted(totals.items())]


def monthly_totals(days=180):
    totals = defaultdict(int)
    for booking in demand_bookings(days):
        totals[booking.travel_date.strftime('%Y-%m')] += booking.guests
    return sorted(totals.items())


def demand_periods(days=180):
    """Months with high or low demand, compared with the average month. Needs two months of bookings."""
    months = monthly_totals(days)
    if len(months) < 2:
        return {'note': 'Not enough bookings yet to compare months.', 'months': []}
    average = sum(guests for _, guests in months) / len(months)
    rows = []
    for month, guests in months:
        ratio = guests / average if average else 0
        if ratio >= HIGH_DEMAND:
            level = 'high'
        elif ratio <= LOW_DEMAND:
            level = 'low'
        else:
            level = 'normal'
        rows.append({'month': month, 'guests': guests, 'level': level, 'ratio': round(ratio, 2)})
    return {'note': '', 'average': round(average, 1), 'months': rows}


def _week_start(day):
    return day - timedelta(days=day.weekday())


def weekly_totals(weeks=12):
    """Guests per Monday-to-Sunday week, oldest first, for the last `weeks` complete weeks."""
    today = timezone.localdate()
    current_week = _week_start(today)
    first_week = current_week - timedelta(weeks=weeks)
    totals = defaultdict(int)
    for booking in (Booking.objects.filter(status__in=DEMAND_STATUSES,
                                           travel_date__gte=first_week, travel_date__lt=current_week)):
        totals[_week_start(booking.travel_date)] += booking.guests
    return [(first_week + timedelta(weeks=i), totals[first_week + timedelta(weeks=i)]) for i in range(weeks)]


def _least_squares(xs, ys):
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
    return slope, mean_y - slope * mean_x


def forecast(weeks_back=12, weeks_ahead=4):
    """Expected guests for the coming weeks, from a straight-line trend over recent weeks.

    It is an estimate from past bookings, not a promise. It needs at least MIN_FORECAST_WEEKS weeks of history.
    """
    history = weekly_totals(weeks_back)
    if sum(1 for _, guests in history if guests) < MIN_FORECAST_WEEKS:
        return {'method': f'Not enough history yet. A forecast needs at least {MIN_FORECAST_WEEKS} weeks with bookings.',
                'history': [], 'weeks': [], 'trend': 'unknown'}
    xs = list(range(len(history)))
    ys = [guests for _, guests in history]
    slope, intercept = _least_squares(xs, ys)
    start = history[-1][0] + timedelta(weeks=1)
    weeks = [{'week_start': start + timedelta(weeks=i),
              'guests': max(0, round(intercept + slope * (len(history) + i), 1))} for i in range(weeks_ahead)]
    mean = sum(ys) / len(ys)
    trend = 'steady'
    if mean and slope / mean > 0.03:
        trend = 'rising'
    elif mean and slope / mean < -0.03:
        trend = 'falling'
    return {
        'method': (f'Straight-line trend over the last {weeks_back} weeks of bookings. '
                   'An estimate from past demand, not a promise of future bookings.'),
        'history': [{'week_start': week, 'guests': guests} for week, guests in history],
        'weeks': weeks,
        'trend': trend,
    }


def recommend_schedules(limit=8, days=60):
    """Bookable dates ranked by how much demand the same weekday and start time have had.

    Only dates that the calendar reports as bookable are listed, and seats are the seats left right now.
    """
    weekday_share = {row['day']: row['share'] for row in weekday_pattern(180)}
    start_share = {row['start']: row['share'] for row in start_time_pattern(180)}
    low_seats = settings.LIMITED_SEATS_THRESHOLD
    candidates = []
    for package in TourPackage.objects.filter(is_active=True):
        for slot in upcoming_bookable_slots(package, limit=60, days=days):
            score = (weekday_share.get(WEEKDAYS[slot.date.weekday()], 0)
                     + start_share.get(slot.start_time.strftime('%H:%M'), 0))
            seats = effective_remaining(slot)
            note = 'Popular day and time, few seats left' if score >= 40 and seats <= low_seats else (
                'Popular day and time' if score >= 40 else 'Seats available')
            candidates.append({'tour': package.title, 'date': slot.date, 'start': slot.start_time.strftime('%H:%M'),
                               'seats_left': seats, 'score': round(score, 1), 'note': note})
    candidates.sort(key=lambda row: (-row['score'], row['date'], row['start']))
    return candidates[:limit]


def capacity_suggestions(days=180):
    """Start times that take a large share of demand but have no bookable date in the next 60 days.

    It only suggests: an administrator decides whether to open dates at that time.
    """
    popular = [row['start'] for row in start_time_pattern(days) if row['share'] >= 20]
    if not popular:
        return []
    open_times = set()
    for package in TourPackage.objects.filter(is_active=True):
        for slot in upcoming_bookable_slots(package, limit=200, days=60):
            open_times.add(slot.start_time.strftime('%H:%M'))
    return [start for start in popular if start not in open_times]
