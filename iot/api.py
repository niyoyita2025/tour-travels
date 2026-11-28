"""
The ingestion endpoint a microcontroller posts its readings to.

    POST /api/iot/readings/
    Authorization: Bearer <device token>
    Content-Type: application/json

    {"readings": {"temperature": 23.5, "humidity": 58}, "timestamp": "2026-11-28T15:00:00Z"}

Answers: 201 when stored, 400 when the data is refused (with the reason), 401 when the token is wrong,
413 when the body is too large, 415 when it is not JSON, and 429 when the device has sent too much.
"""

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import services
from .auth import device_from_request, within_rate_limit

MAX_BODY_BYTES = 8 * 1024


@csrf_exempt  # devices authenticate with their token, not a browser session
@require_http_methods(['POST'])
def submit_readings(request):
    device = device_from_request(request)
    if device is None:
        return JsonResponse({'error': 'The device token is missing or not valid.'}, status=401)
    if not within_rate_limit(device):
        response = JsonResponse({'error': 'Too many readings. Wait a minute and try again.'}, status=429)
        response['Retry-After'] = '60'
        return response
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({'error': 'The request is too large.'}, status=413)
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'Send the readings as JSON (Content-Type: application/json).'}, status=415)
    try:
        data = json.loads(request.body or b'null')
    except ValueError:
        return JsonResponse({'error': 'The body is not valid JSON.'}, status=400)
    try:
        recorded_at, pairs = services.parse_payload(data)
    except services.ReadingError as error:
        return JsonResponse({'error': str(error)}, status=400)
    stored = services.store_readings(device, recorded_at, pairs)
    return JsonResponse({'stored': stored, 'device': device.device_id, 'recorded_at': recorded_at.isoformat()},
                        status=201)
