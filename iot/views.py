"""
Administrator pages for sensor monitoring. Every view is staff-only (Tour_travels.views.staff_required).
Configuration changes are POST requests with a CSRF token, and each one is written to the activity log.
"""

import csv
from datetime import timedelta

from django.contrib import messages
from django.db.models import Avg, Count, Max, Min
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from Tour_travels import notifications
from Tour_travels.views import staff_required

from . import metrics, services
from .forms import DeviceForm, ThresholdForm
from .models import Alert, Device, Reading, Threshold

STATUS_LABELS = {'online': 'Online', 'offline': 'Offline', 'never connected': 'Never connected', 'inactive': 'Inactive'}
SESSION_TOKEN = 'iot_new_token'
HISTORY_WINDOWS = {'1': 1, '7': 7, '30': 30}


def _device_snapshot(device):
    values = {}
    for key in Reading.objects.filter(device=device).values_list('metric', flat=True).distinct():
        reading = Reading.objects.filter(device=device, metric=key).order_by('-received_at').first()
        spec = metrics.METRICS.get(key)
        values[key] = {
            'label': spec.label if spec else key,
            'unit': spec.unit if spec else '',
            'value': round(reading.value, 2),
            'at': timezone.localtime(reading.received_at).strftime('%H:%M:%S'),
        }
    last = timezone.localtime(device.last_seen_at).strftime('%d %b %H:%M') if device.last_seen_at else 'never'
    return {
        'id': device.pk, 'device_id': device.device_id, 'name': device.name, 'location': device.location,
        'status': device.status, 'status_label': STATUS_LABELS.get(device.status, device.status),
        'last_seen': last, 'values': values,
    }


def _alert_row(alert):
    return {'id': alert.pk, 'message': alert.message, 'kind': alert.get_kind_display(),
            'opened': timezone.localtime(alert.opened_at).strftime('%d %b %H:%M:%S')}


@staff_required
def monitor(request):
    services.check_devices()
    devices = [_device_snapshot(device) for device in Device.objects.all()]
    since = timezone.now() - timedelta(hours=24)
    context = {
        'devices': devices,
        'online_count': sum(1 for d in devices if d['status'] == 'online'),
        'offline_count': sum(1 for d in devices if d['status'] in ('offline', 'never connected')),
        'open_alerts': Alert.objects.filter(status=Alert.Status.OPEN).select_related('device')[:10],
        'open_count': Alert.objects.filter(status=Alert.Status.OPEN).count(),
        'readings_today': Reading.objects.filter(received_at__gte=since).count(),
    }
    return render(request, 'iot/monitor.html', context)


@staff_required
def live_json(request):
    """Live values for the monitor page, refreshed every few seconds. Contains no tokens."""
    services.check_devices()
    return JsonResponse({'devices': [_device_snapshot(d) for d in Device.objects.all()],
                         'open_count': Alert.objects.filter(status=Alert.Status.OPEN).count()})


@staff_required
def alerts_feed(request):
    """Alerts for the toast notifications on every admin page. Without `after`, it only reports the newest id."""
    services.check_devices()
    latest = Alert.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
    if 'after' not in request.GET:
        return JsonResponse({'latest_id': latest, 'open_count': Alert.objects.filter(
            status=Alert.Status.OPEN).count(), 'alerts': []})
    try:
        after = int(request.GET['after'])
    except ValueError:
        after = latest
    new = Alert.objects.filter(pk__gt=after, status=Alert.Status.OPEN).select_related('device')[:20]
    return JsonResponse({'latest_id': latest,
                         'open_count': Alert.objects.filter(status=Alert.Status.OPEN).count(),
                         'alerts': [_alert_row(alert) for alert in new]})


# ---------- devices ----------

@staff_required
def devices(request):
    rows = []
    for device in Device.objects.all():
        rows.append({'device': device, 'readings': device.readings.count(),
                     'status_label': STATUS_LABELS.get(device.status, device.status)})
    return render(request, 'iot/devices.html', {'rows': rows})


@staff_required
def device_form(request, pk=None):
    device = get_object_or_404(Device, pk=pk) if pk is not None else None
    if request.method == 'POST':
        form = DeviceForm(request.POST, instance=device)
        if form.is_valid():
            device = form.save()
            verb = 'Updated' if pk else 'Registered'
            notifications.log_activity(request.user, f'iot.device.{verb.lower()}', f'{verb} device {device.device_id}')
            if pk is None:
                token = device.issue_token()
                request.session[SESSION_TOKEN] = {'device': device.pk, 'token': token}
                return redirect('iot_token_show')
            messages.success(request, f'Saved {device.name}.')
            return redirect('iot_devices')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = DeviceForm(instance=device)
    return render(request, 'iot/device_form.html', {'form': form, 'device': device})


@staff_required
@require_POST
def device_token_regenerate(request, pk):
    device = get_object_or_404(Device, pk=pk)
    token = device.issue_token()
    notifications.log_activity(request.user, 'iot.device.token', f'New token issued for {device.device_id}')
    request.session[SESSION_TOKEN] = {'device': device.pk, 'token': token}
    return redirect('iot_token_show')


@staff_required
def device_token_show(request):
    """Shows a new device token exactly once. The token is removed from the session when it is displayed."""
    issued = request.session.pop(SESSION_TOKEN, None)
    device = Device.objects.filter(pk=issued['device']).first() if issued else None
    if device is None:
        messages.info(request, 'A device token can only be shown once, right after it is created or replaced. '
                               'To get a new one, use "Replace token".')
        return redirect('iot_devices')
    response = render(request, 'iot/device_token.html', {'device': device, 'token': issued['token'],
                                                         'api_path': '/api/iot/readings/'})
    response['Cache-Control'] = 'no-store'
    return response


@staff_required
@require_POST
def device_toggle(request, pk):
    device = get_object_or_404(Device, pk=pk)
    device.is_active = not device.is_active
    device.save(update_fields=['is_active'])
    state = 'activated' if device.is_active else 'deactivated'
    notifications.log_activity(request.user, f'iot.device.{state}', f'{state.capitalize()} device {device.device_id}')
    messages.success(request, f'{device.name} is {"active" if device.is_active else "inactive"}. '
                              + ('It accepts readings again.' if device.is_active else 'Its readings are refused.'))
    return redirect('iot_devices')


@staff_required
@require_POST
def device_delete(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if device.readings.exists():
        messages.error(request, f'{device.name} has stored readings, so it cannot be deleted. '
                                'Deactivate it instead to keep its history.')
    else:
        name = device.name
        device.delete()
        notifications.log_activity(request.user, 'iot.device.deleted', f'Deleted device {name}')
        messages.success(request, f'Deleted {name}.')
    return redirect('iot_devices')


# ---------- thresholds ----------

@staff_required
def thresholds(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if request.method == 'POST':
        metric = request.POST.get('metric', '')
        existing = Threshold.objects.filter(device=device, metric=metric).first()
        form = ThresholdForm(request.POST, instance=existing or Threshold(device=device))
        if form.is_valid():
            item = form.save(commit=False)
            item.device = device
            item.save()
            notifications.log_activity(request.user, 'iot.threshold.saved',
                                       f'Threshold for {item.get_metric_display()} on {device.device_id}')
            messages.success(request, f'Saved the {item.get_metric_display().lower()} alert for {device.name}.')
            return redirect('iot_thresholds', pk=device.pk)
        messages.error(request, ' '.join(e for errors in form.errors.values() for e in errors) or 'Check the values.')
    else:
        form = ThresholdForm(initial={'is_enabled': True})
    return render(request, 'iot/thresholds.html', {
        'device': device,
        'thresholds': device.thresholds.all(),
        'form': form,
        'metric_units': {key: spec.unit for key, spec in metrics.METRICS.items()},
    })


@staff_required
@require_POST
def threshold_delete(request, pk):
    item = get_object_or_404(Threshold, pk=pk)
    device_pk = item.device_id
    notifications.log_activity(request.user, 'iot.threshold.deleted', f'Removed {item}')
    item.delete()
    messages.success(request, 'The alert limit was removed.')
    return redirect('iot_thresholds', pk=device_pk)


# ---------- alerts ----------

@staff_required
def alerts(request):
    services.check_devices()
    state = request.GET.get('state', 'open')
    items = Alert.objects.select_related('device', 'acknowledged_by')
    if state in ('open', 'resolved'):
        items = items.filter(status=state)
    return render(request, 'iot/alerts.html', {'alerts': items[:200], 'state': state})


@staff_required
@require_POST
def alert_action(request, pk, action):
    alert = get_object_or_404(Alert, pk=pk)
    if action == 'acknowledge':
        services.acknowledge(alert, request.user)
        messages.success(request, 'Alert acknowledged. It stays open until the reading is back in range.')
    elif action == 'resolve':
        services.resolve_by_hand(alert)
        notifications.log_activity(request.user, 'iot.alert.resolved', f'Alert closed by hand: {alert.message}')
        messages.success(request, 'Alert closed.')
    else:
        raise Http404
    return redirect('iot_alerts')


# ---------- history and reports ----------

def _sparkline(readings):
    """Points for an SVG line, oldest first. Empty when there is nothing to draw."""
    values = [reading.value for reading in readings]
    if len(values) < 2:
        return ''
    low, high = min(values), max(values)
    span = (high - low) or 1
    width, height = 640, 160
    step = width / (len(values) - 1)
    return ' '.join(f'{i * step:.1f},{height - (v - low) / span * (height - 10) - 5:.1f}'
                    for i, v in enumerate(values))


@staff_required
def history(request):
    device_pk = request.GET.get('device', '')
    metric = request.GET.get('metric', '')
    days = HISTORY_WINDOWS.get(request.GET.get('days', '7'), 7)
    since = timezone.now() - timedelta(days=days)
    readings = Reading.objects.select_related('device').filter(recorded_at__gte=since)
    if device_pk.isdigit():
        readings = readings.filter(device_id=int(device_pk))
    if metric in metrics.METRICS:
        readings = readings.filter(metric=metric)
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="sensor-readings-{days}-days.csv"'
        writer = csv.writer(response)
        writer.writerow(['device', 'measurement', 'value', 'unit', 'measured_at', 'received_at'])
        for reading in readings.order_by('recorded_at')[:100_000]:
            spec = metrics.METRICS.get(reading.metric)
            writer.writerow([reading.device.device_id, reading.metric, round(reading.value, 3),
                             spec.unit if spec else '', timezone.localtime(reading.recorded_at).isoformat(),
                             timezone.localtime(reading.received_at).isoformat()])
        return response
    recent = list(readings.order_by('-recorded_at')[:200])
    chronological = list(reversed(recent))
    summary = readings.aggregate(count=Count('id'), low=Min('value'), high=Max('value'), average=Avg('value'))
    return render(request, 'iot/history.html', {
        'devices': Device.objects.all(), 'metrics': metrics.METRICS.values(), 'device_pk': device_pk,
        'metric': metric, 'days': days, 'windows': HISTORY_WINDOWS, 'readings': recent,
        'summary': summary, 'points': _sparkline(chronological),
        'chart_metric': metrics.METRICS.get(metric),
    })
