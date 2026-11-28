import re

from django import forms
from django.core.exceptions import ValidationError

from Tour_travels.forms import StyledFormMixin

from .models import AssistantConfig


class AssistantConfigForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = AssistantConfig
        fields = ['enabled', 'greeting', 'use_llm', 'model_name']
        labels = {'enabled': 'Show the travel assistant to visitors'}

    def clean_model_name(self):
        name = (self.cleaned_data.get('model_name') or '').strip()
        if not re.fullmatch(r'[a-z0-9][a-z0-9.\-]{2,79}', name):
            raise ValidationError('Use the model name only, for example claude-haiku-5-5.')
        return name

    def clean_greeting(self):
        greeting = (self.cleaned_data.get('greeting') or '').strip()
        if not greeting:
            raise ValidationError('Write a greeting for visitors.')
        return greeting[:400]
