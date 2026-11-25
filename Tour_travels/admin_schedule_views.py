"""
Administrator pages for tour schedules (dates, times and capacity) and the booking calendar.
All views here are administrator-only.
"""

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications
from .calendar_service import build_month
from .forms import TourScheduleBulkForm, TourScheduleForm
from .models import Booking, TourPackage, TourSchedule
from .views import staff_required


@staff_required
def admin_schedules(request):
    schedules = TourSchedule.objects.select_related('package').annotate(
        booking_count=Count('bookings', filter=Q(bookings__status__in=['pending', 'confirmed'])),
    )
    tour = request.GET.get('tour', '')
    show = request.GET.get('show', 'upcoming')
    if tour.isdigit():
        schedules = schedules.filter(package_id=int(tour))
    if show == 'upcoming':
        schedules = schedules.filter(date__gte=timezone.localdate())
    elif show == 'past':
        schedules = schedules.filter(date__lt=timezone.localdate()).order_by('-date', 'start_time')
    return render(request, 'admin/schedules.html', {
        'schedules': schedules[:300],
        'tours': TourPackage.objects.order_by('title'),
        'tour': tour,
        'show': show,
    })


@staff_required
def admin_schedule_form(request, pk=None):
    schedule = get_object_or_404(TourSchedule, pk=pk) if pk is not None else None
    old = (schedule.date, schedule.start_time) if schedule else None
    if request.method == 'POST':
        form = TourScheduleForm(request.POST, instance=schedule)
        if form.is_valid():
            item = form.save()
            if old and old != (item.date, item.start_time):
                # Keep every booking's travel date in step with its slot.
                Booking.objects.filter(schedule=item).update(travel_date=item.date)
            verb = 'Updated' if schedule else 'Added'
            notifications.log_activity(request.user, 'schedule.saved',
                                       f'{verb} tour date: {item} (capacity {item.capacity})')
            messages.success(request, f'{verb} tour date: {item.date:%d %b %Y} {item.start_time:%H:%M}, '
                                      f'capacity {item.capacity}.')
            return redirect('admin_schedules')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = TourScheduleForm(instance=schedule)
    return render(request, 'admin/schedule_form.html', {'form': form, 'schedule': schedule})


@staff_required
def admin_schedule_bulk(request):
    if request.method == 'POST':
        form = TourScheduleBulkForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            wanted = {int(d) for d in data['weekdays']}
            created = skipped = 0
            day = data['start_date']
            while day <= data['end_date']:
                if day.weekday() in wanted:
                    _, was_created = TourSchedule.objects.get_or_create(
                        package=data['package'], date=day, start_time=data['start_time'],
                        defaults={'capacity': data['capacity'], 'is_open': data['is_open']},
                    )
                    created += was_created
                    skipped += not was_created
                day += timedelta(days=1)
            notifications.log_activity(request.user, 'schedule.bulk',
                                       f'Added {created} tour date(s) for {data["package"].title}')
            messages.success(request, f'Added {created} tour date(s).'
                                      + (f' {skipped} already existed and were left unchanged.' if skipped else ''))
            return redirect('admin_schedules')
    else:
        form = TourScheduleBulkForm(initial={'start_date': timezone.localdate()})
    return render(request, 'admin/schedule_bulk.html', {'form': form})


@staff_required
@require_POST
def admin_schedule_delete(request, pk):
    schedule = get_object_or_404(TourSchedule, pk=pk)
    label = str(schedule)
    try:
        schedule.delete()
    except ProtectedError:
        messages.error(request, f'"{label}" has bookings, so it cannot be deleted. '
                                'Close the slot instead so no new clients can book it.')
    else:
        notifications.log_activity(request.user, 'schedule.deleted', f'Deleted tour date: {label}')
        messages.success(request, f'Deleted "{label}".')
    return redirect('admin_schedules')


@staff_required
def admin_calendar(request):
    """Administrator month view: booked seats against capacity for every tour or one tour."""
    today = timezone.localdate()
    tour_id = request.GET.get('tour', '')
    package = None
    if tour_id.isdigit():
        package = TourPackage.objects.filter(pk=int(tour_id)).first()
        if package is None:
            raise Http404
    try:
        year, month = (int(part) for part in request.GET.get('month', '').split('-'))
        if not (1 <= month <= 12) or (year, month) < (today.year, today.month):
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    cal = build_month(year, month, package, today, with_bookings=True)
    selected_iso = request.GET.get('date', '')
    selected_day = next((d for w in cal['weeks'] for d in w if d['iso'] == selected_iso and d['in_month']), None)
    return render(request, 'admin/calendar.html', {
        'cal': cal,
        'tours': TourPackage.objects.order_by('title'),
        'tour': package,
        'selected_day': selected_day,
        'weekday_names': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'bulk_url': reverse('admin_schedule_bulk'),
    })
