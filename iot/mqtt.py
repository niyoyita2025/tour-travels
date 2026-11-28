"""
MQTT ingestion. A board publishes to  tour/iot/<device-id>/readings  with the same JSON body as the HTTP
endpoint, plus its token:

    {"token": "<device token>", "readings": {"temperature": 23.5}, "timestamp": "..."}

The listener itself runs as the `iot_mqtt_listen` management command, which needs a broker and the
paho-mqtt package. The message handling lives here so it can be tested without a broker.
"""

import json

from . import services
from .models import Device

TOPIC_PREFIX = 'tour/iot'
MAX_PAYLOAD_BYTES = 8 * 1024


def handle_message(topic, payload):
    """Store one MQTT reading. Returns the number stored. Raises ReadingError when it is refused."""
    parts = topic.split('/')
    if len(parts) != 4 or f'{parts[0]}/{parts[1]}' != TOPIC_PREFIX or parts[3] != 'readings' or not parts[2]:
        raise services.ReadingError('The topic is not a sensor topic.')
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise services.ReadingError('The message is too large.')
    try:
        data = json.loads(payload)
    except ValueError:
        raise services.ReadingError('The message is not valid JSON.') from None
    if not isinstance(data, dict):
        raise services.ReadingError('The message must be a JSON object.')
    token = data.pop('token', '')
    device = Device.objects.filter(device_id=parts[2], is_active=True).first()
    if device is None or not device.check_token(token if isinstance(token, str) else ''):
        raise services.ReadingError('The device token is not valid.')
    recorded_at, pairs = services.parse_payload(data)
    return services.store_readings(device, recorded_at, pairs)
