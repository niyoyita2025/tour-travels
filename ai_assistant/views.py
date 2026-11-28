"""
The travel assistant's public chat endpoint, and the administrator pages for analytics, reports and the
assistant's settings. The administrator pages use staff_required; the chat endpoint is open to visitors but
rate limited and never returns customer data.
"""

import json
import os

from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from Tour_travels import notifications
from Tour_travels.views import staff_required

from . import engine, reports
from .forms import AssistantConfigForm
from .models import AnalyticsReport, AssistantConfig

ASK_LIMIT_PER_MINUTE = 20
MAX_BODY_BYTES = 2048
WINDOWS = (90, 180, 365)


def _ask_allowance_left(request):
    minute = int(timezone.now().timestamp() // 60)
    key = f'assistant-ask:{request.META.get("REMOTE_ADDR", "")}:{minute}'
    cache.add(key, 0, 70)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 70)
        count = 1
    return count <= ASK_LIMIT_PER_MINUTE


@require_POST
def ask(request):
    config = AssistantConfig.load()
    if not config.enabled:
        return JsonResponse({'error': 'The travel assistant is turned off.'}, status=404)
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'Send the question as JSON.'}, status=415)
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({'error': 'That message is too long.'}, status=413)
    if not _ask_allowance_left(request):
        response = JsonResponse({'error': 'Please wait a moment before asking another question.'}, status=429)
        response['Retry-After'] = '60'
        return response
    try:
        data = json.loads(request.body or b'null')
    except ValueError:
        return JsonResponse({'error': 'The question was not understood.'}, status=400)
    message = data.get('message', '') if isinstance(data, dict) else ''
    reply = engine.respond(str(message))
    return JsonResponse({
        'answer': reply.text,
        'intent': reply.intent,
        'links': [{'label': label, 'url': url} for label, url in reply.links],
        'suggestions': reply.suggestions,
    })


def _window(request):
    try:
        days = int(request.GET.get('days', request.POST.get('days', 180)))
    except (TypeError, ValueError):
        days = 180
    return days if days in WINDOWS else 180


@staff_required
def analytics_page(request):
    days = _window(request)
    return render(request, 'ai_assistant/analytics.html', {
        'data': reports.build(days), 'days': days, 'windows': WINDOWS,
    })


@staff_required
def reports_page(request):
    if request.method == 'POST':
        days = _window(request)
        report = reports.create(request.user, days)
        notifications.log_activity(request.user, 'ai.report', f'Created {report.title}')
        messages.success(request, 'The report is ready. It contains totals and patterns only.')
        return redirect('ai_reports')
    return render(request, 'ai_assistant/reports.html', {
        'reports': AnalyticsReport.objects.all()[:30], 'windows': WINDOWS,
    })


@staff_required
def assistant_settings(request):
    config = AssistantConfig.load()
    if request.method == 'POST':
        form = AssistantConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'ai.assistant', 'Travel assistant settings changed')
            messages.success(request, 'Travel assistant settings saved.')
            return redirect('ai_assistant_settings')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = AssistantConfigForm(instance=config)
    return render(request, 'ai_assistant/assistant_settings.html', {
        'form': form,
        # Only whether the key is set is shown. The key itself is never displayed or stored in the database.
        'key_configured': bool(os.environ.get('ANTHROPIC_API_KEY', '').strip()),
    })
