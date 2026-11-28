"""
Tests for the website-management additions: branding (name, logo, favicon), the fixed header, policies,
the branded email system and its delivery log, booking reminders, schedule-change emails, galleries,
testimonials, and the richer destination and tour details.
"""

import io
import shutil
import tempfile
from datetime import datetime, timedelta
from datetime import time as clock
from smtplib import SMTPAuthenticationError
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from . import notifications
from .models import (
    Booking, DestinationPhoto, EmailLog, SiteSettings, Testimonial, TourPhoto, TourSchedule,
)
from .otp_testing import code_post, emailed_code
from .reminders import send_due_reminders
from .tests import PASSWORD, DataMixin, future


def png(name='logo.png', size=(64, 64)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (18, 61, 50)).save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


class MediaMixin:
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media_root)
        self._media.enable()

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        super().tearDown()


# ---------- branding ----------

class BrandingTests(MediaMixin, DataMixin, TestCase):
    def save_branding(self, **extra):
        data = {'site_name': 'Kigali Hills Tours', 'tagline': 'local guides', 'site_description': 'Walks in Kigali.'}
        data.update(extra)
        self.client.force_login(self.staff)
        return self.client.post(reverse('branding_manage'), data, follow=True)

    def test_new_name_logo_and_favicon_appear_on_every_page_and_in_emails(self):
        response = self.save_branding(logo=png(), favicon=png('icon.png', (32, 32)))
        self.assertContains(response, 'Branding saved.')
        site = SiteSettings.load()
        self.assertTrue(site.logo.name.startswith('site/'))
        for url in [reverse('home'), reverse('packages'), reverse('destinations'), reverse('login')]:
            with self.subTest(url=url):
                page = self.client.get(url).content.decode()
                self.assertIn(site.logo.url, page)
                self.assertIn(f'<link rel="icon" href="{site.favicon.url}">', page)
                self.assertIn('Kigali Hills Tours', page)
                self.assertIn('local guides', page)
        for url in [reverse('home'), reverse('login')]:  # pages without their own description
            self.assertIn('content="Walks in Kigali."', self.client.get(url).content.decode())
        dashboard = self.client.get(reverse('admin_dashboard')).content.decode()
        self.assertIn(site.logo.url, dashboard)
        notifications.booking_received(self.make_booking())
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(site.logo.url, html)
        self.assertIn('Kigali Hills Tours', html)

    def test_without_a_logo_the_first_letter_of_the_name_is_shown(self):
        self.save_branding(site_name='Umusozi Tours')
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn('<span class="brandmark" aria-hidden="true">U</span>', page)
        self.assertNotIn('rel="icon"', page)

    def test_a_dark_logo_is_used_in_dark_mode_and_on_dark_backgrounds(self):
        self.save_branding(logo=png(), logo_dark=png('dark.png'))
        site = SiteSettings.load()
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn(f'class="brand-logo brand-logo-dark" src="{site.logo_dark.url}"', page)

    def test_wrong_file_types_and_oversized_favicons_are_refused(self):
        response = self.save_branding(logo=SimpleUploadedFile('logo.gif', b'GIF89a', content_type='image/gif'))
        self.assertContains(response, 'Please fix the highlighted fields.')
        self.assertFalse(SiteSettings.load().logo)
        from django.core.exceptions import ValidationError

        from .models import validate_favicon_size
        with self.assertRaisesMessage(ValidationError, '512 KB'):
            validate_favicon_size(mock.Mock(size=600 * 1024))

    def test_uploaded_logo_is_served_when_debug_is_off(self):
        from django.views.static import serve
        self.save_branding(logo=png())
        url = SiteSettings.load().logo.url
        response = serve(self.client.get('/').wsgi_request, url[len('/media/'):], document_root=self.media_root)
        self.assertEqual(response.status_code, 200)
        from django.urls import resolve
        with self.settings(DEBUG=False, SERVE_MEDIA=True):
            import importlib

            import Travels.urls
            importlib.reload(Travels.urls)
            self.assertEqual(resolve(url, 'Travels.urls').func, serve)
        importlib.reload(Travels.urls)

    def test_a_logo_can_be_removed(self):
        self.save_branding(logo=png())
        self.client.post(reverse('branding_remove', args=['logo']))
        self.assertFalse(SiteSettings.load().logo)
        self.assertNotIn('brand-logo', self.client.get(reverse('home')).content.decode())

    def test_clients_cannot_open_branding_or_email_pages(self):
        self.client.force_login(self.user)
        for name in ['branding_manage', 'policies_manage', 'email_center']:
            with self.subTest(page=name):
                self.assertIn(self.client.get(reverse(name)).status_code, (302, 403))
        self.client.post(reverse('branding_manage'), {'site_name': 'Hacked'})
        self.assertNotEqual(SiteSettings.load().site_name, 'Hacked')


# ---------- header ----------

class HeaderTests(DataMixin, TestCase):
    def test_header_has_every_main_link_and_is_fixed(self):
        page = self.client.get(reverse('about')).content.decode()
        self.assertIn('id="siteNav"', page)
        for name in ['home', 'destinations', 'packages', 'about', 'contact', 'login', 'register']:
            with self.subTest(link=name):
                self.assertIn(f'href="{reverse(name)}"', page)
        self.assertIn('class="nav-book" href="/tours/#tour-list">Book Now</a>', page)
        self.assertIn('id="menuBtn"', page)  # phones get the menu button

    def test_header_follows_the_signed_in_role(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('home')), 'My account')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('home')), 'Admin dashboard')


# ---------- policies ----------

class PolicyTests(DataMixin, TestCase):
    def test_empty_policies_have_no_page_or_footer_link(self):
        site = SiteSettings.load()
        site.terms_text = site.privacy_text = site.booking_policy_text = ''
        site.save()
        self.assertEqual(self.client.get(reverse('policy', args=['terms'])).status_code, 404)
        self.assertNotIn('/policies/', self.client.get(reverse('home')).content.decode())

    def test_saved_policies_get_their_page_and_footer_link(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('policies_manage'), {
            'terms_text': 'Rule one.\n\nRule two.', 'privacy_text': 'We keep data safe.',
            'booking_policy_text': 'Cancel 48 hours ahead.'})
        response = self.client.get(reverse('policy', args=['terms']))
        self.assertContains(response, '<p>Rule one.</p>', html=True)
        self.assertContains(response, '<p>Rule two.</p>', html=True)
        home = self.client.get(reverse('home')).content.decode()
        for slug in ['terms', 'privacy', 'booking-policy']:
            self.assertIn(reverse('policy', args=[slug]), home)
        tour = self.client.get(self.package.get_absolute_url())
        self.assertContains(tour, 'Cancel 48 hours ahead.')


# ---------- emails ----------

class EmailSystemTests(DataMixin, TestCase):
    def setUp(self):
        site = SiteSettings.load()
        site.staff_notification_email = 'desk@example.com'
        site.contact_phone = '+250 788 000 000'
        site.save()

    def test_booking_email_is_branded_has_details_and_is_logged(self):
        booking = self.make_booking()
        self.assertTrue(notifications.booking_received(booking))
        customer, staff = mail.outbox
        self.assertEqual(customer.to, ['amina@example.com'])
        self.assertIn(booking.reference, customer.body)
        self.assertIn('Kigali Highlights', customer.body)
        html = customer.alternatives[0][0]
        self.assertIn('We received your booking request', html)
        self.assertIn('+250 788 000 000', html)  # contact details in the footer
        self.assertIn(booking.get_absolute_url(), html)
        self.assertEqual(staff.to, ['desk@example.com'])
        self.assertEqual(EmailLog.objects.filter(status=EmailLog.Status.SENT).count(), 2)

    def test_each_kind_of_email_uses_its_own_template(self):
        booking = self.make_booking()
        notifications.booking_received(booking)
        booking.status = Booking.Status.CONFIRMED
        notifications.booking_status_changed(booking)
        notifications.booking_updated(booking)
        notifications.booking_reminder(booking)
        labels = [m.alternatives[0][0] for m in mail.outbox if m.to == ['amina@example.com']]
        for label in ['Booking request', 'Booking update', 'Booking changed', 'Reminder']:
            self.assertTrue(any(f'>{label}</p>' in html for html in labels), label)

    def test_a_failed_booking_email_is_reported_truthfully_and_the_booking_is_kept(self):
        slot = self.slot()
        self.client.force_login(self.user)
        refusal = SMTPAuthenticationError(535, b'Username and Password not accepted')
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=refusal):
            response = self.client.post(reverse('book_package', args=[self.package.slug]),
                                        self.booking_form_data(schedule=slot.pk), follow=True)
        booking = Booking.objects.get()
        self.assertContains(response, 'could not send the confirmation email')
        self.assertNotContains(response, 'A confirmation email was sent')
        failed = EmailLog.objects.get(kind='booking_received')
        self.assertEqual(failed.status, EmailLog.Status.FAILED)
        self.assertIn('Gmail rejected the sign-in', failed.error)
        self.assertNotIn(booking.reference, failed.error)

    def test_a_successful_booking_says_the_email_was_sent(self):
        slot = self.slot()
        self.client.force_login(self.user)
        response = self.client.post(reverse('book_package', args=[self.package.slug]),
                                    self.booking_form_data(schedule=slot.pk), follow=True)
        self.assertContains(response, 'A confirmation email was sent to amina@example.com')

    def test_staff_emails_can_be_switched_off(self):
        site = SiteSettings.load()
        site.email_staff_on_booking = False
        site.save()
        notifications.booking_received(self.make_booking())
        self.assertEqual([m.to for m in mail.outbox], [['amina@example.com']])

    def test_customer_cancellation_emails_the_customer_and_staff(self):
        notifications.booking_cancelled_by_customer(self.make_booking())
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['amina@example.com', 'desk@example.com'])

    def test_codes_are_logged_without_the_code(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        code = emailed_code(mail.outbox[-1])
        log = EmailLog.objects.get(kind='reset_code')
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertNotIn(code, log.subject + log.error)


class AccountEmailTests(DataMixin, TestCase):
    def test_welcome_email_after_verification(self):
        self.client.post(reverse('register'), {
            'full_name': 'Nadia Uwase', 'email': 'nadia@example.com',
            'password1': 'Strong-Pass-2026', 'password2': 'Strong-Pass-2026'})
        code = emailed_code(mail.outbox[-1])
        self.client.post(reverse('verify_email'), code_post(code))
        welcome = mail.outbox[-1]
        self.assertEqual(welcome.to, ['nadia@example.com'])
        self.assertIn('Your account is ready', welcome.body)
        self.assertTrue(EmailLog.objects.filter(kind='welcome', status=EmailLog.Status.SENT).exists())

    def test_password_reset_and_change_send_a_security_notice(self):
        self.client.post(reverse('password_reset'), {'email': 'amina@example.com'})
        self.client.post(reverse('password_reset_verify'), code_post(emailed_code(mail.outbox[-1])))
        self.client.post(reverse('password_reset_new'),
                         {'new_password1': 'New-Pass-2026x', 'new_password2': 'New-Pass-2026x'})
        self.assertIn('changed with an emailed code', mail.outbox[-1].body)
        self.assertNotIn('New-Pass-2026x', mail.outbox[-1].body)
        self.client.login(username='amina', password='New-Pass-2026x')
        self.client.post(reverse('password_change'), {
            'old_password': 'New-Pass-2026x', 'new_password1': 'Other-Pass-2026y',
            'new_password2': 'Other-Pass-2026y'})
        self.assertIn('from your account settings', mail.outbox[-1].body)


class EmailCentreTests(DataMixin, TestCase):
    def test_status_settings_and_log_are_shown(self):
        EmailLog.objects.create(kind='welcome', recipient='x@example.com', subject='Hi',
                                status=EmailLog.Status.FAILED, error='EMAIL_HOST_PASSWORD is empty.')
        self.client.force_login(self.staff)
        page = self.client.get(reverse('email_center'))
        self.assertContains(page, 'Gmail connection')
        self.assertContains(page, 'EMAIL_HOST_PASSWORD is empty.')
        self.assertNotContains(page, 'password_set')  # values, not internals
        self.client.post(reverse('email_center'), {
            'action': 'settings', 'staff_notification_email': 'desk@example.com',
            'email_staff_on_booking': 'on', 'reminder_hours_before': 12})
        site = SiteSettings.load()
        self.assertEqual(site.reminder_hours_before, 12)
        self.assertFalse(site.send_booking_reminders)

    def test_test_email_reports_success_and_failure_truthfully(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('email_test'), follow=True)
        self.assertContains(response, 'Gmail accepted the test email to staff@example.com')
        self.assertEqual(mail.outbox[-1].to, ['staff@example.com'])
        with mock.patch('django.core.mail.EmailMultiAlternatives.send',
                        side_effect=SMTPAuthenticationError(535, b'no')):
            response = self.client.post(reverse('email_test'), follow=True)
        self.assertContains(response, 'The test email was not sent.')


# ---------- reminders and schedule changes ----------

class ReminderTests(DataMixin, TestCase):
    def booking_starting_in(self, hours, status=Booking.Status.CONFIRMED):
        start = timezone.localtime() + timedelta(hours=hours)
        slot = TourSchedule.objects.create(package=self.package, date=start.date(),
                                           start_time=clock(start.hour, 0), capacity=10)
        booking = self.make_booking(status=status, travel_date=start.date())
        booking.schedule = slot
        booking.save()
        return booking

    def test_due_bookings_get_one_reminder(self):
        soon = self.booking_starting_in(5)
        later = self.booking_starting_in(24 * 5)
        self.assertEqual(send_due_reminders(), 1)
        self.assertEqual(mail.outbox[-1].to, ['amina@example.com'])
        self.assertIn('Your tour is coming up', mail.outbox[-1].body)
        soon.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNotNone(soon.reminder_sent_at)
        self.assertIsNone(later.reminder_sent_at)
        self.assertEqual(send_due_reminders(), 0)  # never twice

    def test_cancelled_bookings_and_switched_off_reminders_get_none(self):
        self.booking_starting_in(5, status=Booking.Status.CANCELLED)
        self.assertEqual(send_due_reminders(), 0)
        self.booking_starting_in(6)
        site = SiteSettings.load()
        site.send_booking_reminders = False
        site.save()
        self.assertEqual(send_due_reminders(), 0)

    def test_a_failed_reminder_is_tried_again(self):
        booking = self.booking_starting_in(5)
        with mock.patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('down')):
            self.assertEqual(send_due_reminders(), 0)
        booking.refresh_from_db()
        self.assertIsNone(booking.reminder_sent_at)
        self.assertEqual(send_due_reminders(), 1)

    def test_command_sends_reminders(self):
        self.booking_starting_in(5)
        out = io.StringIO()
        call_command('send_booking_reminders', stdout=out)
        self.assertIn('Sent 1 reminder(s).', out.getvalue())


class ScheduleChangeTests(DataMixin, TestCase):
    def test_moving_a_booked_time_emails_its_customers(self):
        slot = self.slot()
        booking = self.make_booking(status=Booking.Status.CONFIRMED, travel_date=slot.date)
        booking.schedule = slot
        booking.save()
        self.client.force_login(self.staff)
        new_day = future(12)
        response = self.client.post(reverse('admin_schedule_edit', args=[slot.pk]), {
            'package': self.package.pk, 'date': new_day.isoformat(), 'start_time': '11:00',
            'capacity': slot.capacity, 'is_open': 'on', 'notes': ''}, follow=True)
        self.assertContains(response, 'emailed about the change')
        message = mail.outbox[-1]
        self.assertEqual(message.to, ['amina@example.com'])
        self.assertIn('The schedule for your tour changed', message.body)
        self.assertIn('11:00', message.body)
        booking.refresh_from_db()
        self.assertEqual(booking.travel_date, new_day)


# ---------- destinations, tours, galleries and testimonials ----------

class ContentDetailTests(MediaMixin, DataMixin, TestCase):
    def test_staff_fill_in_every_destination_field_and_it_appears(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('content_edit', args=['destinations', self.destination.pk]), {
            'name': 'Kimironko Market', 'slug': self.destination.slug, 'location': 'Kigali', 'country': '',
            'summary': 'The biggest market in Kigali.', 'description': 'A busy market.',
            'attractions': 'Fabric hall\nFruit stalls', 'activities': 'Shopping\nTasting',
            'best_time': 'Weekday mornings', 'recommended_duration': '2 hours',
            'travel_info': 'Take a moto.', 'accommodation_info': 'Hotels nearby.',
            'visitor_info': 'Bring cash.', 'map_link': '', 'image_url': '', 'is_active': 'on'})
        self.destination.refresh_from_db()
        self.assertEqual(self.destination.country, 'Rwanda')  # blank means Rwanda
        DestinationPhoto.objects.create(destination=self.destination, image=png('g.png'), caption='Fabric hall')
        page = self.client.get(self.destination.get_absolute_url())
        for text in ['The biggest market in Kigali.', 'Fabric hall', 'Tasting', 'Weekday mornings', '2 hours',
                     'Take a moto.', 'Hotels nearby.', 'Bring cash.', 'class="gallery"']:
            self.assertContains(page, text)
        self.assertContains(self.client.get(reverse('destinations')), 'The biggest market in Kigali.')

    def test_tour_details_itinerary_and_gallery_appear(self):
        for field, value in {'itinerary': '09:00 Meet\n10:00 Market', 'included': 'Guide\nWater',
                             'excluded': 'Lunch', 'start_location': 'Kigali Convention Centre',
                             'end_location': 'Nyamirambo', 'travel_instructions': 'Wear walking shoes.',
                             'cancellation_policy': 'Free cancellation 24 hours ahead.'}.items():
            setattr(self.package, field, value)
        self.package.min_guests = 2
        self.package.save()
        TourPhoto.objects.create(package=self.package, image=png('t.png'), caption='Market lane')
        page = self.client.get(self.package.get_absolute_url())
        for text in ['Itinerary', '10:00 Market', 'Water', 'Lunch', 'Kigali Convention Centre', 'Nyamirambo',
                     'Wear walking shoes.', 'Free cancellation 24 hours ahead.', 'Market lane', '2 to 4 guests']:
            self.assertContains(page, text)

    def test_testimonials_appear_on_the_home_page_when_published(self):
        Testimonial.objects.create(name='Jo', origin='Leeds, UK', quote='Wonderful day.', rating=5)
        Testimonial.objects.create(name='Hidden', quote='Not shown.', is_active=False)
        page = self.client.get(reverse('home'))
        self.assertContains(page, 'Wonderful day.')
        self.assertContains(page, 'Leeds, UK')
        self.assertNotContains(page, 'Not shown.')

    def test_new_management_lists_open_for_staff(self):
        self.client.force_login(self.staff)
        for section in ['destination_photos', 'tour_photos', 'testimonials']:
            with self.subTest(section=section):
                self.assertEqual(self.client.get(reverse('content_list', args=[section])).status_code, 200)
                self.assertEqual(self.client.get(reverse('content_new', args=[section])).status_code, 200)

    def test_admin_sidebar_is_grouped_by_area(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('admin_dashboard')).content.decode()
        for group in ['Website management', 'Tour management', 'Booking management', 'Customer management',
                      'Email management', 'Additional management']:
            self.assertIn(group, page)
