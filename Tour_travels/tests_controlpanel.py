"""
Tests for the Control Panel: backgrounds, Details, About, Contact, social media, FAQs,
booking duration and summary, time conflicts, administrator booking changes, and password recovery.
"""

import io
import re
import shutil
import tempfile
from datetime import datetime, time as clock, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .bookings import BookingUnavailable, create_booking, update_booking
from .models import (
    AboutSection, BackgroundImage, Booking, CoHost, ContactDetail, DetailBook, DetailLink, DetailPhoto,
    DetailSection, DetailsPage, FAQ, Notification, SiteSettings, SocialLink, TourSchedule,
)
from .tests import DataMixin, PASSWORD, future


def png_bytes(colour=(18, 61, 50)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), colour).save(buffer, format='PNG')
    return buffer.getvalue()


class ControlPanelTestCase(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media_root)
        self._media.enable()

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def png(self, name='image.png', colour=(18, 61, 50)):
        return SimpleUploadedFile(name, png_bytes(colour), content_type='image/png')

    def hold(self, user, schedule, guests=1, duration=1, status=Booking.Status.PENDING):
        return create_booking(user=user, schedule=schedule, guests=guests, duration_hours=duration,
                              status=status, full_name='Guest', email=user.email or 'g@example.com',
                              phone='+250700000001', country='Rwanda')

    def booking_data(self, **overrides):
        data = {'guests': 1, 'duration_hours': 1, 'destination': self.destination.pk,
                'full_name': 'Amina K', 'email': 'amina@example.com',
                'phone': '+250700000000', 'country': 'Rwanda', 'special_requests': '', 'confirm': '1'}
        data.update(overrides)
        return data


# ---------- sign-in and access ----------

class SingleSignInTests(ControlPanelTestCase):
    def test_public_login_has_no_separate_administrator_option(self):
        page = self.client.get(reverse('login'))
        self.assertNotContains(page, 'Use the administrator sign-in')
        self.assertNotContains(page, 'Administrator sign-in')
        with self.assertRaises(NoReverseMatch):
            reverse('admin_login')
        self.assertEqual(self.client.get('/manage/login/').status_code, 404)

    def test_administrator_uses_the_normal_login_and_reaches_the_control_panel(self):
        response = self.client.post(reverse('login'), {'username': 'staffer', 'password': PASSWORD})
        self.assertRedirects(response, reverse('admin_dashboard'))
        self.assertContains(self.client.get(reverse('admin_dashboard')), 'Manage your whole website')

    def test_client_is_sent_to_the_client_dashboard_even_with_an_admin_next_link(self):
        response = self.client.post(reverse('login') + '?next=/manage/', {'username': 'amina', 'password': PASSWORD})
        self.assertRedirects(response, reverse('dashboard'))

    def test_anonymous_visitors_are_sent_to_the_single_login(self):
        response = self.client.get(reverse('background_manage'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_control_panel_offers_every_management_area(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('admin_dashboard'))
        for name in ['background_manage', 'site_settings', 'about_manage', 'details_manage', 'contact_manage',
                     'admin_payments', 'admin_inquiries', 'admin_bookings', 'admin_calendar']:
            with self.subTest(area=name):
                self.assertContains(page, reverse(name))
        self.assertContains(page, reverse('content_list', args=['social']))
        self.assertContains(page, reverse('content_list', args=['faqs']))

    def test_clients_cannot_open_control_panel_areas(self):
        self.client.force_login(self.user)
        for name in ['background_manage', 'about_manage', 'details_manage', 'contact_manage']:
            with self.subTest(area=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.get(reverse('content_list', args=['social'])).status_code, 403)


# ---------- background images ----------

class BackgroundImageTests(ControlPanelTestCase):
    def save(self, section, **post):
        return self.client.post(reverse('background_save', args=[section]), post)

    def test_administrator_uploads_replaces_and_removes_a_background(self):
        self.client.force_login(self.staff)
        self.save('hero', image=self.png('first.png'))
        hero = BackgroundImage.objects.get(section='hero')
        first_name = hero.image.name
        self.assertTrue(first_name.startswith('backgrounds/'))
        home = self.client.get(reverse('home'))
        self.client.logout()
        self.assertContains(self.client.get(reverse('home')), hero.image.url)

        self.client.force_login(self.staff)
        self.save('hero', image=self.png('second.png', colour=(200, 100, 50)))
        hero.refresh_from_db()
        self.assertNotEqual(hero.image.name, first_name)
        self.assertFalse(hero.image.storage.exists(first_name))  # the replaced file is removed

        self.save('hero', action='remove')
        hero.refresh_from_db()
        self.assertFalse(hero.image)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse('home')), 'backgrounds/')

    def test_background_applies_to_inner_pages(self):
        self.client.force_login(self.staff)
        self.save('about', image=self.png())
        self.client.logout()
        url = BackgroundImage.objects.get(section='about').image.url
        self.assertContains(self.client.get(reverse('about')), url)

    def test_background_rejects_files_that_are_not_images(self):
        self.client.force_login(self.staff)
        fake = SimpleUploadedFile('photo.png', b'not really an image', content_type='image/png')
        self.save('contact', image=fake)
        self.assertFalse(BackgroundImage.objects.filter(section='contact').exclude(image='').exists())

    def test_background_rejects_unknown_sections_and_clients(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse('background_save', args=['nonsense']),
                                          {'image': self.png()}).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('background_save', args=['hero']),
                                          {'image': self.png()}).status_code, 403)

    def test_every_background_section_has_a_row_in_the_form(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('background_manage'))
        self.assertEqual(len(page.context['rows']), len(BackgroundImage.Section.values))


# ---------- Details page ----------

class DetailsPageTests(ControlPanelTestCase):
    def test_public_details_page_shows_every_kind_of_information(self):
        page = DetailsPage.load()
        page.title = 'Know before you go'
        page.body = 'First paragraph.\n\nSecond paragraph.'
        page.save()
        DetailBook.objects.create(title='Rwanda: A Short History', author='A. Author',
                                  description='Start here.', link='https://books.example.com/rwanda')
        DetailLink.objects.create(title='National Museum', url='https://museum.example.com',
                                  description='Official site')
        DetailSection.objects.create(heading='Getting around', body='Taxis and motorbikes.')
        DetailPhoto.objects.create(image=self.png('gallery.png'), caption='Hills at dawn')
        response = self.client.get(reverse('details'))
        self.assertContains(response, 'Know before you go')
        self.assertContains(response, 'Rwanda: A Short History')
        self.assertContains(response, 'href="https://books.example.com/rwanda"')
        self.assertContains(response, 'National Museum')
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, 'Getting around')
        self.assertContains(response, 'Hills at dawn')
        self.assertContains(response, 'Recommended books')

    def test_hidden_details_page_is_not_found(self):
        page = DetailsPage.load()
        page.is_published = False
        page.save()
        self.assertEqual(self.client.get(reverse('details')).status_code, 404)

    def test_inactive_items_are_hidden(self):
        DetailSection.objects.create(heading='Secret note', body='hidden', is_active=False)
        self.assertNotContains(self.client.get(reverse('details')), 'Secret note')

    def test_administrator_manages_details_lists_and_the_page(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('details_manage'), {'title': 'Details', 'intro': 'Intro text', 'body': '',
                                                     'is_published': 'on'})
        self.client.post(reverse('content_new', args=['detail_links']),
                         {'title': 'Tourism board', 'url': 'tourism.example.com', 'description': '',
                          'order': 0, 'is_active': 'on'})
        link = DetailLink.objects.get()
        self.assertEqual(link.url, 'https://tourism.example.com')  # a bare address gets https
        self.client.post(reverse('content_edit', args=['detail_links', link.pk]),
                         {'title': 'Tourism board', 'url': 'https://tourism.example.com', 'description': 'Updated',
                          'order': 0, 'is_active': 'on'})
        self.client.logout()
        self.assertContains(self.client.get(reverse('details')), 'Updated')
        self.client.force_login(self.staff)
        self.client.post(reverse('content_delete', args=['detail_links', link.pk]))
        self.assertFalse(DetailLink.objects.exists())


# ---------- About ----------

class AboutTests(ControlPanelTestCase):
    def test_about_text_and_sections_are_managed_and_shown(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('about_manage'), {'about_title': 'Our team', 'about_text': 'We guide.'})
        self.client.post(reverse('content_new', args=['about_sections']),
                         {'heading': 'Our promise', 'body': 'Honest guiding.', 'image': self.png('p.png'),
                          'order': 1, 'is_active': 'on'})
        section = AboutSection.objects.get()
        self.assertTrue(section.image.name.startswith('about/'))
        self.client.logout()
        page = self.client.get(reverse('about'))
        self.assertContains(page, 'Our team')
        self.assertContains(page, 'We guide.')
        self.assertContains(page, 'Our promise')
        self.assertContains(page, section.image.url)

    def test_inactive_about_section_is_hidden(self):
        AboutSection.objects.create(heading='Hidden block', body='x', is_active=False)
        self.assertNotContains(self.client.get(reverse('about')), 'Hidden block')


# ---------- Contact ----------

class ContactTests(ControlPanelTestCase):
    def test_contact_details_show_on_the_contact_page(self):
        ContactDetail.objects.create(kind='phone', label='Tour desk', value='+250 788 555 000')
        ContactDetail.objects.create(kind='hours', value='Mon-Sat 08:00-18:00')
        ContactDetail.objects.create(kind='email', value='hidden@example.com', is_active=False)
        page = self.client.get(reverse('contact'))
        self.assertContains(page, 'Tour desk')
        self.assertContains(page, '+250 788 555 000')
        self.assertContains(page, 'Mon-Sat 08:00-18:00')
        self.assertNotContains(page, 'hidden@example.com')

    def test_contact_manage_saves_and_validates_whatsapp(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('contact_manage'), {
            'contact_intro': 'Hi', 'contact_email': 'hello@example.com', 'contact_phone': '+250 788 000 111',
            'contact_address': 'Kigali', 'whatsapp_number': '+250 (788) 000-111', 'payment_phone': ''})
        self.assertEqual(SiteSettings.load().whatsapp_number, '250788000111')
        self.client.post(reverse('contact_manage'), {
            'contact_intro': 'Hi', 'contact_email': '', 'contact_phone': '', 'contact_address': '',
            'whatsapp_number': 'call me', 'payment_phone': ''})
        self.assertEqual(SiteSettings.load().whatsapp_number, '250788000111')  # invalid value not saved

    def test_visitor_message_is_saved_and_listed_for_administrators(self):
        self.client.post(reverse('contact'), {'name': 'Lee', 'email': 'lee@example.com', 'phone': '',
                                              'package': '', 'message': 'Are you open on Sundays?'})
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_inquiries')), 'Are you open on Sundays?')


# ---------- social media and footer ----------

class FooterTests(ControlPanelTestCase):
    def test_social_icons_open_the_correct_accounts(self):
        SocialLink.objects.create(platform='facebook', url='https://facebook.com/explorerwanda')
        SocialLink.objects.create(platform='instagram', url='https://instagram.com/explorerwanda')
        SocialLink.objects.create(platform='x', url='https://x.com/explorerwanda')
        SocialLink.objects.create(platform='tiktok', url='https://tiktok.com/@explorerwanda')
        page = self.client.get(reverse('home')).content.decode()
        for platform, url in [('facebook', 'https://facebook.com/explorerwanda'),
                              ('instagram', 'https://instagram.com/explorerwanda'),
                              ('x', 'https://x.com/explorerwanda'),
                              ('tiktok', 'https://tiktok.com/@explorerwanda')]:
            with self.subTest(platform=platform):
                anchor = re.search(rf'<a class="social-link social-{platform}"[^>]*>', page).group(0)
                self.assertIn(f'href="{url}"', anchor)
                self.assertIn('target="_blank"', anchor)

    def test_explore_section_is_removed_from_the_footer(self):
        page = self.client.get(reverse('home'))
        self.assertNotContains(page, '<h3>Explore</h3>')
        self.assertContains(page, '<h3>Contact</h3>')

    def test_social_links_are_managed_from_the_control_panel(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('content_new', args=['social']),
                         {'platform': 'linkedin', 'label': 'Our company page', 'url': 'linkedin.com/company/x',
                          'order': 0, 'is_active': 'on'})
        link = SocialLink.objects.get()
        self.assertEqual(link.url, 'https://linkedin.com/company/x')
        self.client.logout()
        self.assertContains(self.client.get(reverse('home')), 'Our company page')

    def test_footer_hides_social_section_when_empty(self):
        self.assertNotContains(self.client.get(reverse('home')), 'Follow us')


# ---------- FAQ management ----------

class FaqManagementTests(ControlPanelTestCase):
    def test_administrator_adds_edits_and_deletes_any_question(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('content_new', args=['faqs']),
                         {'question': 'Can we bring children?', 'answer': 'Yes, with a responsible adult.',
                          'order': 5, 'is_active': 'on'})
        faq = FAQ.objects.get(question='Can we bring children?')
        self.client.logout()
        self.assertContains(self.client.get(reverse('faq')), 'Can we bring children?')
        self.client.force_login(self.staff)
        self.client.post(reverse('content_edit', args=['faqs', faq.pk]),
                         {'question': 'Can we bring children?', 'answer': 'Yes, always welcome.', 'order': 5,
                          'is_active': 'on'})
        faq.refresh_from_db()
        self.assertEqual(faq.answer, 'Yes, always welcome.')
        self.client.post(reverse('content_delete', args=['faqs', faq.pk]))
        self.assertFalse(FAQ.objects.filter(pk=faq.pk).exists())


# ---------- booking duration, summary and conflicts ----------

class BookingDurationTests(ControlPanelTestCase):
    def post_booking(self, schedule, **data):
        return self.client.post(reverse('book_package', args=[self.package.slug]),
                                self.booking_data(schedule=schedule.pk, **data))

    def test_submitting_shows_a_summary_before_anything_is_saved(self):
        schedule = self.slot(capacity=5)
        self.client.force_login(self.user)
        response = self.post_booking(schedule, duration_hours=3, confirm='')
        self.assertContains(response, 'Review your booking')
        self.assertContains(response, '3 hours')
        self.assertFalse(Booking.objects.exists())

    def test_confirming_saves_the_booking_with_its_duration(self):
        schedule = self.slot(capacity=5)
        self.client.force_login(self.user)
        self.post_booking(schedule, duration_hours=3)
        booking = Booking.objects.get()
        self.assertEqual(booking.duration_hours, 3)
        self.assertEqual(booking.window_minutes, (540, 720))  # 09:00 to 12:00

    def test_duration_is_limited(self):
        schedule = self.slot(capacity=5)
        self.client.force_login(self.user)
        response = self.post_booking(schedule, duration_hours=13)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Booking.objects.exists())

    def test_a_different_start_time_that_overlaps_is_refused(self):
        early = self.slot(capacity=10, start_time=clock(9, 0))
        late = self.slot(capacity=10, start_time=clock(10, 0))
        self.hold(self.other, early, guests=1, duration=3, status=Booking.Status.CONFIRMED)
        with self.assertRaises(BookingUnavailable) as caught:
            self.hold(self.user, late, guests=1, duration=1)
        self.assertIn('already reserved', str(caught.exception))
        self.client.force_login(self.user)
        self.assertContains(self.post_booking(late, duration_hours=1), 'already reserved')

    def test_the_same_start_time_shares_capacity_instead_of_conflicting(self):
        schedule = self.slot(capacity=10, start_time=clock(9, 0))
        self.hold(self.other, schedule, guests=2, duration=3, status=Booking.Status.CONFIRMED)
        second = self.hold(self.user, schedule, guests=1, duration=1)
        self.assertEqual(second.schedule, schedule)

    def test_times_that_do_not_overlap_are_both_allowed(self):
        early = self.slot(capacity=10, start_time=clock(9, 0))
        late = self.slot(capacity=10, start_time=clock(12, 0))
        self.hold(self.other, early, guests=1, duration=2, status=Booking.Status.CONFIRMED)
        self.hold(self.user, late, guests=1, duration=2)
        self.assertEqual(Booking.objects.count(), 2)

    def test_calendar_does_not_offer_a_time_covered_by_a_longer_booking(self):
        early = self.slot(capacity=10, start_time=clock(9, 0), days=20)
        covered = self.slot(capacity=10, start_time=clock(10, 0), days=20)
        self.hold(self.other, early, guests=1, duration=3, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        feed = self.client.get(reverse('tour_availability', args=[self.package.slug]),
                               {'month': covered.date.strftime('%Y-%m')}).json()
        self.assertFalse(feed['slots'][str(covered.pk)]['bookable'])


# ---------- administrator changes to bookings ----------

class AdminBookingChangeTests(ControlPanelTestCase):
    def edit_url(self, booking):
        return reverse('admin_booking_edit', args=[booking.pk])

    def edit_data(self, schedule, **overrides):
        data = {'schedule': schedule.pk, 'guests': 2, 'duration_hours': 2, 'full_name': 'Amina K',
                'email': 'amina@example.com', 'phone': '+250700000000', 'country': 'Rwanda',
                'special_requests': ''}
        data.update(overrides)
        return data

    def test_moving_a_booking_moves_its_seats_and_date_and_notifies_the_client(self):
        first = self.slot(capacity=5, days=7, start_time=clock(9, 0))
        second = self.slot(capacity=5, days=9, start_time=clock(9, 0))
        booking = self.hold(self.user, first, guests=2, duration=2, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        self.client.post(self.edit_url(booking), self.edit_data(second))
        booking.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(booking.schedule, second)
        self.assertEqual(booking.travel_date, second.date)
        self.assertEqual((first.seats_booked, second.seats_booked), (0, 2))
        self.assertTrue(Notification.objects.filter(recipient=self.user, title__icontains='updated').exists())

    def test_edit_is_refused_when_the_new_time_overlaps_another_booking(self):
        busy = self.slot(capacity=5, days=7, start_time=clock(10, 0))
        mine = self.slot(capacity=5, days=7, start_time=clock(9, 0))
        self.hold(self.other, busy, guests=1, duration=3, status=Booking.Status.CONFIRMED)
        booking = self.hold(self.user, mine, guests=1, duration=1, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        response = self.client.post(self.edit_url(booking), self.edit_data(mine, guests=1, duration_hours=2))
        self.assertContains(response, 'already reserved')
        booking.refresh_from_db()
        self.assertEqual(booking.duration_hours, 1)

    def test_edit_is_refused_when_the_new_slot_is_full_and_old_seats_are_kept(self):
        full = self.slot(capacity=1, days=7, start_time=clock(14, 0))
        mine = self.slot(capacity=5, days=7, start_time=clock(9, 0))
        booking = self.hold(self.user, mine, guests=2, duration=1, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        self.client.post(self.edit_url(booking), self.edit_data(full, guests=2, duration_hours=1))
        booking.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(booking.schedule, mine)
        self.assertEqual(mine.seats_booked, 2)

    def test_admin_calendar_lists_every_booking_on_its_day(self):
        schedule = self.slot(capacity=6, days=12, start_time=clock(11, 0))
        booking = self.hold(self.user, schedule, guests=3, duration=4, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin_calendar'), {
            'month': schedule.date.strftime('%Y-%m'), 'date': schedule.date.isoformat()})
        self.assertContains(response, booking.reference)
        self.assertContains(response, '11:00')
        self.assertContains(response, '4 h')
        self.assertContains(response, reverse('admin_booking_edit', args=[booking.pk]))


# ---------- password recovery ----------

class PasswordRecoveryTests(ControlPanelTestCase):
    def reset_link(self, user):
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        return uid, default_token_generator.make_token(user)

    def test_reset_token_expires_after_the_configured_period(self):
        uid, token = self.reset_link(self.user)
        later = datetime.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 120)
        with mock.patch.object(default_token_generator, '_now', return_value=later):
            page = self.client.get(reverse('password_reset_confirm', args=[uid, token]), follow=True)
        self.assertFalse(page.context['validlink'])
        self.assertContains(page, 'Link no longer valid')

    def test_reset_link_cannot_be_used_twice(self):
        uid, token = self.reset_link(self.user)
        first = self.client.get(reverse('password_reset_confirm', args=[uid, token]), follow=True)
        target = first.redirect_chain[-1][0]
        self.client.post(target, {'new_password1': 'Once-only-pass-12', 'new_password2': 'Once-only-pass-12'})
        self.client.logout()
        again = self.client.get(reverse('password_reset_confirm', args=[uid, token]), follow=True)
        self.assertFalse(again.context['validlink'])

    def test_administrator_reset_email_contains_a_link_and_never_the_password(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_reset', args=[self.user.pk]))
        body = mail.outbox[0].body
        self.assertIn('/password-reset/', body)
        self.assertNotIn(PASSWORD, body)


# ---------- every Control Panel page works ----------

class ControlPanelCrawlTests(ControlPanelTestCase):
    def test_every_control_panel_page_loads_for_administrators(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        names = ['admin_dashboard', 'background_manage', 'site_settings', 'about_manage', 'details_manage',
                 'contact_manage', 'admin_bookings', 'admin_calendar', 'admin_payments', 'admin_inquiries',
                 'admin_customers', 'admin_create', 'admin_activity', 'content_index', 'notifications',
                 'admin_schedules']
        for name in names:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        for section in ['destinations', 'tours', 'categories', 'reviews', 'faqs', 'culture', 'cohosts',
                        'social', 'contact_details', 'about_sections', 'detail_photos', 'detail_books',
                        'detail_links', 'detail_sections']:
            with self.subTest(section=section):
                self.assertEqual(self.client.get(reverse('content_list', args=[section])).status_code, 200)
                expected = 302 if section == 'reviews' else 200  # reviews are written by clients only
                self.assertEqual(self.client.get(reverse('content_new', args=[section])).status_code, expected)
        self.assertEqual(self.client.get(reverse('admin_booking_edit', args=[booking.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin_broadcast')).status_code, 302)
