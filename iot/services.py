"""
The rules for sensor data. Every reading, from the HTTP API or the MQTT listener, passes through here:

* parse_payload() checks the data a device sent and refuses anything malformed or out of range.
* store_readings() saves the readings, marks the device as seen, and checks each threshold.
* check_devices() opens alerts for devices that stopped reporting and for measurements that stopped arriving.

An alert is opened once per problem. A reading that is still out of range updates the open alert, and a
reading back in range resolves it, so the same problem does not raise a new alert every few seconds.
"""

from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import metrics
from .models import Alert, Device, Reading, Threshold, offline_after


class ReadingError(ValueError):
    """The data was refused. The message is safe to send back to the device."""


def _setting(name, default):
    return getattr(settings, name, default)


def _timestamp(text, now):
    if text in (None, ''):
        return now
    if not isinstance(text, str):
        raise ReadingError('"timestamp" must be text such as 2026-11-28T15:00:00Z.')
    moment = parse_datetime(text)
    if moment is None:
        raise ReadingError('"timestamp" is not a valid date and time.')
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=dt_timezone.utc)
    if moment > now + timedelta(minutes=5):
        raise ReadingError('"timestamp" is in the future. Check the device clock.')
    if moment < now - timedelta(hours=_setting('IOT_MAX_PAST_HOURS', 24)):
        raise ReadingError('"timestamp" is too old to store.')
    return moment


def parse_payload(data, now=None):
    """Check a device's JSON body. Returns (recorded_at, [(metric, value), ...]).

    Both shapes are accepted:
        {"readings": {"temperature": 23.5, "humidity": 58}, "timestamp": "2026-11-28T15:00:00Z"}
        {"readings": [{"metric": "temperature", "value": 23.5}]}
    """
    now = now or timezone.now()
    if not isinstance(data, dict):
        raise ReadingError('The body must be a JSON object.')
    raw = data.get('readings')
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for item in raw:
            if not isinstance(item, dict):
                raise ReadingError('Each reading in the list must be an object with "metric" and "value".')
            pairs.append((item.get('metric'), item.get('value')))
    else:
        raise ReadingError('"readings" must be an object or a list.')
    if not pairs:
        raise ReadingError('No readings were sent.')
    limit = _setting('IOT_MAX_READINGS_PER_REQUEST', 20)
    if len(pairs) > limit:
        raise ReadingError(f'At most {limit} readings can be sent at once.')
    recorded_at = _timestamp(data.get('timestamp'), now)
    clean = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ReadingError('Each measurement needs a name.')
        try:
            clean[key] = metrics.validate_value(key, value)
        except ValueError as error:
            raise ReadingError(str(error)) from None
    return recorded_at, list(clean.items())


def _open_alert(device, metric, kind):
    return Alert.objects.filter(device=device, metric=metric, kind=kind, status=Alert.Status.OPEN).first()


def _open_or_refresh(device, metric, kind, message, now, value=None, limit=None):
    """Open the alert, or refresh the open one. Returns True only when a new alert was opened."""
    existing = _open_alert(device, metric, kind)
    if existing is not None:
        Alert.objects.filter(pk=existing.pk).update(message=message, value=value, limit=limit)
        return False
    try:
        with transaction.atomic():
            Alert.objects.create(device=device, metric=metric, kind=kind, message=message, value=value,
                                 limit=limit, opened_at=now)
    except IntegrityError:  # another request opened the same alert a moment ago
        return False
    return True


def _resolve(device, metric, kind, now):
    Alert.objects.filter(device=device, metric=metric, kind=kind, status=Alert.Status.OPEN).update(
        status=Alert.Status.RESOLVED, resolved_at=now)


def check_threshold(device, metric, value, now=None):
    """Compare one reading with the device's threshold for that measurement. Returns True if a new alert opened."""
    now = now or timezone.now()
    spec = metrics.METRICS[metric]
    threshold = Threshold.objects.filter(device=device, metric=metric, is_enabled=True).first()
    opened = False

    if threshold is not None and threshold.maximum is not None and value > threshold.maximum:
        message = (f'{device.name}: {spec.label.lower()} {value:g} {spec.unit} is above the maximum of '
                   f'{threshold.maximum:g} {spec.unit}.')
        opened = _open_or_refresh(device, metric, Alert.Kind.ABOVE, message, now, value, threshold.maximum)
    else:
        _resolve(device, metric, Alert.Kind.ABOVE, now)

    if threshold is not None and threshold.minimum is not None and value < threshold.minimum:
        message = (f'{device.name}: {spec.label.lower()} {value:g} {spec.unit} is below the minimum of '
                   f'{threshold.minimum:g} {spec.unit}.')
        opened = _open_or_refresh(device, metric, Alert.Kind.BELOW, message, now, value, threshold.minimum) or opened
    else:
        _resolve(device, metric, Alert.Kind.BELOW, now)
    return opened


def store_readings(device, recorded_at, pairs, now=None):
    """Save the readings, mark the device as seen, and check every threshold. Returns the number stored."""
    now = now or timezone.now()
    with transaction.atomic():
        Reading.objects.bulk_create([
            Reading(device=device, metric=key, value=value, recorded_at=recorded_at) for key, value in pairs
        ])
        Device.objects.filter(pk=device.pk).update(last_seen_at=now)
        device.last_seen_at = now
        # The device is talking again: its offline alert closes, and so do missing-data alerts for what it sent.
        _resolve(device, '', Alert.Kind.OFFLINE, now)
        for key, _ in pairs:
            _resolve(device, key, Alert.Kind.MISSING, now)
        for key, value in pairs:
            check_threshold(device, key, value, now)
    return len(pairs)


def check_devices(now=None):
    """Open alerts for devices that stopped reporting, and for expected measurements that stopped arriving.

    Safe to run often. Returns the number of new alerts opened.
    """
    now = now or timezone.now()
    opened = 0
    missing_after = timedelta(minutes=_setting('IOT_MISSING_MINUTES', 15))
    for device in Device.objects.filter(is_active=True):
        if device.last_seen_at is None:
            # A new device gets time to send its first reading before it is reported as silent.
            if now - device.created_at <= offline_after():
                continue
            message = f'{device.name} has never sent a reading.'
        elif now - device.last_seen_at > offline_after():
            message = f'{device.name} has not sent a reading since {timezone.localtime(device.last_seen_at):%d %b %H:%M}.'
        else:
            _resolve(device, '', Alert.Kind.OFFLINE, now)
            for key in device.expected_list:
                last = Reading.objects.filter(device=device, metric=key).order_by('-received_at').first()
                label = metrics.METRICS[key].label if key in metrics.METRICS else key
                if last is None or now - last.received_at > missing_after:
                    minutes = int(missing_after.total_seconds() // 60)
                    text = f'{device.name} has not reported {label.lower()} for {minutes} minutes.'
                    if _open_or_refresh(device, key, Alert.Kind.MISSING, text, now):
                        opened += 1
                else:
                    _resolve(device, key, Alert.Kind.MISSING, now)
            continue
        if _open_or_refresh(device, '', Alert.Kind.OFFLINE, message, now):
            opened += 1
    return opened


def acknowledge(alert, user, now=None):
    """An administrator has seen the alert. It stays open until the problem clears."""
    now = now or timezone.now()
    if alert.acknowledged_at is None:
        Alert.objects.filter(pk=alert.pk).update(acknowledged_at=now, acknowledged_by=user)
        alert.refresh_from_db()


def resolve_by_hand(alert, now=None):
    now = now or timezone.now()
    if alert.status == Alert.Status.OPEN:
        Alert.objects.filter(pk=alert.pk).update(status=Alert.Status.RESOLVED, resolved_at=now)
        alert.refresh_from_db()
