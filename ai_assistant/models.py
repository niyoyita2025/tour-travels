from django.conf import settings
from django.db import models
from django.utils import timezone

GREETING = ('Hello! I can help you find destinations and tours, check prices and open dates, '
            'and explain how booking works.')


class AssistantConfig(models.Model):
    """Settings for the travel assistant on the public website. There is exactly one row (pk=1)."""

    enabled = models.BooleanField('show the travel assistant', default=True,
                                  help_text='When off, the chat button is hidden from visitors.')
    greeting = models.TextField(default=GREETING, help_text='The first message a visitor sees.')
    use_llm = models.BooleanField(
        'let a language model phrase the answers', default=False,
        help_text='The model only rewords facts this website has already retrieved, and it is told not to add '
                  'any. It needs the ANTHROPIC_API_KEY setting on the server. Off by default.')
    model_name = models.CharField(max_length=80, default='claude-haiku-5-5',
                                  help_text='The language model used when the option above is on.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'travel assistant settings'

    def __str__(self):
        return 'Travel assistant settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AnalyticsReport(models.Model):
    """A saved report built from booking data. It holds totals and patterns only, never customer details."""

    title = models.CharField(max_length=160)
    period_days = models.PositiveSmallIntegerField()
    body = models.TextField()
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='analytics_reports')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title
