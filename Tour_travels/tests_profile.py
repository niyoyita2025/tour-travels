"""
Profiles: name, photo, phone, country and bio; the email change that needs a code; who may edit what;
and the markup for the show/hide password buttons and the language selector.
"""

import io
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import otp
from .models import EmailOTP, UserProfile
from .otp_testing import code_post, emailed_code
from .tests import DataMixin, PASSWORD

User = get_user_model()
NEW_EMAIL = 'amina.new@example.com'


def image_file(name='me.png', size=(800, 600), fmt='PNG', colour=(18, 61, 50)):
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f'image/{fmt.lower()}')


class ProfileTestCase(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media_root)
        self._media.enable()
        self.addCleanup(self._media.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.client.force_login(self.user)

    def details(self, **overrides):
        data = {'full_name': 'Amina Uwase', 'phone': '+250 788 123 456', 'country': 'Rwanda', 'bio': 'I love Kigali.'}
        data.update(overrides)
        return data

    def save_details(self, **overrides):
        return self.client.post(reverse('profile'), self.details(**overrides), follow=True)


class ProfileDetailsTests(ProfileTestCase):
    def test_profile_requires_sign_in(self):
        self.client.logout()
        self.assertRedirects(self.client.get(reverse('profile')), f"{reverse('login')}?next={reverse('profile')}")

    def test_saves_name_phone_country_and_bio(self):
        response = self.save_details(full_name='Amina Uwase K')
        self.assertContains(response, 'Your profile has been saved.')
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ('Amina', 'Uwase K'))
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual((profile.phone, profile.country, profile.bio),
                         ('+250 788 123 456', 'Rwanda', 'I love Kigali.'))

    def test_changes_can_be_made_again_at_any_time(self):
        self.save_details(bio='First version')
        self.save_details(bio='Second version')
        self.assertEqual(UserProfile.objects.get(user=self.user).bio, 'Second version')

    def test_bio_over_the_limit_is_refused(self):
        self.save_details(bio='x' * 501)
        self.assertEqual(UserProfile.objects.get(user=self.user).bio, '')

    def test_invalid_phone_number_is_refused(self):
        self.save_details(phone='call me maybe')
        self.assertEqual(UserProfile.objects.get(user=self.user).phone, '')

    def test_full_name_is_required(self):
        self.save_details(full_name='   ')
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Amina')  # unchanged

    def test_photo_is_cropped_to_a_square_and_stored_as_jpeg(self):
        self.save_details(photo=image_file(size=(800, 600)))
        profile = UserProfile.objects.get(user=self.user)
        self.assertTrue(profile.photo.name.startswith('profiles/'))
        self.assertTrue(profile.photo.name.endswith('.jpg'))
        with default_storage.open(profile.photo.name) as stored:
            picture = Image.open(stored)
            self.assertEqual(picture.format, 'JPEG')
            self.assertEqual(picture.size[0], picture.size[1])  # square: never stretched

    def test_a_non_image_file_is_refused(self):
        fake = SimpleUploadedFile('photo.jpg', b'this is not an image', content_type='image/jpeg')
        self.save_details(photo=fake)
        self.assertFalse(UserProfile.objects.get(user=self.user).photo)

    def test_remove_photo_clears_it_and_deletes_the_file(self):
        self.save_details(photo=image_file())
        name = UserProfile.objects.get(user=self.user).photo.name
        self.assertTrue(default_storage.exists(name))
        self.save_details(remove_photo='on')
        self.assertFalse(UserProfile.objects.get(user=self.user).photo)
        self.assertFalse(default_storage.exists(name))

    def test_replacing_the_photo_deletes_the_old_file(self):
        self.save_details(photo=image_file('first.png'))
        first = UserProfile.objects.get(user=self.user).photo.name
        self.save_details(photo=image_file('second.png', colour=(200, 40, 40)))
        second = UserProfile.objects.get(user=self.user).photo.name
        self.assertNotEqual(first, second)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(second))

    def test_photo_and_name_show_in_the_header_and_dashboard(self):
        self.save_details(photo=image_file())
        photo_url = UserProfile.objects.get(user=self.user).photo.url
        self.assertContains(self.client.get(reverse('home')), f'src="{photo_url}"')
        self.assertContains(self.client.get(reverse('dashboard')), f'src="{photo_url}"')
        self.assertContains(self.client.get(reverse('dashboard')), 'Edit profile')

    def test_initials_show_when_there_is_no_photo(self):
        page = self.client.get(reverse('dashboard'))
        self.assertContains(page, 'avatar-lg" aria-hidden="true">A</span>')

    def test_profile_page_is_available_on_the_administrator_dashboard_too(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('profile'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'dash-sidebar')


class OwnProfileTests(ProfileTestCase):
    def test_each_person_edits_only_their_own_profile(self):
        self.client.force_login(self.other)
        self.client.post(reverse('profile'), self.details(full_name='Ben Mugisha', bio='Ben bio'))
        self.client.force_login(self.user)
        # Trying to name another account in the request changes nothing.
        self.client.post(reverse('profile') + f'?user={self.other.pk}&pk={self.other.pk}',
                         self.details(full_name='Amina Uwase', bio='Amina bio', phone='+250 700 000 000'))
        self.other.refresh_from_db()
        self.assertEqual(self.other.first_name, 'Ben')
        self.assertEqual(UserProfile.objects.get(user=self.other).bio, 'Ben bio')
        self.assertEqual(UserProfile.objects.get(user=self.user).bio, 'Amina bio')

    def test_profile_address_has_no_account_id_in_it(self):
        self.assertEqual(reverse('profile'), '/profile/')


class EmailChangeTests(ProfileTestCase):
    def request_change(self, email=NEW_EMAIL):
        return self.client.post(reverse('profile_email'), {'new_email': email})

    def test_the_code_goes_to_the_new_address_and_the_old_one_stays_until_confirmed(self):
        self.assertRedirects(self.request_change(), reverse('profile_email_verify'))
        self.assertEqual(mail.outbox[-1].to, [NEW_EMAIL])
        self.assertEqual(mail.outbox[-1].subject, 'Confirm your new email address')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'amina@example.com')

    def test_entering_the_code_changes_the_address(self):
        self.request_change()
        response = self.client.post(reverse('profile_email_verify'), code_post(emailed_code(mail.outbox[-1])))
        self.assertRedirects(response, reverse('profile'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW_EMAIL)
        self.client.logout()
        self.client.post(reverse('login'), {'username': NEW_EMAIL, 'password': PASSWORD})
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_a_wrong_code_leaves_the_address_unchanged(self):
        self.request_change()
        right = emailed_code(mail.outbox[-1])
        wrong = '000000' if right != '000000' else '111111'
        response = self.client.post(reverse('profile_email_verify'), code_post(wrong), follow=True)
        self.assertContains(response, 'attempts left')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'amina@example.com')

    def test_an_address_another_account_uses_is_refused(self):
        response = self.request_change('ben@example.com')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Another account already uses this email address')
        self.assertEqual(len(mail.outbox), 0)

    def test_the_current_address_is_refused(self):
        response = self.request_change('AMINA@example.com')
        self.assertContains(response, 'already your email address')
        self.assertEqual(len(mail.outbox), 0)

    def test_switching_to_another_address_while_a_code_is_out_is_refused(self):
        self.request_change(NEW_EMAIL)
        response = self.request_change('someone.else@example.com')
        self.assertRedirects(response, reverse('profile_email_verify'))
        self.assertEqual(len(mail.outbox), 1)  # the second address never received a code
        self.assertEqual(otp.pending_target(self.user, otp.EMAIL_CHANGE), NEW_EMAIL)

    def test_a_code_cannot_be_used_once_the_address_is_taken_by_someone_else(self):
        self.request_change()
        code = emailed_code(mail.outbox[-1])
        User.objects.create_user('latecomer', NEW_EMAIL, PASSWORD)
        self.client.post(reverse('profile_email_verify'), code_post(code))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'amina@example.com')

    def test_confirmation_page_without_a_pending_change_goes_back_to_profile(self):
        self.assertRedirects(self.client.get(reverse('profile_email_verify')), reverse('profile'))

    def test_the_profile_shows_the_masked_pending_address(self):
        self.request_change()
        page = self.client.get(reverse('profile'))
        self.assertContains(page, 'a***@example.com')  # masked, the way the code page shows it
        self.assertNotContains(page, NEW_EMAIL)

    def test_codes_expire_like_other_codes(self):
        self.request_change()
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(reverse('profile_email_verify'), code_post(emailed_code(mail.outbox[-1])),
                                    follow=True)
        self.assertContains(response, 'has expired')


class InterfaceMarkupTests(ProfileTestCase):
    def test_password_fields_get_the_show_and_hide_script(self):
        page = self.client.get(reverse('login'))
        self.assertContains(page, 'js/password-toggle.js')

    def test_language_selector_offers_the_four_languages(self):
        page = self.client.get(reverse('home'))
        for code, name in [('en', 'English'), ('rw', 'Kinyarwanda'), ('fr', 'French'), ('sw', 'Swahili')]:
            with self.subTest(code=code):
                self.assertContains(page, f'data-lang-option="{code}"')
                self.assertContains(page, name)
        self.assertContains(page, 'js/language.js')
        self.assertContains(page, 'data-lang-button')

    def test_the_selector_is_on_the_dashboards_too(self):
        self.assertContains(self.client.get(reverse('dashboard')), 'data-lang-picker')
