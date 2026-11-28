"""
IoT data: devices (microcontroller boards), the readings they send, thresholds set by administrators,
and the alerts raised when a reading breaks a threshold or a device stops reporting.

Device tokens are never stored. Only a keyed hash of each token is kept, so a copy of the database
cannot be used to send readings as a device.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from . import metrics

TOKEN_SALT = 'iot.device-token'


def offline_after():
    """A device that has sent nothing for this long is shown as offline."""
    return timedelta(minutes=getattr(settings, 'IOT_OFFLINE_MINUTES', 10))


class Device(models.Model):
    class Kind(models.TextChoices):
        ESP32 = 'esp32', 'ESP32'
        ESP8266 = 'esp8266', 'ESP8266'
        OTHER = 'other', 'Other compatible board'

    device_id = models.SlugField(
        'device ID', max_length=40, unique=True,
        validators=[RegexValidator(r'^[a-z0-9][a-z0-9-]*$', 'Use lowercase letters, digits and dashes only.')],
        help_text='The name the device sends with its readings, for example kitchen-esp32.')
    name = models.CharField(max_length=120)
    location = models.CharField(max_length=160, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ESP32)
    expected_metrics = models.CharField(
        max_length=200, blank=True,
        help_text='Comma-separated measurements this device must report, for example temperature,humidity. '
                  'A measurement that stops arriving raises an alert.')
    is_active = models.BooleanField('active', default=True,
                                    help_text='Untick to refuse this device\'s readings without deleting its history.')
    token_hash = models.CharField(max_length=64, blank=True, editable=False)
    token_issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @staticmethod
    def hash_token(token):
        return salted_hmac(TOKEN_SALT, token, algorithm='sha256').hexdigest()

    def issue_token(self):
        """Create a new token for this device. Returns the token once; only its hash is saved."""
        token = secrets.token_urlsafe(32)
        self.token_hash = self.hash_token(token)
        self.token_issued_at = timezone.now()
        self.save(update_fields=['token_hash', 'token_issued_at'])
        return token

    def check_token(self, token):
        return bool(self.token_hash) and constant_time_compare(self.token_hash, self.hash_token(token or ''))

    @property
    def expected_list(self):
        return [item.strip() for item in self.expected_metrics.split(',') if item.strip()]

    @property
    def is_online(self):
        return (self.is_active and self.last_seen_at is not None
                and timezone.now() - self.last_seen_at <= offline_after())

    @property
    def status(self):
        if not self.is_active:
            return 'inactive'
        if self.last_seen_at is None:
            return 'never connected'
        return 'online' if self.is_online else 'offline'


class Reading(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='readings')
    metric = models.CharField(max_length=20, choices=metrics.choices())
    value = models.FloatField()
    recorded_at = models.DateTimeField(help_text='When the sensor took the measurement (device clock).')
    received_at = models.DateTimeField(auto_now_add=True, help_text='When the website received it.')

    class Meta:
        ordering = ['-recorded_at']
        indexes = [models.Index(fields=['device', 'metric', '-recorded_at'])]

    def __str__(self):
        return f'{self.device}: {self.metric} {self.value:g}'


class Threshold(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='thresholds')
    metric = models.CharField(max_length=20, choices=metrics.choices())
    minimum = models.FloatField('alert below', null=True, blank=True)
    maximum = models.FloatField('alert above', null=True, blank=True)
    is_enabled = models.BooleanField('enabled', default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['device__name', 'metric']
        constraints = [
            models.UniqueConstraint(fields=['device', 'metric'], name='one_threshold_per_metric'),
        ]

    def __str__(self):
        return f'{self.device}: {self.get_metric_display()}'

    def clean(self):
        super().clean()
        if self.minimum is None and self.maximum is None:
            raise ValidationError('Set a minimum, a maximum, or both.')
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValidationError('The minimum must be lower than the maximum.')


class Alert(models.Model):
    class Kind(models.TextChoices):
        ABOVE = 'above_max', 'Above the maximum'
        BELOW = 'below_min', 'Below the minimum'
        OFFLINE = 'offline', 'Device offline'
        MISSING = 'missing_data', 'Readings missing'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESOLVED = 'resolved', 'Resolved'

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='alerts')
    metric = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    value = models.FloatField(null=True, blank=True)
    limit = models.FloatField(null=True, blank=True)
    message = models.CharField(max_length=255)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='acknowledged_alerts')

    class Meta:
        ordering = ['-opened_at']
        constraints = [
            # Only one open alert per device, measurement and kind: a repeated reading updates it instead.
            models.UniqueConstraint(fields=['device', 'metric', 'kind'], condition=Q(status='open'),
                                    name='one_open_alert_per_condition'),
        ]

    def __str__(self):
        return self.message
