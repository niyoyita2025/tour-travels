"""Authenticating boards and limiting how fast they can send."""

import time

from django.conf import settings
from django.core.cache import cache

from .models import Device


def device_from_request(request):
    """The active device whose token is in `Authorization: Bearer <token>`, or None.

    A wrong, missing or revoked token gets the same answer, so the response never reveals which device IDs exist.
    """
    scheme, _, token = request.META.get('HTTP_AUTHORIZATION', '').partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token:
        return None
    return Device.objects.filter(token_hash=Device.hash_token(token), is_active=True).first()


def within_rate_limit(device):
    """A fixed one-minute allowance per device. Returns False once the device has used it up."""
    limit = getattr(settings, 'IOT_RATE_LIMIT_PER_MINUTE', 60)
    key = f'iot-rate:{device.pk}:{int(time.time() // 60)}'
    cache.add(key, 0, 70)
    try:
        count = cache.incr(key)
    except ValueError:  # the minute ended between the two calls
        cache.set(key, 1, 70)
        count = 1
    return count <= limit
