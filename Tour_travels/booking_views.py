"""
The booking flow: calendar → choose a date and time → sign in or register → booking form.

A visitor who is not signed in can browse the calendar and pick a slot. Their choice
is kept in the session ("booking intent"), so registering or signing in returns them
to the same tour, date and time. Availability is rechecked at every step, and again
inside the database transaction that saves the booking (see bookings.py).
"""

import re
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import notifications
from .bookings import BookingUnavailable, check_slot_open, create_booking, expire_stale_holds
from .calendar_service import availability_payload, build_month
from .destination_tours import top_up_dates
from .forms import BookingForm
from .models import Booking, ScheduleRule, TourPackage, TourSchedule, UserProfile

SUMMARY_FIELDS = ['schedule', 'guests', 'duration_hours', 'destination', 'full_name', 'email', 'phone', 'country',
                  'special_requests']
INTENT_KEY = 'booking_intent'
INTENT_MAX_AGE_SECONDS = 2 * 60 * 60  # a remembered choice is kept for two hours
MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')


# ---------- remembering the visitor's choice across sign-in ----------

def save_intent(request, schedule, guests):
    request.session[INTENT_KEY] = {
        'schedule': schedule.pk,
        'slug': schedule.package.slug,
        'guests': guests,
        'saved_at': timezone.now().timestamp(),
    }


def read_intent(request):
    data = request.session.get(INTENT_KEY)
    if not data:
        return None
    if timezone.now().timestamp() - data.get('saved_at', 0) > INTENT_MAX_AGE_SECONDS:
        clear_intent(request)
        return None
    return data


def clear_intent(request):
    request.session.pop(INTENT_KEY, None)


# ---------- helpers ----------

def _slot_for(package, schedule_id):
    """The slot the visitor chose, which must belong to this tour. Anything else is a 404."""
    try:
        pk = int(schedule_id)
    except (TypeError, ValueError):
        raise Http404
    return get_object_or_404(TourSchedule.objects.select_related('package'), pk=pk, package=package)


def _parse_guests(value, default=1):
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _group_size(request, package):
    """How many guests the visitor is booking for, kept within the tour's smallest and largest group."""
    guests = _parse_guests(request.GET.get('guests'), default=package.min_guests)
    return min(max(guests, package.min_guests), package.max_guests)


def _month_from_request(request):
    """Year and month to show: from ?date=, else ?month=, else this month. Never before this month."""
    today = timezone.localdate()
    selected = request.GET.get('date', '')
    try:
        day = datetime.strptime(selected, '%Y-%m-%d').date()
        return day.year, day.month
    except ValueError:
        pass
    match = MONTH_RE.match(request.GET.get('month', ''))
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and (year, month) >= (today.year, today.month):
            return year, month
    return today.year, today.month


# ---------- the calendar ----------

@require_GET
def tour_calendar(request, slug):
    package = get_object_or_404(TourPackage, slug=slug, is_active=True)
    expire_stale_holds()
    top_up_dates(package)  # automatic destination tours always have dates ahead
    year, month = _month_from_request(request)
    cal = build_month(year, month, package)

    guests = _group_size(request, package)
    selected_day = None
    selected_iso = request.GET.get('date', '')
    today = timezone.localdate()
    for week in cal['weeks']:
        for day in week:
            if day['iso'] == selected_iso and day['in_month'] and day['date'] >= today:
                selected_day = day

    suggested = None
    if selected_day is not None:
        # Mark the times that fit this group, and suggest the earliest of them.
        for slot in selected_day['slots']:
            slot['fits'] = slot['bookable'] and slot['remaining'] >= guests
            if slot.get('book_url'):
                slot['book_url'] += f'&guests={guests}'
            if slot['fits'] and suggested is None:
                suggested = slot

    return render(request, 'calendar.html', {
        'package': package,
        'cal': cal,
        'selected_day': selected_day,
        'guests': guests,
        'guest_options': range(package.min_guests, package.max_guests + 1),
        'suggested': suggested,
        'rule': ScheduleRule.objects.filter(package=package).first(),
        'availability_url': reverse('tour_availability', args=[package.slug]),
        'weekday_names': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'hold_hours': settings.BOOKING_HOLD_HOURS,
        'cancel_hours': settings.CANCELLATION_NOTICE_HOURS,
    })


@require_GET
def tour_availability(request, slug):
    """Live availability for the calendar. Refreshed by the page every few seconds."""
    package = get_object_or_404(TourPackage, slug=slug, is_active=True)
    expire_stale_holds()
    year, month = _month_from_request(request)
    return JsonResponse(availability_payload(year, month, package))


# ---------- choosing a slot, signing in, and the booking form ----------

def book_package(request, slug):
    package = get_object_or_404(TourPackage, slug=slug, is_active=True)
    expire_stale_holds()

    if request.method == 'POST':
        return _submit_booking(request, package)
    top_up_dates(package)

    schedule_id = request.GET.get('schedule')
    if not schedule_id:
        messages.info(request, 'Choose a date and time first.')
        return redirect('tour_calendar', slug=package.slug)

    schedule = _slot_for(package, schedule_id)
    try:
        check_slot_open(schedule)
    except BookingUnavailable as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('tour_calendar', args=[package.slug])}?date={schedule.date.isoformat()}")

    guests = _group_size(request, package)
    if not request.user.is_authenticated:
        save_intent(request, schedule, guests)
        return redirect('booking_start')

    intent = read_intent(request)
    if intent and intent.get('schedule') == schedule.pk and 'guests' not in request.GET:
        guests = min(max(_parse_guests(intent.get('guests')), package.min_guests), package.max_guests)
    guests = min(guests, max(schedule.seats_remaining, 1))

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    form = BookingForm(
        initial={
            'guests': guests,
            'full_name': request.user.get_full_name(),
            'email': request.user.email,
            'phone': profile.phone,
            'country': profile.country,
        },
        schedule=schedule,
    )
    return render(request, 'booking_form.html', {'form': form, 'package': package, 'schedule': schedule})


def _submit_booking(request, package):
    schedule = _slot_for(package, request.POST.get('schedule'))
    if not request.user.is_authenticated:
        save_intent(request, schedule, _parse_guests(request.POST.get('guests')))
        return redirect('booking_start')

    form = BookingForm(
        request.POST,
        instance=Booking(user=request.user, package=package, travel_date=schedule.date),
        schedule=schedule,
    )
    if form.is_valid() and request.POST.get('confirm') != '1':
        # Step 2: show the summary before anything is saved.
        return render(request, 'booking_summary.html', {
            'package': package,
            'schedule': schedule,
            'data': form.cleaned_data,
            'total': package.price * form.cleaned_data['guests'],
            'hidden': [(name, request.POST.get(name, '')) for name in SUMMARY_FIELDS],
        })
    if form.is_valid():
        try:
            booking = create_booking(
                user=request.user,
                schedule=schedule,
                guests=form.cleaned_data['guests'],
                duration_hours=form.cleaned_data['duration_hours'],
                destination=form.cleaned_data.get('destination'),
                full_name=form.cleaned_data['full_name'],
                email=form.cleaned_data['email'],
                phone=form.cleaned_data['phone'],
                country=form.cleaned_data['country'],
                special_requests=form.cleaned_data['special_requests'],
            )
        except BookingUnavailable as exc:
            form.add_error(None, str(exc))
        else:
            clear_intent(request)
            if notifications.booking_received(booking):
                messages.success(request, f'Booking request {booking.reference} received. '
                                          f'A confirmation email was sent to {booking.email}.')
            else:
                # The booking is saved either way. Only claim an email when the mail server accepted it.
                messages.warning(request, f'Booking request {booking.reference} received. We could not send '
                                          'the confirmation email right now, but your booking is saved and '
                                          'shown below.')
            return redirect('booking_detail', reference=booking.reference)
    return render(request, 'booking_form.html', {'form': form, 'package': package, 'schedule': schedule})


def booking_start(request):
    """Shown to a visitor who picked a slot but is not signed in: create an account or sign in."""
    intent = read_intent(request)
    if not intent:
        return redirect('packages')
    if request.user.is_authenticated:
        return redirect('booking_resume')
    schedule = TourSchedule.objects.select_related('package').filter(pk=intent['schedule']).first()
    if schedule is None or not schedule.is_bookable:
        clear_intent(request)
        messages.error(request, 'That date is no longer available. Please choose another.')
        return redirect('packages')
    return render(request, 'booking_start.html', {
        'schedule': schedule,
        'package': schedule.package,
        'guests': min(intent['guests'], max(schedule.seats_remaining, 1)),
    })


@login_required
def booking_resume(request):
    """
    After registering or signing in, return to the chosen tour and date.
    Availability is checked again, because the slot may have filled meanwhile.
    """
    intent = read_intent(request)
    if not intent:
        messages.info(request, 'Your previous selection has expired. Please choose a date again.')
        return redirect('packages')
    schedule = TourSchedule.objects.select_related('package').filter(pk=intent['schedule']).first()
    if schedule is None:
        clear_intent(request)
        return redirect('packages')
    calendar_url = f"{reverse('tour_calendar', args=[schedule.package.slug])}?date={schedule.date.isoformat()}"
    try:
        check_slot_open(schedule)
    except BookingUnavailable as exc:
        clear_intent(request)
        messages.error(request, f'{exc} Your other details are safe; choose another date to continue.')
        return redirect(calendar_url)
    if schedule.seats_remaining == 0:
        clear_intent(request)
        messages.error(request, 'This date was fully booked while you were signing in. Please choose another.')
        return redirect(calendar_url)
    guests = min(_parse_guests(intent.get('guests')), schedule.seats_remaining)
    messages.info(request, f'Welcome back. Your selection is restored: {schedule.date:%d %B %Y} '
                           f'at {schedule.start_time:%H:%M}.')
    return redirect(f"{reverse('book_package', args=[schedule.package.slug])}"
                    f"?schedule={schedule.pk}&guests={guests}")
