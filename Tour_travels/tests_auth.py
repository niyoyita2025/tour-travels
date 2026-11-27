"""
Sign-up with an emailed six-digit code, sign-in with an email address or username, and forgot password.

Covers: code generation and storage, activation, expiry, attempt limits, single use, resend intervals,
invalidation of older codes, delivery failures, duplicate emails, per-device limits, and the forgot-password
flow, including the identical response for registered and unknown addresses.
"""

import io
import re
from datetime import timedelta
from smtplib import SMTPAuthenticationError, SMTPException
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import otp
from .accounts_views import (
    PASSWORD_CHANGED_MESSAGE, RESET_REQUESTED_AT, RESET_VERIFIED_AT, RESET_SENT_MESSAGE, SEND_LIMIT,
    SEND_LIMIT_MESSAGE, TOO_MANY_CODES_MESSAGE,
)
from .models import EmailOTP, UserProfile
from .otp_testing import code_post, emailed_code
from .tests import DataMixin, PASSWORD

User = get_user_model()
NEW_PASSWORD = 'Brand-New-pass-99'
RESET_PASSWORD = 'Reset-pass-7788'
WRONG_CODE_MARKER = 'That code is not valid'


class AuthTestCase(DataMixin, TestCase):
    def setUp(self):
        cache.clear()  # the per-device limits live in the cache

    def register(self, email='new@example.com', full_name='New Person', **overrides):
        data = {'full_name': full_name, 'email': email, 'password1': NEW_PASSWORD, 'password2': NEW_PASSWORD}
        data.update(overrides)
        return self.client.post(reverse('register'), data)

    def pending_user(self, email='new@example.com'):
        return User.objects.get(email=email)

    def let_resend_interval_pass(self, user=None):
        """Move the newest codes into the past, so a new one may be requested."""
        qs = EmailOTP.objects.all() if user is None else EmailOTP.objects.filter(user=user)
        qs.update(created_at=timezone.now() - timedelta(seconds=settings.OTP_RESEND_SECONDS + 5))

    def set_session(self, **values):
        session = self.client.session
        session.update(values)
        session.save()


class OneTimeCodeTests(AuthTestCase):
    def test_codes_are_six_digits_including_leading_zeros(self):
        with mock.patch('Tour_travels.otp.secrets.randbelow', return_value=417):
            self.assertEqual(otp.generate_code(), '000417')
        with mock.patch('Tour_travels.otp.secrets.randbelow', return_value=0):
            self.assertEqual(otp.generate_code(), '000000')
        codes = {otp.generate_code() for _ in range(300)}
        self.assertTrue(all(re.fullmatch(r'\d{6}', code) for code in codes))

    def test_only_a_keyed_hash_of_the_code_is_stored(self):
        user = User.objects.create_user('hashcheck', 'hashcheck@example.com', PASSWORD, is_active=False)
        with mock.patch('Tour_travels.otp.secrets.randbelow', return_value=123456):
            otp.issue_code(user, otp.SIGNUP)
        row = EmailOTP.objects.get(user=user)
        self.assertEqual(len(row.code_hash), 64)
        self.assertNotIn('123456', row.code_hash)
        self.assertIn('123456', mail.outbox[-1].body)  # the person receives the code in the email

    def test_a_code_works_only_once(self):
        user = User.objects.create_user('once', 'once@example.com', PASSWORD)
        otp.issue_code(user, otp.RESET)
        code = emailed_code(mail.outbox[-1])
        self.assertEqual(otp.check_code(user, otp.RESET, code).status, otp.OK)
        self.assertEqual(otp.check_code(user, otp.RESET, code).status, otp.EXPIRED)

    def test_a_code_is_bound_to_its_purpose(self):
        user = User.objects.create_user('purpose', 'purpose@example.com', PASSWORD)
        otp.issue_code(user, otp.SIGNUP)
        code = emailed_code(mail.outbox[-1])
        self.assertEqual(otp.check_code(user, otp.RESET, code).status, otp.EXPIRED)
        self.assertEqual(otp.check_code(user, otp.SIGNUP, code).status, otp.OK)

    def test_a_new_code_cancels_the_previous_one(self):
        user = User.objects.create_user('replace', 'replace@example.com', PASSWORD)
        otp.issue_code(user, otp.RESET)
        first = emailed_code(mail.outbox[-1])
        self.let_resend_interval_pass(user)
        otp.issue_code(user, otp.RESET)
        second = emailed_code(mail.outbox[-1])
        if first == second:
            self.skipTest('the two random codes happened to match')
        self.assertEqual(otp.check_code(user, otp.RESET, first).status, otp.WRONG)  # old code is dead
        self.assertEqual(otp.check_code(user, otp.RESET, second).status, otp.OK)

    def test_expired_codes_are_refused(self):
        user = User.objects.create_user('late', 'late@example.com', PASSWORD)
        otp.issue_code(user, otp.RESET)
        code = emailed_code(mail.outbox[-1])
        EmailOTP.objects.filter(user=user).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(otp.check_code(user, otp.RESET, code).status, otp.EXPIRED)

    def test_wrong_codes_count_down_and_then_lock_the_code(self):
        user = User.objects.create_user('locker', 'locker@example.com', PASSWORD)
        otp.issue_code(user, otp.RESET)
        right = emailed_code(mail.outbox[-1])
        wrong = '000000' if right != '000000' else '000001'
        results = [otp.check_code(user, otp.RESET, wrong) for _ in range(settings.OTP_MAX_ATTEMPTS)]
        self.assertEqual([r.status for r in results[:-1]], [otp.WRONG] * (settings.OTP_MAX_ATTEMPTS - 1))
        self.assertEqual(results[0].attempts_left, settings.OTP_MAX_ATTEMPTS - 1)
        self.assertEqual(results[-1].status, otp.LOCKED)
        self.assertEqual(otp.check_code(user, otp.RESET, right).status, otp.LOCKED)  # even the right code

    def test_timers_report_the_code_lifetime_and_the_resend_wait(self):
        user = User.objects.create_user('timers', 'timers@example.com', PASSWORD)
        self.assertEqual(otp.timers(user, otp.RESET), (0, 0))
        otp.issue_code(user, otp.RESET)
        expires_in, resend_in = otp.timers(user, otp.RESET)
        self.assertGreater(expires_in, settings.OTP_LIFETIME_SECONDS - 5)
        self.assertGreater(resend_in, settings.OTP_RESEND_SECONDS - 5)

    def test_masked_email_shows_only_the_first_letter(self):
        self.assertEqual(otp.mask_email('niyoyita@gmail.com'), 'n***@gmail.com')
        self.assertEqual(otp.mask_email('not-an-address'), '')

    def test_codes_for_one_account_are_capped_per_window_and_purpose(self):
        user = User.objects.create_user('capped', 'capped@example.com', PASSWORD)
        for _ in range(settings.OTP_ACCOUNT_MAX_SENDS):
            otp.issue_code(user, otp.RESET)
            self.let_resend_interval_pass(user)
        with self.assertRaises(otp.TooManyCodes):
            otp.issue_code(user, otp.RESET)
        self.assertEqual(len(mail.outbox), settings.OTP_ACCOUNT_MAX_SENDS)
        otp.issue_code(user, otp.SIGNUP)  # another purpose has its own allowance
        self.assertEqual(len(mail.outbox), settings.OTP_ACCOUNT_MAX_SENDS + 1)


class SignUpTests(AuthTestCase):
    def test_registration_emails_a_code_with_the_agreed_wording(self):
        self.register()
        message = mail.outbox[-1]
        self.assertEqual(message.subject, 'Your Account Verification Code')
        self.assertEqual(message.to, ['new@example.com'])
        self.assertIn('Thank you for registering on our website.', message.body)
        self.assertIn('This code expires after 5 minutes. Do not share it with anyone.', message.body)
        self.assertNotIn(NEW_PASSWORD, message.body)

    def test_correct_code_activates_the_account_and_signs_in(self):
        self.register()
        response = self.client.post(reverse('verify_email'), code_post(emailed_code(mail.outbox[-1])))
        self.assertRedirects(response, reverse('profile'))  # a new account completes its profile first
        user = self.pending_user()
        self.assertTrue(user.is_active)
        self.assertTrue(UserProfile.objects.get(user=user).email_verified)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_a_verified_code_cannot_be_used_again_to_sign_in(self):
        self.register()
        code = emailed_code(mail.outbox[-1])
        self.client.post(reverse('verify_email'), code_post(code))
        self.client.logout()
        # The sign-up session is gone, so the code page sends the visitor back to sign-up.
        response = self.client.post(reverse('verify_email'), code_post(code))
        self.assertRedirects(response, reverse('register'))

    def test_wrong_code_is_refused_and_shows_the_attempts_left(self):
        self.register()
        right = emailed_code(mail.outbox[-1])
        wrong = '000000' if right != '000000' else '000001'
        response = self.client.post(reverse('verify_email'), code_post(wrong), follow=True)
        self.assertContains(response, f'{settings.OTP_MAX_ATTEMPTS - 1} attempts left')
        self.assertFalse(self.pending_user().is_active)

    def test_five_wrong_codes_lock_the_code_even_if_the_right_one_follows(self):
        self.register()
        right = emailed_code(mail.outbox[-1])
        wrong = '000000' if right != '000000' else '000001'
        for _ in range(settings.OTP_MAX_ATTEMPTS):
            self.client.post(reverse('verify_email'), code_post(wrong))
        response = self.client.post(reverse('verify_email'), code_post(right), follow=True)
        self.assertContains(response, 'Too many incorrect codes')
        self.assertFalse(self.pending_user().is_active)

    def test_an_incomplete_code_is_not_counted_as_an_attempt(self):
        self.register()
        response = self.client.post(reverse('verify_email'), {'digit_0': '1', 'digit_1': '2'}, follow=True)
        self.assertContains(response, 'Enter all six digits')
        self.assertEqual(EmailOTP.objects.get().attempts, 0)

    def test_expired_code_is_refused_with_a_clear_message(self):
        self.register()
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(reverse('verify_email'), code_post(emailed_code(mail.outbox[-1])), follow=True)
        self.assertContains(response, 'has expired')
        self.assertFalse(self.pending_user().is_active)

    def test_requesting_a_new_code_cancels_the_old_one(self):
        self.register()
        first = emailed_code(mail.outbox[-1])
        self.let_resend_interval_pass()
        self.client.post(reverse('verify_email_resend'))
        self.assertEqual(len(mail.outbox), 2)
        second = emailed_code(mail.outbox[-1])
        if first == second:
            self.skipTest('the two random codes happened to match')
        self.client.post(reverse('verify_email'), code_post(first))
        self.assertFalse(self.pending_user().is_active)
        self.client.post(reverse('verify_email'), code_post(second))
        self.assertTrue(self.pending_user().is_active)

    def test_resend_is_refused_until_the_interval_has_passed(self):
        self.register()
        response = self.client.post(reverse('verify_email_resend'), follow=True)
        self.assertContains(response, 'You can request another in')
        self.assertEqual(len(mail.outbox), 1)

    def test_sign_up_resends_stop_at_the_account_cap(self):
        self.register()
        for _ in range(settings.OTP_ACCOUNT_MAX_SENDS - 1):
            self.let_resend_interval_pass()
            self.client.post(reverse('verify_email_resend'))
        self.let_resend_interval_pass()
        response = self.client.post(reverse('verify_email_resend'), follow=True)
        self.assertContains(response, TOO_MANY_CODES_MESSAGE)
        self.assertEqual(len(mail.outbox), settings.OTP_ACCOUNT_MAX_SENDS)

    def test_a_failed_email_is_reported_and_can_be_retried(self):
        with mock.patch('Tour_travels.otp.send_mail', side_effect=SMTPException('Gmail is unavailable')):
            self.register()
        response = self.client.get(reverse('verify_email'))
        self.assertContains(response, 'email could not be sent')
        self.assertEqual(EmailOTP.objects.count(), 0)  # nothing was issued, so nothing can be entered
        self.assertEqual(len(mail.outbox), 0)

        self.client.post(reverse('verify_email_resend'))  # no wait is needed after a failed send
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailOTP.objects.count(), 1)

    def test_a_failed_resend_keeps_the_previous_code_working(self):
        self.register()
        code = emailed_code(mail.outbox[-1])
        self.let_resend_interval_pass()
        with mock.patch('Tour_travels.otp.send_mail', side_effect=SMTPException('down')):
            self.client.post(reverse('verify_email_resend'))
        response = self.client.post(reverse('verify_email'), code_post(code))
        self.assertRedirects(response, reverse('profile'))

    def test_duplicate_active_email_is_refused(self):
        response = self.register(email='AMINA@example.com')
        self.assertContains(response, 'already exists')
        self.assertEqual(len(mail.outbox), 0)

    def test_a_deactivated_account_cannot_be_recreated_through_sign_up(self):
        self.user.is_active = False
        self.user.save()
        response = self.register(email='amina@example.com')
        self.assertContains(response, 'already exists')
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unfinished_sign_up_is_resumed_instead_of_duplicated(self):
        self.register()
        self.let_resend_interval_pass()
        response = self.register(full_name='Different Name')
        self.assertRedirects(response, reverse('verify_email'))
        self.assertEqual(User.objects.filter(email__iexact='new@example.com').count(), 1)
        self.assertEqual(len(mail.outbox), 2)  # a fresh code went to the same address
        self.assertEqual(self.pending_user().first_name, 'New')  # the first details are kept

    def test_password_must_match_and_meet_the_rules(self):
        self.assertContains(self.register(password2='Different-pass-99'), 'do not match')
        response = self.register(password1='12345678', password2='12345678')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_full_name_needs_a_first_and_last_name(self):
        response = self.register(full_name='Cher')
        self.assertContains(response, 'first and last name')

    def test_code_requests_are_limited_per_device(self):
        for index in range(SEND_LIMIT):
            self.register(email=f'person{index}@example.com')
        response = self.register(email='one-more@example.com')
        self.assertContains(response, SEND_LIMIT_MESSAGE)
        self.assertFalse(User.objects.filter(email='one-more@example.com').exists())

    def test_the_verification_page_needs_a_started_sign_up(self):
        response = self.client.get(reverse('verify_email'))
        self.assertRedirects(response, reverse('register'))


class SignInTests(AuthTestCase):
    def test_sign_in_works_with_email_in_any_case_or_with_username(self):
        for identifier in ['amina@example.com', 'AMINA@Example.com', 'amina']:
            with self.subTest(identifier=identifier):
                self.client.logout()
                self.client.post(reverse('login'), {'username': identifier, 'password': PASSWORD})
                self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_wrong_password_with_an_email_is_refused(self):
        response = self.client.post(reverse('login'), {'username': 'amina@example.com', 'password': 'nope'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_unverified_account_is_sent_to_the_code_page_instead_of_a_generic_error(self):
        self.register()
        self.let_resend_interval_pass()
        response = self.client.post(reverse('login'), {'username': 'new@example.com', 'password': NEW_PASSWORD})
        self.assertRedirects(response, reverse('verify_email'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(len(mail.outbox), 2)

    def test_unverified_account_with_a_wrong_password_gets_the_ordinary_error(self):
        self.register()
        response = self.client.post(reverse('login'), {'username': 'new@example.com', 'password': 'wrong-one'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)  # no extra email for a wrong password


class ForgotPasswordTests(AuthTestCase):
    def request_reset(self, email):
        return self.client.post(reverse('password_reset'), {'email': email}, follow=True)

    def test_a_registered_email_receives_a_code_not_a_link(self):
        response = self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.assertRedirects(response, reverse('password_reset_verify'))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['amina@example.com'])
        self.assertEqual(message.subject, 'Your Password Reset Code')
        self.assertRegex(message.body, r'(?m)^\d{6}$')
        self.assertNotIn('/password-reset/', message.body)
        self.assertNotIn(PASSWORD, message.body)

    def test_unknown_and_known_addresses_get_the_same_response(self):
        known = self.request_reset('amina@example.com')
        unknown = self.request_reset('nobody@example.com')
        self.assertEqual(known.redirect_chain, unknown.redirect_chain)
        self.assertContains(known, RESET_SENT_MESSAGE)
        self.assertContains(unknown, RESET_SENT_MESSAGE)
        self.assertEqual(len(mail.outbox), 1)  # only the registered address received anything

    def test_unverified_or_deactivated_accounts_get_no_reset_code(self):
        self.user.is_active = False
        self.user.save()
        self.request_reset('amina@example.com')
        self.assertEqual(len(mail.outbox), 0)

    def test_the_reset_code_page_needs_a_started_request(self):
        response = self.client.get(reverse('password_reset_verify'))
        self.assertRedirects(response, reverse('password_reset'))

    def test_full_reset_flow_sets_a_new_password(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        code = emailed_code(mail.outbox[-1])
        response = self.client.post(reverse('password_reset_verify'), code_post(code))
        self.assertRedirects(response, reverse('password_reset_new'))
        response = self.client.post(reverse('password_reset_new'), {
            'new_password1': RESET_PASSWORD, 'new_password2': RESET_PASSWORD}, follow=True)
        self.assertRedirects(response, reverse('login'))
        self.assertContains(response, PASSWORD_CHANGED_MESSAGE)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(RESET_PASSWORD))
        self.assertFalse(self.user.check_password(PASSWORD))
        self.client.post(reverse('login'), {'username': 'amina@example.com', 'password': PASSWORD})
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)  # old password refused
        self.client.post(reverse('login'), {'username': 'amina@example.com', 'password': RESET_PASSWORD})
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_a_wrong_reset_code_gets_a_generic_message(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        code = emailed_code(mail.outbox[-1])
        wrong = '000000' if code != '000000' else '000001'
        response = self.client.post(reverse('password_reset_verify'), code_post(wrong), follow=True)
        self.assertContains(response, WRONG_CODE_MARKER)
        self.assertNotContains(response, 'attempts left')  # no hint that could reveal the account

    def test_an_unknown_address_cannot_pass_the_code_step(self):
        self.request_reset('nobody@example.com')
        response = self.client.post(reverse('password_reset_verify'), code_post('123456'), follow=True)
        self.assertContains(response, WRONG_CODE_MARKER)
        self.assertEqual(self.client.get(reverse('password_reset_new')).status_code, 302)

    def test_the_choose_password_step_expires(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.client.post(reverse('password_reset_verify'), code_post(emailed_code(mail.outbox[-1])))
        self.set_session(**{RESET_VERIFIED_AT: timezone.now().timestamp()
                            - settings.PASSWORD_RESET_CHOOSE_SECONDS - 60})
        response = self.client.get(reverse('password_reset_new'))
        self.assertRedirects(response, reverse('password_reset'))

    def test_the_choose_password_step_cannot_be_reused_after_completion(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.client.post(reverse('password_reset_verify'), code_post(emailed_code(mail.outbox[-1])))
        self.client.post(reverse('password_reset_new'), {
            'new_password1': RESET_PASSWORD, 'new_password2': RESET_PASSWORD})
        self.assertRedirects(self.client.get(reverse('password_reset_new')), reverse('password_reset'))

    def test_a_used_reset_code_cannot_be_entered_again(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        code = emailed_code(mail.outbox[-1])
        self.client.post(reverse('password_reset_verify'), code_post(code))
        self.set_session(**{RESET_REQUESTED_AT: timezone.now().timestamp() - 3600,
                            'reset_email': 'amina@example.com'})
        response = self.client.post(reverse('password_reset_verify'), code_post(code), follow=True)
        self.assertContains(response, WRONG_CODE_MARKER)

    def test_reset_resend_waits_for_the_interval(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        response = self.client.post(reverse('password_reset_resend'), follow=True)
        self.assertContains(response, 'Please wait')
        self.assertEqual(len(mail.outbox), 1)

    def test_reset_resend_sends_a_new_code_and_cancels_the_old_one(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        first = emailed_code(mail.outbox[-1])
        self.let_resend_interval_pass()
        self.set_session(**{RESET_REQUESTED_AT: timezone.now().timestamp() - settings.OTP_RESEND_SECONDS - 5})
        self.client.post(reverse('password_reset_resend'))
        self.assertEqual(len(mail.outbox), 2)
        second = emailed_code(mail.outbox[-1])
        if first == second:
            self.skipTest('the two random codes happened to match')
        self.assertContains(self.client.post(reverse('password_reset_verify'), code_post(first), follow=True),
                            WRONG_CODE_MARKER)
        self.assertRedirects(self.client.post(reverse('password_reset_verify'), code_post(second)),
                             reverse('password_reset_new'))

    def test_a_failed_reset_email_is_reported(self):
        with mock.patch('Tour_travels.otp.send_mail', side_effect=SMTPException('Gmail is unavailable')):
            response = self.request_reset('amina@example.com')
        self.assertContains(response, 'email could not be sent')

    def test_reset_requests_are_limited_per_device(self):
        for index in range(SEND_LIMIT):
            self.request_reset(f'nobody{index}@example.com')
        response = self.request_reset('amina@example.com')
        self.assertContains(response, SEND_LIMIT_MESSAGE)
        self.assertEqual(len(mail.outbox), 0)  # unknown addresses get no email, and the limit held

    def test_reset_requests_past_the_account_cap_send_nothing_and_look_the_same(self):
        for _ in range(settings.OTP_ACCOUNT_MAX_SENDS):
            self.request_reset('amina@example.com')
            self.let_resend_interval_pass(self.user)
        response = self.request_reset('amina@example.com')
        self.assertContains(response, RESET_SENT_MESSAGE)
        self.assertNotContains(response, TOO_MANY_CODES_MESSAGE)  # the limit must not reveal the account
        self.assertEqual(len(mail.outbox), settings.OTP_ACCOUNT_MAX_SENDS)

    def test_a_reset_code_for_one_account_cannot_reset_another(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        code = emailed_code(mail.outbox[-1])
        self.set_session(**{'reset_email': 'ben@example.com'})
        response = self.client.post(reverse('password_reset_verify'), code_post(code), follow=True)
        self.assertContains(response, WRONG_CODE_MARKER)


class EmailChangeCapTests(AuthTestCase):
    def test_email_change_codes_stop_at_the_account_cap(self):
        self.client.force_login(self.user)
        new_email = 'amina-new@example.com'
        for _ in range(settings.OTP_ACCOUNT_MAX_SENDS):
            self.client.post(reverse('profile_email'), {'new_email': new_email})
            self.let_resend_interval_pass(self.user)
        response = self.client.post(reverse('profile_email'), {'new_email': new_email}, follow=True)
        self.assertContains(response, TOO_MANY_CODES_MESSAGE)
        response = self.client.post(reverse('profile_email_resend'), follow=True)
        self.assertContains(response, TOO_MANY_CODES_MESSAGE)
        self.assertEqual(len(mail.outbox), settings.OTP_ACCOUNT_MAX_SENDS)


class PasswordStorageTests(AuthTestCase):
    def test_new_passwords_are_stored_with_argon2(self):
        user = User.objects.create_user('argon', 'argon@example.com', PASSWORD)
        self.assertTrue(user.password.startswith('argon2$'))

    def test_a_pbkdf2_password_still_signs_in_and_is_upgraded_to_argon2(self):
        user = User.objects.create(username='legacy', email='legacy@example.com', is_active=True,
                                   password=make_password(PASSWORD, hasher='pbkdf2_sha256'))
        response = self.client.post(reverse('login'), {'username': 'legacy@example.com', 'password': PASSWORD})
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.password.startswith('argon2$'))


class EmailDeliveryTests(AuthTestCase):
    def test_code_email_has_a_plain_text_and_a_styled_html_version(self):
        user = User.objects.create_user('htmlmail', 'htmlmail@example.com', PASSWORD)
        with mock.patch('Tour_travels.otp.secrets.randbelow', return_value=246810):
            otp.issue_code(user, otp.RESET)
        message = mail.outbox[-1]
        self.assertIn('246810', message.body)
        html = [content for content, kind in message.alternatives if kind == 'text/html']
        self.assertEqual(len(html), 1)
        self.assertIn('246810', html[0])
        self.assertIn('letter-spacing', html[0])  # the code is set in a styled box

    def test_reset_link_email_states_the_real_expiry(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_reset', args=[self.user.pk]))
        body = mail.outbox[0].body
        self.assertIn(f'expires after {settings.PASSWORD_RESET_TIMEOUT // 60} minutes', body)
        self.assertNotIn('24 hours', body)


class SendTestEmailCommandTests(AuthTestCase):
    def test_sends_one_email_through_the_current_backend(self):
        call_command('send_test_email', 'someone@example.com', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['someone@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Explore Rwanda: test email')

    def test_refuses_to_run_with_gmail_credentials_missing(self):
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
                               EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD=''):
            with self.assertRaisesMessage(CommandError, 'EMAIL_HOST_PASSWORD'):
                call_command('send_test_email', 'someone@example.com', stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 0)

    def test_reports_a_gmail_refusal_as_a_plain_error(self):
        refusal = SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')
        with mock.patch('Tour_travels.management.commands.send_test_email.send_mail', side_effect=refusal):
            with self.assertRaisesMessage(CommandError, 'Gmail rejected the sign-in'):
                call_command('send_test_email', 'someone@example.com', stdout=io.StringIO())


class DeliveryFailureTests(AuthTestCase):
    def test_a_refused_gmail_sign_in_shows_no_success_and_logs_the_reason_without_the_code(self):
        refusal = SMTPAuthenticationError(535, b'5.7.8 Username and Password not accepted')
        with mock.patch('Tour_travels.otp.send_mail', side_effect=refusal), \
                mock.patch('Tour_travels.otp.secrets.randbelow', return_value=135791), \
                self.assertLogs('Tour_travels.otp', level='ERROR') as logs:
            response = self.client.post(reverse('password_reset'), {'email': 'amina@example.com'}, follow=True)
        self.assertContains(response, 'no code is on its way')
        self.assertNotContains(response, RESET_SENT_MESSAGE)
        self.assertFalse(EmailOTP.objects.filter(user=self.user).exists())  # no orphan code is left
        log = '\n'.join(logs.output)
        self.assertIn('Gmail rejected the sign-in', log)
        self.assertNotIn('135791', log)
        self.assertNotIn(settings.EMAIL_HOST_PASSWORD or 'unset-password-marker', log)


class EmailSettingsCheckTests(AuthTestCase):
    SMTP = 'django.core.mail.backends.smtp.EmailBackend'

    def check_ids(self):
        from .checks import email_settings_check
        return {problem.id for problem in email_settings_check(None)}

    def test_missing_app_password_is_reported_at_startup(self):
        with override_settings(EMAIL_BACKEND=self.SMTP, EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='',
                               DEFAULT_FROM_EMAIL='Explore Rwanda <me@gmail.com>'):
            self.assertEqual(self.check_ids(), {'Tour_travels.W002'})

    def test_a_sender_other_than_the_gmail_account_is_reported(self):
        with override_settings(EMAIL_BACKEND=self.SMTP, EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='x',
                               DEFAULT_FROM_EMAIL='noreply@example.com'):
            self.assertEqual(self.check_ids(), {'Tour_travels.W003'})

    def test_complete_settings_and_the_console_backend_pass(self):
        with override_settings(EMAIL_BACKEND=self.SMTP, EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='x',
                               DEFAULT_FROM_EMAIL='Explore Rwanda <me@gmail.com>'):
            self.assertEqual(self.check_ids(), set())
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
                               EMAIL_HOST_PASSWORD=''):
            self.assertEqual(self.check_ids(), set())


class CreateSuperuserTests(AuthTestCase):
    def create(self, username, email, **kwargs):
        env = {'DJANGO_SUPERUSER_PASSWORD': 'Strong-Admin-Pass-2026'}
        with mock.patch.dict('os.environ', env):
            call_command('createsuperuser', interactive=False, username=username, email=email,
                         stdout=io.StringIO(), stderr=io.StringIO(), **kwargs)

    def test_creates_an_active_staff_superuser_who_lands_on_the_admin_dashboard(self):
        self.create('chief', 'chief@example.com')
        chief = User.objects.get(username='chief')
        self.assertTrue(chief.is_active and chief.is_staff and chief.is_superuser)
        self.assertTrue(chief.profile.email_verified)
        self.assertTrue(chief.password.startswith('argon2$'))
        response = self.client.post(reverse('login'), {'username': 'chief@example.com',
                                                       'password': 'Strong-Admin-Pass-2026'})
        self.assertRedirects(response, reverse('admin_dashboard'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 200)
        self.assertEqual(self.client.get('/admin/').status_code, 200)  # Django admin panel

    def test_refuses_an_email_that_another_account_uses(self):
        with self.assertRaisesMessage(CommandError, 'Another account already uses'):
            self.create('second', self.user.email.upper())
        self.assertFalse(User.objects.filter(username='second').exists())

    def test_refuses_a_blank_email(self):
        with self.assertRaisesMessage(CommandError, 'An email address is required'):
            self.create('noemail', '')

    def test_interactive_prompt_asks_again_for_a_taken_email(self):
        answers = iter(['boss', self.user.email, 'boss@example.com'])
        from .management.commands.createsuperuser import Command
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        command._database = 'default'
        email_field = User._meta.get_field('email')
        with mock.patch('builtins.input', side_effect=lambda *_: next(answers)):
            self.assertEqual(command.get_input_data(User._meta.get_field('username'), 'Username: '), 'boss')
            self.assertIsNone(command.get_input_data(email_field, 'Email: '))  # taken: asked again
            self.assertEqual(command.get_input_data(email_field, 'Email: '), 'boss@example.com')
        self.assertIn('Another account already uses', command.stderr.getvalue())

    def test_clients_cannot_open_administrator_pages(self):
        self.client.force_login(self.user)
        for name in ['admin_dashboard', 'admin_customers', 'admin_create']:
            with self.subTest(page=name):
                self.assertIn(self.client.get(reverse(name)).status_code, (302, 403))
        self.assertEqual(self.client.get('/admin/').status_code, 302)  # Django admin sends non-staff to login


class AuthPagesTests(AuthTestCase):
    def test_auth_pages_render(self):
        for name in ['register', 'login', 'password_reset']:
            with self.subTest(page=name):
                page = self.client.get(reverse(name))
                self.assertEqual(page.status_code, 200)
        self.assertContains(self.client.get(reverse('login')), 'name="username"')
        self.assertContains(self.client.get(reverse('register')), 'js/otp.js')

    def test_login_page_shows_the_site_name_not_the_domain(self):
        page = self.client.get(reverse('login'))
        self.assertContains(page, '<title>Log in | Explore Rwanda with a Local Guide</title>')

    def test_code_page_has_six_boxes_and_a_resend_control(self):
        self.register()
        page = self.client.get(reverse('verify_email'))
        for index in range(6):
            self.assertContains(page, f'name="digit_{index}"')
        self.assertContains(page, 'data-resend')
        self.assertContains(page, 'autocomplete="one-time-code"')

    def test_menu_offers_sign_up_on_phones(self):
        page = self.client.get(reverse('home'))
        self.assertContains(page, 'class="mobile-auth"')
        self.assertContains(page, reverse('register'))
