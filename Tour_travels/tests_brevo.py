"""
Brevo email sending. The HTTP call to Brevo is replaced by a fake response, so no email leaves the test run
and no real key is used.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from . import notifications
from .models import EmailLog
from .tests import DataMixin

FAKE_KEY = 'xkeysib-test-0000-not-a-real-key'
BREVO = {
    'EMAIL_BACKEND': 'anymail.backends.brevo.EmailBackend',
    'ANYMAIL': {'BREVO_API_KEY': FAKE_KEY},
    'DEFAULT_FROM_EMAIL': 'Explore Rwanda <niyoyitarobert1@gmail.com>',
    'EMAIL_PROVIDER': 'brevo',
    'BREVO_API_KEY': FAKE_KEY,
}


def brevo_response(status=201, body=None):
    response = mock.Mock(status_code=status, reason='OK' if status < 300 else 'Error', headers={},
                         encoding='utf-8')
    payload = body if body is not None else {'messageId': '<202611281234.1@smtp-relay.mailin.fr>'}
    response.json.return_value = payload
    response.text = json.dumps(payload)
    response.content = response.text.encode()
    response.raise_for_status = mock.Mock()
    return response


@override_settings(**BREVO)
class BrevoSendingTests(DataMixin, TestCase):
    def test_a_booking_email_goes_through_brevo_from_the_configured_sender(self):
        with mock.patch('requests.Session.request', return_value=brevo_response()) as call:
            self.assertTrue(notifications.booking_received(self.make_booking()))
        args, kwargs = call.call_args
        method = (args[0] if args else kwargs['method']).upper()
        url = args[1] if len(args) > 1 else kwargs['url']
        sent = json.loads(kwargs['data'])
        self.assertEqual((method, url), ('POST', 'https://api.brevo.com/v3/smtp/email'))
        self.assertEqual(call.call_args.kwargs['headers']['api-key'], FAKE_KEY)
        self.assertEqual(sent['sender'], {'email': 'niyoyitarobert1@gmail.com', 'name': 'Explore Rwanda'})
        self.assertEqual(sent['to'], [{'email': 'amina@example.com'}])
        self.assertIn('We received your booking request', sent['htmlContent'])
        self.assertTrue(EmailLog.objects.filter(kind='booking_received', status=EmailLog.Status.SENT).exists())

    def test_sign_up_codes_go_through_brevo(self):
        with mock.patch('requests.Session.request', return_value=brevo_response()) as call:
            self.client.post(reverse('register'), {
                'full_name': 'Nadia Uwase', 'email': 'nadia@example.com',
                'password1': 'Strong-Pass-2026', 'password2': 'Strong-Pass-2026'})
        sent = json.loads(call.call_args.kwargs['data'])
        self.assertEqual(sent['to'], [{'email': 'nadia@example.com'}])
        self.assertEqual(sent['subject'], 'Your Account Verification Code')

    def test_a_rejected_key_is_reported_plainly_without_the_key(self):
        refusal = brevo_response(401, {'code': 'unauthorized', 'message': 'Key not found'})
        with mock.patch('requests.Session.request', return_value=refusal):
            self.assertFalse(notifications.booking_received(self.make_booking()))
        failure = EmailLog.objects.get(kind='booking_received')
        self.assertEqual(failure.status, EmailLog.Status.FAILED)
        self.assertIn('Brevo rejected the API key', failure.error)
        self.assertNotIn(FAKE_KEY, failure.error)

    def test_an_unverified_sender_is_reported(self):
        refusal = brevo_response(400, {'code': 'invalid_parameter', 'message': 'Sender is not valid'})
        with mock.patch('requests.Session.request', return_value=refusal):
            notifications.booking_received(self.make_booking())
        self.assertIn('Verify that address in Brevo', EmailLog.objects.get(kind='booking_received').error)

    def test_email_centre_shows_brevo_without_the_key(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('email_center'))
        self.assertContains(page, 'Brevo connection')
        self.assertContains(page, 'niyoyitarobert1@gmail.com')
        self.assertContains(page, 'set (hidden)')
        self.assertNotContains(page, FAKE_KEY)


class BrevoSettingsTests(TestCase):
    """settings.py picks Brevo only when a key is set, and console output still wins for local testing."""

    def settings_with(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(('BREVO_', 'DJANGO_EMAIL', 'EMAIL_'))}
        base.update({'DJANGO_DEBUG': '0', 'DJANGO_SECRET_KEY': 'x' * 50, 'DATABASE_URL': 'sqlite:///:memory:',
                     **env})
        code = ('import django,os;os.environ["DJANGO_SETTINGS_MODULE"]="Travels.settings";django.setup();'
                'from django.conf import settings as s;print(s.EMAIL_BACKEND);print(s.DEFAULT_FROM_EMAIL)')
        out = subprocess.run([sys.executable, '-c', code], env=base, capture_output=True, text=True,
                             cwd=Path(settings.BASE_DIR), check=True).stdout.split('\n')
        return out[0], out[1]

    def test_key_set_means_brevo_from_niyoyitarobert1(self):
        self.assertEqual(self.settings_with(BREVO_API_KEY=FAKE_KEY, EMAIL_HOST_USER='other@gmail.com'),
                         ('anymail.backends.brevo.EmailBackend', 'Explore Rwanda <niyoyitarobert1@gmail.com>'))

    def test_no_key_keeps_gmail(self):
        backend, _ = self.settings_with(EMAIL_HOST_USER='other@gmail.com', BREVO_API_KEY='',
                                        DJANGO_EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend')
        self.assertEqual(backend, 'django.core.mail.backends.smtp.EmailBackend')

    def test_console_backend_wins_for_local_testing(self):
        backend, _ = self.settings_with(BREVO_API_KEY=FAKE_KEY,
                                        DJANGO_EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
        self.assertEqual(backend, 'django.core.mail.backends.console.EmailBackend')
