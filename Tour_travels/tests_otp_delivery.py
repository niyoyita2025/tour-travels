"""
Code delivery for sign-up and password reset. The visitor only reaches the code page when Gmail has accepted
the email, and a refusal is shown in plain words. Nothing here contacts Gmail: the mail is captured
in the test outbox, and the sign-in check is mocked.
"""

from smtplib import SMTPAuthenticationError, SMTPServerDisconnected
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from . import email_diagnostics, otp
from .models import ActivityLog, EmailLog, EmailOTP, UserProfile
from .otp_testing import code_post, emailed_code
from .tests import PASSWORD
from .tests_auth import AuthTestCase, NEW_PASSWORD

User = get_user_model()
SEND = 'Tour_travels.otp.send_mail'
REFUSED = SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')
DISCONNECTED = SMTPServerDisconnected('Connection unexpectedly closed')
NOT_SENT = 'no code is on its way'


class SignUpDeliveryTests(AuthTestCase):
    def test_a_refused_email_keeps_the_visitor_on_the_sign_up_page(self):
        with mock.patch(SEND, side_effect=REFUSED):
            response = self.register('nina@example.com')
        self.assertEqual(response.status_code, 200)  # not sent on to the code page
        self.assertTemplateUsed(response, 'accounts/register.html')
        self.assertContains(response, NOT_SENT)
        self.assertFalse(EmailOTP.objects.filter(user__email='nina@example.com').exists())
        self.assertTrue(EmailLog.objects.filter(status=EmailLog.Status.FAILED, recipient='nina@example.com').exists())

    def test_retrying_after_a_refusal_sends_a_code_and_opens_the_code_page(self):
        with mock.patch(SEND, side_effect=REFUSED):
            self.register('nina@example.com')
        response = self.register('nina@example.com')  # the unfinished account is reused
        self.assertRedirects(response, reverse('verify_email'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)

    def test_an_accepted_email_opens_the_code_page_and_the_code_verifies_the_account(self):
        response = self.register('nina@example.com')
        self.assertRedirects(response, reverse('verify_email'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['nina@example.com'])
        self.client.post(reverse('verify_email'), code_post(emailed_code(mail.outbox[-1])))
        self.assertTrue(User.objects.get(email='nina@example.com').is_active)
        self.assertTrue(EmailLog.objects.filter(status=EmailLog.Status.SENT, recipient='nina@example.com').exists())


class SignInDeliveryTests(AuthTestCase):
    def unverified_account(self):
        user = User.objects.create_user('nina', 'nina@example.com', PASSWORD, is_active=False)
        UserProfile.objects.filter(user=user).update(email_verified=False)
        return user

    def test_an_unverified_sign_in_with_a_refused_email_stays_on_the_sign_in_page(self):
        self.unverified_account()
        with mock.patch(SEND, side_effect=REFUSED):
            response = self.client.post(reverse('login'), {'username': 'nina@example.com', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NOT_SENT)

    def test_an_unverified_sign_in_with_an_accepted_email_opens_the_code_page(self):
        self.unverified_account()
        response = self.client.post(reverse('login'), {'username': 'nina@example.com', 'password': PASSWORD})
        self.assertRedirects(response, reverse('verify_email'), fetch_redirect_response=False)


class ForgotPasswordDeliveryTests(AuthTestCase):
    def test_a_refused_reset_email_for_a_real_account_stays_on_the_form(self):
        with mock.patch(SEND, side_effect=REFUSED):
            response = self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NOT_SENT)
        self.assertFalse(EmailOTP.objects.filter(user=self.user, purpose=otp.RESET).exists())

    def test_an_accepted_reset_email_opens_the_code_page(self):
        response = self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.assertRedirects(response, reverse('password_reset_verify'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)

    def test_an_unknown_address_gets_the_same_reply_and_no_email(self):
        response = self.client.post(reverse('password_reset'), {'email': 'nobody@example.com'})
        self.assertRedirects(response, reverse('password_reset_verify'), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_code_from_the_email_resets_the_password(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.client.post(reverse('password_reset_verify'), code_post(emailed_code(mail.outbox[-1])))
        self.client.post(reverse('password_reset_new'), {'new_password1': NEW_PASSWORD,
                                                         'new_password2': NEW_PASSWORD})
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(NEW_PASSWORD))


class DeliveryReasonTests(AuthTestCase):
    def test_a_disconnect_during_sign_in_points_to_the_app_password_and_hides_it(self):
        with override_settings(EMAIL_HOST_PASSWORD='not-the-real-one'):
            reason = otp.describe_email_error(DISCONNECTED)
        self.assertIn('app password', reason)
        self.assertNotIn('not-the-real-one', reason)


class SignInCheckTests(AuthTestCase):
    def test_nothing_is_checked_without_a_password(self):
        with override_settings(EMAIL_HOST_PASSWORD=''):
            ok, message = email_diagnostics.check_sign_in()
        self.assertFalse(ok)
        self.assertIn('EMAIL_HOST_PASSWORD', message)

    def test_a_refused_sign_in_gives_a_plain_reason_and_never_the_password(self):
        with override_settings(EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='not-the-real-one'), \
                mock.patch('Tour_travels.email_diagnostics.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = REFUSED
            ok, message = email_diagnostics.check_sign_in()
        self.assertFalse(ok)
        self.assertIn('Gmail rejected the sign-in', message)
        self.assertNotIn('not-the-real-one', message)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_accepted_sign_in_sends_nothing(self):
        with override_settings(EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='placeholder'), \
                mock.patch('Tour_travels.email_diagnostics.smtplib.SMTP') as smtp:
            ok, message = email_diagnostics.check_sign_in()
        self.assertTrue(ok)
        smtp.return_value.__enter__.return_value.login.assert_called_once_with('me@gmail.com', 'placeholder')
        self.assertIn('No email was sent', message)
        self.assertEqual(len(mail.outbox), 0)


class EmailCentreDiagnosticsTests(AuthTestCase):
    def test_only_administrators_can_run_the_sign_in_check(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('email_center'), {'action': 'check'}).status_code, 403)

    def test_the_check_reports_the_result_and_is_recorded_in_the_activity_log(self):
        self.client.force_login(self.staff)
        with mock.patch('Tour_travels.website_views.check_sign_in',
                        return_value=(False, 'Gmail rejected the sign-in for me@gmail.com.')):
            response = self.client.post(reverse('email_center'), {'action': 'check'}, follow=True)
        self.assertContains(response, 'Gmail rejected the sign-in')
        self.assertTrue(ActivityLog.objects.filter(action='email.check', description__contains='refused').exists())

    def test_the_email_centre_never_shows_the_app_password(self):
        self.client.force_login(self.staff)
        with override_settings(EMAIL_HOST_PASSWORD='secret-value-123'):
            response = self.client.get(reverse('email_center'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'secret-value-123')
