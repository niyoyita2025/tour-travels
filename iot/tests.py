"""
Sensor monitoring: device authentication, validation, thresholds, offline and missing-data alerts, the MQTT
handler, and administrator-only pages. The sensor boards are simulated with plain requests.
"""

import json
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from Tour_travels.tests import DataMixin, PASSWORD

from . import metrics, mqtt, services
from .models import Alert, Device, Reading, Threshold


class MetricsTests(TestCase):
    def test_values_inside_the_sensor_range_are_stored_as_numbers(self):
        self.assertEqual(metrics.validate_value('temperature', 23), 23.0)

    def test_values_outside_the_sensor_range_are_refused(self):
        with self.assertRaisesMessage(ValueError, 'outside the range'):
            metrics.validate_value('humidity', 140)

    def test_booleans_text_and_unknown_measurements_are_refused(self):
        for bad in (True, '23.5', None):
            with self.assertRaises(ValueError):
                metrics.validate_value('temperature', bad)
        with self.assertRaisesMessage(ValueError, 'not a measurement'):
            metrics.validate_value('radiation', 1)

    def test_not_a_number_is_refused(self):
        with self.assertRaises(ValueError):
            metrics.validate_value('temperature', float('nan'))


class PayloadTests(TestCase):
    def test_accepts_the_object_and_the_list_shapes(self):
        _, pairs = services.parse_payload({'readings': {'temperature': 22, 'humidity': 50}})
        self.assertEqual(dict(pairs), {'temperature': 22.0, 'humidity': 50.0})
        _, pairs = services.parse_payload({'readings': [{'metric': 'co2', 'value': 600}]})
        self.assertEqual(pairs, [('co2', 600.0)])

    def test_refuses_empty_too_many_and_malformed_data(self):
        with self.assertRaises(services.ReadingError):
            services.parse_payload({'readings': {}})
        with self.assertRaises(services.ReadingError):
            services.parse_payload({'readings': {f'm{i}': 1 for i in range(21)}})
        with self.assertRaises(services.ReadingError):
            services.parse_payload({'readings': 'hot'})
        with self.assertRaises(services.ReadingError):
            services.parse_payload(['not', 'an', 'object'])

    def test_refuses_future_and_stale_timestamps(self):
        future = (timezone.now() + timedelta(hours=1)).isoformat()
        old = (timezone.now() - timedelta(days=3)).isoformat()
        with self.assertRaisesMessage(services.ReadingError, 'in the future'):
            services.parse_payload({'readings': {'temperature': 20}, 'timestamp': future})
        with self.assertRaisesMessage(services.ReadingError, 'too old'):
            services.parse_payload({'readings': {'temperature': 20}, 'timestamp': old})


class DeviceTokenTests(TestCase):
    def test_the_token_is_never_stored_only_its_keyed_hash(self):
        device = Device.objects.create(device_id='kitchen-esp32', name='Kitchen')
        token = device.issue_token()
        device.refresh_from_db()
        self.assertNotEqual(device.token_hash, token)
        self.assertNotIn(token, device.token_hash)
        self.assertTrue(device.check_token(token))
        self.assertFalse(device.check_token('guessed-token'))
        self.assertFalse(device.check_token(''))

    def test_a_device_without_a_token_accepts_nothing(self):
        device = Device.objects.create(device_id='no-token', name='No token')
        self.assertFalse(device.check_token(''))


class ReadingApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(device_id='kitchen-esp32', name='Kitchen', expected_metrics='temperature')
        self.token = self.device.issue_token()
        self.url = reverse('iot_readings_api')

    def post(self, body, token=None, content_type='application/json'):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}'} if token is not None else {}
        data = body if isinstance(body, str) else json.dumps(body)
        return self.client.post(self.url, data, content_type=content_type, **headers)

    def test_a_valid_token_stores_the_readings_and_marks_the_device_seen(self):
        response = self.post({'readings': {'temperature': 23.5, 'humidity': 58}}, token=self.token)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['stored'], 2)
        self.assertEqual(Reading.objects.filter(device=self.device).count(), 2)
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)
        self.assertTrue(self.device.is_online)

    def test_a_missing_wrong_or_revoked_token_gets_the_same_refusal(self):
        for token in (None, 'wrong-token'):
            response = self.post({'readings': {'temperature': 20}}, token=token)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['error'], 'The device token is missing or not valid.')
        self.device.is_active = False
        self.device.save()
        self.assertEqual(self.post({'readings': {'temperature': 20}}, token=self.token).status_code, 401)
        self.assertEqual(Reading.objects.count(), 0)

    def test_out_of_range_values_are_refused_with_the_reason(self):
        response = self.post({'readings': {'temperature': 900}}, token=self.token)
        self.assertEqual(response.status_code, 400)
        self.assertIn('outside the range', response.json()['error'])
        self.assertEqual(Reading.objects.count(), 0)

    def test_bodies_must_be_json_and_not_too_large(self):
        self.assertEqual(self.post('temperature=20', token=self.token, content_type='text/plain').status_code, 415)
        self.assertEqual(self.post('{not json', token=self.token).status_code, 400)
        big = {'readings': {'temperature': 20}, 'padding': 'x' * 9000}
        self.assertEqual(self.post(big, token=self.token).status_code, 413)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url, HTTP_AUTHORIZATION=f'Bearer {self.token}').status_code, 405)

    @override_settings(IOT_RATE_LIMIT_PER_MINUTE=2)
    def test_a_device_that_sends_too_much_is_slowed_down(self):
        body = {'readings': {'temperature': 20}}
        self.assertEqual(self.post(body, token=self.token).status_code, 201)
        self.assertEqual(self.post(body, token=self.token).status_code, 201)
        response = self.post(body, token=self.token)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '60')


class ThresholdAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(device_id='store-1', name='Store room', expected_metrics='temperature')
        self.token = self.device.issue_token()
        Threshold.objects.create(device=self.device, metric='temperature', minimum=10, maximum=30)

    def send(self, value):
        return services.store_readings(self.device, timezone.now(), [('temperature', float(value))])

    def open_alerts(self, kind):
        return Alert.objects.filter(device=self.device, kind=kind, status=Alert.Status.OPEN)

    def test_a_reading_above_the_maximum_opens_one_alert(self):
        self.send(31)
        self.assertEqual(self.open_alerts(Alert.Kind.ABOVE).count(), 1)
        self.assertIn('above the maximum of 30', self.open_alerts(Alert.Kind.ABOVE).get().message)

    def test_repeated_readings_update_the_alert_instead_of_repeating_it(self):
        self.send(31)
        self.send(33)
        self.send(34)
        alerts = self.open_alerts(Alert.Kind.ABOVE)
        self.assertEqual(alerts.count(), 1)
        self.assertEqual(alerts.get().value, 34)

    def test_back_in_range_closes_the_alert(self):
        self.send(31)
        self.send(25)
        self.assertEqual(self.open_alerts(Alert.Kind.ABOVE).count(), 0)
        self.assertEqual(Alert.objects.filter(kind=Alert.Kind.ABOVE, status=Alert.Status.RESOLVED).count(), 1)

    def test_a_reading_below_the_minimum_opens_a_below_alert(self):
        self.send(8)
        self.assertEqual(self.open_alerts(Alert.Kind.BELOW).count(), 1)

    def test_a_disabled_limit_raises_nothing(self):
        Threshold.objects.filter(device=self.device).update(is_enabled=False)
        self.send(99)
        self.assertEqual(Alert.objects.count(), 0)

    def test_threshold_needs_a_limit_and_a_valid_order(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            Threshold(device=self.device, metric='humidity').clean()
        with self.assertRaises(ValidationError):
            Threshold(device=self.device, metric='humidity', minimum=60, maximum=40).clean()


class DeviceHealthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(device_id='roof-1', name='Roof', expected_metrics='temperature,humidity')
        self.device.issue_token()

    def test_a_silent_device_is_reported_once_and_the_alert_clears_when_it_returns(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 20.0), ('humidity', 50.0)])
        later = timezone.now() + timedelta(minutes=30)
        self.assertEqual(services.check_devices(later), 1)
        self.assertEqual(services.check_devices(later), 0)  # not repeated
        self.assertEqual(Alert.objects.filter(kind=Alert.Kind.OFFLINE, status='open').count(), 1)
        services.store_readings(self.device, timezone.now(), [('temperature', 20.0), ('humidity', 50.0)])
        self.assertEqual(Alert.objects.filter(kind=Alert.Kind.OFFLINE, status='open').count(), 0)

    def test_a_measurement_that_stops_arriving_is_reported(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 20.0)])
        # The device is online, but humidity was never reported.
        opened = services.check_devices(timezone.now())
        self.assertEqual(opened, 1)
        self.assertEqual(Alert.objects.get(kind=Alert.Kind.MISSING).metric, 'humidity')
        services.store_readings(self.device, timezone.now(), [('humidity', 55.0)])
        self.assertEqual(Alert.objects.filter(kind=Alert.Kind.MISSING, status='open').count(), 0)

    def test_a_brand_new_device_gets_time_before_it_is_called_silent(self):
        fresh = Device.objects.create(device_id='fresh-1', name='Fresh')
        self.assertEqual(services.check_devices(timezone.now()), 0)
        self.assertFalse(Alert.objects.filter(device=fresh).exists())


class MqttHandlerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(device_id='garden-1', name='Garden', expected_metrics='')
        self.token = self.device.issue_token()

    def test_a_correct_token_on_the_right_topic_stores_the_readings(self):
        payload = json.dumps({'token': self.token, 'readings': {'pm25': 12.5}}).encode()
        self.assertEqual(mqtt.handle_message('tour/iot/garden-1/readings', payload), 1)
        self.assertEqual(Reading.objects.get().metric, 'pm25')

    def test_a_wrong_token_or_topic_is_refused(self):
        bad_token = json.dumps({'token': 'nope', 'readings': {'pm25': 12.5}}).encode()
        with self.assertRaises(services.ReadingError):
            mqtt.handle_message('tour/iot/garden-1/readings', bad_token)
        with self.assertRaises(services.ReadingError):
            mqtt.handle_message('other/topic', bad_token)
        self.assertEqual(Reading.objects.count(), 0)


class AdministratorPagesTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.device = Device.objects.create(device_id='lab-1', name='Lab', expected_metrics='temperature')
        self.token = self.device.issue_token()

    def test_anonymous_visitors_are_sent_to_sign_in_and_clients_are_refused(self):
        self.assertEqual(self.client.get(reverse('iot_monitor')).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('iot_monitor')).status_code, 403)
        self.assertEqual(self.client.get(reverse('iot_live')).status_code, 403)
        self.assertEqual(self.client.get(reverse('iot_history')).status_code, 403)

    def test_administrators_see_the_monitor_and_live_values_without_tokens(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 21.0)])
        self.client.force_login(self.staff)
        response = self.client.get(reverse('iot_monitor'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Lab')
        live = self.client.get(reverse('iot_live')).json()
        self.assertEqual(live['devices'][0]['values']['temperature']['value'], 21.0)
        self.assertNotIn(self.token, json.dumps(live))
        self.assertNotIn('token_hash', json.dumps(live))

    def test_the_new_device_token_is_shown_once_only(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('iot_device_token', args=[self.device.pk]))
        first = self.client.get(reverse('iot_token_show'))
        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(first['Cache-Control'], '')
        second = self.client.get(reverse('iot_token_show'))
        self.assertEqual(second.status_code, 302)  # the token is removed once displayed

    def test_a_device_with_readings_is_deactivated_rather_than_deleted(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 21.0)])
        self.client.force_login(self.staff)
        self.client.post(reverse('iot_device_delete', args=[self.device.pk]), follow=True)
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())

    def test_the_csv_report_lists_readings_for_the_period(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 21.0)])
        self.client.force_login(self.staff)
        response = self.client.get(reverse('iot_history'), {'format': 'csv', 'days': '7'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('lab-1,temperature,21.0', response.content.decode())

    def test_the_alert_feed_reports_only_new_alerts_after_the_last_id(self):
        Threshold.objects.create(device=self.device, metric='temperature', maximum=20)
        self.client.force_login(self.staff)
        baseline = self.client.get(reverse('iot_alerts_feed')).json()
        services.store_readings(self.device, timezone.now(), [('temperature', 25.0)])
        feed = self.client.get(reverse('iot_alerts_feed'), {'after': baseline['latest_id']}).json()
        self.assertEqual(len(feed['alerts']), 1)
        self.assertIn('above the maximum', feed['alerts'][0]['message'])

    def test_every_monitoring_page_loads_for_an_administrator(self):
        services.store_readings(self.device, timezone.now(), [('temperature', 21.0)])
        Threshold.objects.create(device=self.device, metric='temperature', maximum=30)
        self.client.force_login(self.staff)
        pages = [
            reverse('iot_monitor'), reverse('iot_devices'), reverse('iot_device_new'),
            reverse('iot_device_edit', args=[self.device.pk]), reverse('iot_thresholds', args=[self.device.pk]),
            reverse('iot_alerts'), reverse('iot_alerts') + '?state=all', reverse('iot_history'),
            reverse('iot_history') + '?days=30&metric=temperature',
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_threshold_page_saves_a_limit_for_the_device(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('iot_thresholds', args=[self.device.pk]),
                                    {'metric': 'temperature', 'minimum': '5', 'maximum': '30', 'is_enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        limit = Threshold.objects.get(device=self.device, metric='temperature')
        self.assertEqual((limit.minimum, limit.maximum), (5, 30))
