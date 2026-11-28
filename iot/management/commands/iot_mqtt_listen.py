import logging

from django.core.management.base import BaseCommand, CommandError

from iot import mqtt, services

logger = logging.getLogger('iot.mqtt')


class Command(BaseCommand):
    help = ('Listen for sensor readings published over MQTT to tour/iot/<device-id>/readings. '
            'Needs the paho-mqtt package (pip install paho-mqtt) and a running broker.')

    def add_arguments(self, parser):
        parser.add_argument('--host', default='localhost', help='Broker host name.')
        parser.add_argument('--port', type=int, default=1883, help='Broker port (1883, or 8883 for TLS).')
        parser.add_argument('--username', default='', help='Broker username, if the broker requires one.')
        parser.add_argument('--password', default='', help='Broker password, if the broker requires one.')

    def handle(self, *args, **options):
        try:
            import paho.mqtt.client as mqtt_client
        except ImportError:
            raise CommandError('The MQTT listener needs the paho-mqtt package. Install it with: '
                               'pip install paho-mqtt') from None

        def on_connect(client, userdata, flags, reason_code, *extra):
            client.subscribe(f'{mqtt.TOPIC_PREFIX}/+/readings')
            self.stdout.write(f'Connected. Listening on {mqtt.TOPIC_PREFIX}/+/readings')

        def on_message(client, userdata, message):
            try:
                stored = mqtt.handle_message(message.topic, message.payload)
            except services.ReadingError as error:
                logger.warning('Refused a reading on %s: %s', message.topic, error)
            else:
                logger.info('Stored %s reading(s) from %s', stored, message.topic)

        try:
            client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt_client.Client()
        if options['username']:
            client.username_pw_set(options['username'], options['password'])
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(options['host'], options['port'], keepalive=60)
        self.stdout.write(f'Connecting to {options["host"]}:{options["port"]} (Ctrl+C to stop)')
        client.loop_forever()
