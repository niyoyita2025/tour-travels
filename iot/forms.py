from django import forms
from django.core.exceptions import ValidationError

from Tour_travels.forms import StyledFormMixin

from . import metrics
from .models import Device, Threshold


class DeviceForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ['device_id', 'name', 'location', 'kind', 'expected_metrics', 'is_active']

    def clean_device_id(self):
        return (self.cleaned_data.get('device_id') or '').strip().lower()

    def clean_expected_metrics(self):
        keys = [key.strip() for key in (self.cleaned_data.get('expected_metrics') or '').split(',') if key.strip()]
        unknown = [key for key in keys if key not in metrics.METRICS]
        if unknown:
            raise ValidationError(f'Unknown measurement: {", ".join(unknown)}. '
                                  f'Choose from: {", ".join(metrics.METRICS)}.')
        return ','.join(dict.fromkeys(keys))


class ThresholdForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Threshold
        fields = ['metric', 'minimum', 'maximum', 'is_enabled']
        labels = {'minimum': 'Alert when below', 'maximum': 'Alert when above'}
