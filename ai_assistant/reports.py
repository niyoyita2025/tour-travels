"""
Administrator reports built from booking analytics. The text is written by fixed rules from the numbers,
so every statement can be traced to the data. It contains totals and patterns only, never customer names,
e-mail addresses or phone numbers.
"""

import json
from datetime import date

from django.utils import timezone

from . import analytics
from .models import AnalyticsReport

FOOTER = ('Generated from booking records by fixed rules. Contains totals and patterns only, '
          'with no customer names or contact details.')


def build(days=180):
    """Collect every analysis for the period. Returns the dictionary the report and the page both use."""
    return {
        'period_days': days,
        'summary': analytics.summary(days),
        'popular_tours': analytics.popular_tours(days),
        'popular_destinations': analytics.popular_destinations(days),
        'weekdays': analytics.weekday_pattern(days),
        'start_times': analytics.start_time_pattern(days),
        'periods': analytics.demand_periods(days),
        'forecast': analytics.forecast(),
        'recommendations': analytics.recommend_schedules(),
        'capacity': analytics.capacity_suggestions(days),
    }


def _money(value):
    return f'USD {value:,.2f}'


def narrate(data):
    """Plain-language report text from the numbers in `data`."""
    days = data['period_days']
    summary = data['summary']
    lines = [f'Booking report for the last {days} days.', '']

    if not summary['bookings']:
        lines.append('There are no bookings in this period yet, so there are no patterns to report.')
    else:
        lines.append(f'{summary["bookings"]} booking(s) for {summary["guests"]} guest(s). '
                     f'The average group has {summary["average_group"]} guest(s).')
        if summary['revenue']:
            lines.append(f'Confirmed and completed bookings total {_money(summary["revenue"])}.')
        if data['popular_tours']:
            top = data['popular_tours'][0]
            lines.append(f'Most booked tour: {top["name"]} ({top["guests"]} guests in {top["bookings"]} bookings).')
        if data['popular_destinations']:
            top = data['popular_destinations'][0]
            lines.append(f'Most chosen destination: {top["name"]} ({top["guests"]} guests).')
        busiest = max(data['weekdays'], key=lambda row: row['guests'])
        if busiest['guests']:
            lines.append(f'Busiest weekday: {busiest["day"]} ({busiest["share"]}% of guests).')
        if data['start_times']:
            top_start = max(data['start_times'], key=lambda row: row['guests'])
            lines.append(f'Most popular start time: {top_start["start"]} ({top_start["share"]}% of guests).')

    periods = data['periods']
    if periods.get('months'):
        high = [row['month'] for row in periods['months'] if row['level'] == 'high']
        low = [row['month'] for row in periods['months'] if row['level'] == 'low']
        lines.append('High-demand months: ' + (', '.join(high) if high else 'none'))
        lines.append('Low-demand months: ' + (', '.join(low) if low else 'none'))
    elif periods.get('note'):
        lines.append(periods['note'])

    forecast = data['forecast']
    if forecast['weeks']:
        expected = ', '.join(f'week of {row["week_start"]:%d %b}: about {row["guests"]:g} guests'
                             for row in forecast['weeks'])
        lines.append(f'Demand is {forecast["trend"]}. Estimate: {expected}.')
        lines.append(forecast['method'])
    else:
        lines.append(forecast['method'])

    if data['capacity']:
        lines.append('Popular start times with no bookable date in the next 60 days: '
                     + ', '.join(data['capacity']) + '. Consider opening dates at these times.')

    if data['recommendations']:
        lines.append('Bookable dates with the strongest demand (seats are the seats left right now):')
        for row in data['recommendations'][:5]:
            lines.append(f'- {row["tour"]}: {row["date"]:%a %d %b} at {row["start"]}, {row["seats_left"]} seat(s) '
                         f'left. {row["note"]}.')

    lines.extend(['', FOOTER])
    return '\n'.join(lines)


def _jsonable(value):
    return json.loads(json.dumps(value, default=lambda o: o.isoformat() if isinstance(o, date) else str(o)))


def create(user, days=180):
    """Build, narrate and save a report. Returns the saved AnalyticsReport."""
    data = build(days)
    body = narrate(data)
    title = f'Booking report, last {days} days, {timezone.localdate():%d %b %Y}'
    return AnalyticsReport.objects.create(title=title, period_days=days, body=body, data=_jsonable(data),
                                          created_by=user)
