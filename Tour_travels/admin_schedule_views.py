"""
Administrator pages for tour schedules (dates, times and capacity) and the booking calendar.
All views here are administrator-only.
"""

from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications, scheduling
from .calendar_service import build_month
from .forms import ScheduleRuleForm, TourScheduleBulkForm, TourScheduleForm
from .models import Booking, ScheduleRule, TourPackage, TourSchedule
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
                # Keep every booking's travel date in step with its slot, and tell each customer.
                Booking.objects.filter(schedule=item).update(travel_date=item.date)
                was = f'{old[0]:%d %b %Y} at {old[1]:%H:%M}'
                affected = Booking.objects.filter(
                    schedule=item, status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED],
                ).select_related('package', 'schedule')
                for booking in affected:
                    notifications.schedule_changed(booking, was, actor=request.user)
                if affected:
                    messages.info(request, f'{len(affected)} customer(s) on this time were emailed about the change.')
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
def admin_scheduling(request):
    """Every tour, with its automatic timetable where it has one."""
    rules = {rule.package_id: rule for rule in ScheduleRule.objects.all()}
    rows = [{'package': package, 'rule': rules.get(package.pk)}
            for package in TourPackage.objects.order_by('title')]
    return render(request, 'admin/scheduling.html', {'rows': rows})


@staff_required
def admin_scheduling_edit(request, package_id):
    """Set a tour's timetable: start times, interval, tour length, operating days, capacity and group sizes."""
    package = get_object_or_404(TourPackage, pk=package_id)
    rule = ScheduleRule.objects.filter(package=package).first() or ScheduleRule(package=package)
    if request.method == 'POST':
        form = ScheduleRuleForm(request.POST, instance=rule, package=package)
        if form.is_valid():
            with transaction.atomic():
                rule = form.save()
                added, removed = scheduling.sync_rule(package)
            notifications.log_activity(
                request.user, 'schedule.rule',
                f'Saved the timetable for {package.title}: {len(rule.start_times())} start times a day, '
                f'{added} opened, {removed} removed')
            messages.success(request, f'Timetable saved for "{package.title}". {added} new start time(s) opened'
                                      f' and {removed} unbooked time(s) that no longer fit were removed.')
            return redirect('admin_scheduling')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = ScheduleRuleForm(instance=rule, package=package)
    saved = rule.pk is not None
    return render(request, 'admin/scheduling_form.html', {
        'form': form,
        'package': package,
        'rule': rule if saved else None,
        'times': rule.start_times() if saved else [],
    })


@staff_required
@require_POST
def admin_scheduling_off(request, package_id):
    """Stop automatic dates for a tour. Booked times are kept; unbooked future times are removed."""
    package = get_object_or_404(TourPackage, pk=package_id)
    with transaction.atomic():
        removed = scheduling.turn_off(package)
    notifications.log_activity(request.user, 'schedule.rule_off', f'Stopped the automatic timetable for {package.title}')
    messages.success(request, f'Automatic dates stopped for "{package.title}". {removed} unbooked time(s) removed; '
                              'booked times were kept.')
    return redirect('admin_scheduling')


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
