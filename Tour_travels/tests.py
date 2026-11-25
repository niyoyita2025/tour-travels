import io
import re
import shutil
import tempfile
from datetime import timedelta
from datetime import time as clock
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .bookings import BookingUnavailable, create_booking, expire_stale_holds, set_booking_status
from .calendar_service import availability_payload
from .models import (
    FAQ, ActivityLog, Booking, Category, CultureItem, Destination, Inquiry, Notification, Payment, Review,
    CoHost, SiteSettings, SocialLink, TourPackage, TourSchedule, UserProfile,
)
from .otp_testing import code_post, emailed_code

User = get_user_model()
PASSWORD = 'Sup3r-Secret-pass'


def future(days=7):
    return timezone.localdate() + timedelta(days=days)


class DataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='City Tours')
        cls.destination = Destination.objects.create(name='Kimironko Market', description='A busy market.')
        cls.package = TourPackage.objects.create(
            title='Kigali Highlights', summary='Intro tour', description='Half day tour.',
            price=Decimal('35.00'), max_guests=4, category=cls.category,
            highlights='One\nTwo\n\nThree',
        )
        cls.package.destinations.add(cls.destination)
        cls.user = User.objects.create_user('amina', 'amina@example.com', PASSWORD, first_name='Amina')
        cls.other = User.objects.create_user('ben', 'ben@example.com', PASSWORD)
        cls.staff = User.objects.create_user('staffer', 'staff@example.com', PASSWORD, is_staff=True)

    def make_booking(self, user=None, status=Booking.Status.PENDING, guests=2, travel_date=None, package=None):
        return Booking.objects.create(
            user=user or self.user, package=package or self.package,
            travel_date=travel_date or future(), guests=guests,
            full_name='Amina K', email='amina@example.com', phone='+250700000000', country='Rwanda',
            status=status,
        )

    def slot(self, capacity=10, days=7, package=None, start_time=None):
        """A bookable date and time for a tour (created once, then reused)."""
        from datetime import time as clock
        schedule, _ = TourSchedule.objects.get_or_create(
            package=package or self.package, date=future(days), start_time=start_time or clock(9, 0),
            defaults={'capacity': capacity},
        )
        return schedule

    def booking_form_data(self, **overrides):
        data = {
            'travel_date': future().isoformat(), 'guests': 2, 'duration_hours': 2, 'confirm': '1',
            'destination': self.destination.pk, 'full_name': 'Amina K',
            'email': 'amina@example.com', 'phone': '+250700000000', 'country': 'Rwanda',
            'special_requests': '',
        }
        data.update(overrides)
        return data


class ModelTests(DataMixin, TestCase):
    def test_profile_created_with_user(self):
        self.assertTrue(UserProfile.objects.filter(user=self.user).exists())

    def test_total_price_is_snapshot_at_booking_time(self):
        booking = self.make_booking(guests=2)
        self.assertEqual(booking.total_price, Decimal('70.00'))
        self.package.price = Decimal('100.00')
        self.package.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_price, Decimal('70.00'))

    def test_reference_is_generated_and_unique(self):
        first, second = self.make_booking(), self.make_booking()
        self.assertTrue(first.reference.startswith('ER'))
        self.assertEqual(len(first.reference), 8)
        self.assertNotEqual(first.reference, second.reference)

    def test_guest_limit_is_enforced(self):
        booking = Booking(user=self.user, package=self.package, travel_date=future(), guests=5,
                          full_name='A', email='a@example.com', phone='1', country='RW')
        with self.assertRaises(ValidationError):
            booking.clean()

    def test_past_travel_date_is_rejected_for_new_booking(self):
        booking = Booking(user=self.user, package=self.package, travel_date=timezone.localdate() - timedelta(days=1),
                          guests=1, full_name='A', email='a@example.com', phone='1', country='RW')
        with self.assertRaises(ValidationError):
            booking.clean()

    def test_payment_status_is_set_by_staff_and_balance_comes_from_recorded_amounts(self):
        booking = self.make_booking(guests=2)  # total 70
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)
        Payment.objects.create(booking=booking, amount=Decimal('35'))
        self.assertEqual(booking.amount_paid, Decimal('35'))
        self.assertEqual(booking.balance_due, Decimal('35'))
        # Recording money never changes the status by itself.
        self.assertEqual(booking.payment_state, 'unpaid')
        booking.payment_status = Booking.PaymentStatus.PARTIAL
        booking.save()
        self.assertEqual(booking.payment_state, 'partial')

    def test_payment_statuses_are_exactly_the_four_required(self):
        self.assertEqual(
            [label for _, label in Booking.PaymentStatus.choices],
            ['Unpaid', 'Partially paid', 'Paid', 'Payment verified'])

    def test_one_review_per_user_per_package(self):
        Review.objects.create(package=self.package, user=self.user, rating=5, comment='Great')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Review.objects.create(package=self.package, user=self.user, rating=4, comment='Again')

    def test_highlight_list_skips_blank_lines(self):
        self.assertEqual(self.package.highlight_list, ['One', 'Two', 'Three'])

    def test_slugs_are_generated_and_unique(self):
        clone = TourPackage.objects.create(title='Kigali Highlights', summary='s', description='d', price=1)
        self.assertNotEqual(clone.slug, self.package.slug)
        self.assertTrue(clone.slug.startswith('kigali-highlights'))


class AccountTests(DataMixin, TestCase):
    def test_register_creates_unverified_account_and_emails_a_code(self):
        response = self.client.post(reverse('register'), {
            'full_name': 'New Person', 'email': 'new@example.com',
            'password1': 'Brand-New-pass-99', 'password2': 'Brand-New-pass-99',
        })
        self.assertRedirects(response, reverse('verify_email'))
        new_user = User.objects.get(email='new@example.com')
        self.assertFalse(new_user.is_active)                    # cannot sign in until verified
        self.assertFalse(new_user.profile.email_verified)
        self.assertEqual((new_user.first_name, new_user.last_name), ('New', 'Person'))
        self.assertNotEqual(new_user.password, 'Brand-New-pass-99')  # stored hashed
        self.assertNotIn('_auth_user_id', self.client.session)  # not signed in yet
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Your Account Verification Code')

    def test_register_rejects_duplicate_email(self):
        response = self.client.post(reverse('register'), {
            'full_name': 'Copy Cat', 'email': 'AMINA@example.com',
            'password1': 'Brand-New-pass-99', 'password2': 'Brand-New-pass-99',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(User.objects.filter(email__iexact='amina@example.com').count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse('dashboard'))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('dashboard')}")

    def test_login_and_logout(self):
        response = self.client.post(reverse('login'), {'username': 'amina', 'password': PASSWORD})
        self.assertRedirects(response, reverse('dashboard'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        response = self.client.post(reverse('logout'))
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)

    def test_wrong_password_is_rejected(self):
        response = self.client.post(reverse('login'), {'username': 'amina', 'password': 'wrong'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_password_change(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('password_change'), {
            'old_password': PASSWORD, 'new_password1': 'Another-pass-123', 'new_password2': 'Another-pass-123'})
        self.assertRedirects(response, reverse('password_change_done'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Another-pass-123'))

    # Forgot password is covered in tests_auth.py. This is the link an administrator emails.
    def test_password_reset_confirm_sets_new_password(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        first = self.client.get(reverse('password_reset_confirm', args=[uid, token]), follow=True)
        target = first.redirect_chain[-1][0]
        response = self.client.post(target, {'new_password1': 'Reset-pass-7788', 'new_password2': 'Reset-pass-7788'})
        self.assertRedirects(response, reverse('password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Reset-pass-7788'))


class BookingTests(DataMixin, TestCase):
    def test_booking_without_a_date_opens_the_calendar(self):
        url = reverse('book_package', args=[self.package.slug])
        self.assertRedirects(self.client.get(url), reverse('tour_calendar', args=[self.package.slug]))

    def test_creating_booking_sends_confirmation_email(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('book_package', args=[self.package.slug]),
                                    self.booking_form_data(schedule=self.slot().pk))
        booking = Booking.objects.get()
        self.assertRedirects(response, booking.get_absolute_url())
        self.assertEqual(booking.total_price, Decimal('70.00'))
        self.assertEqual(booking.status, Booking.Status.PENDING)
        customer_emails = [m for m in mail.outbox if 'amina@example.com' in m.to]
        self.assertEqual(len(customer_emails), 1)
        self.assertIn(booking.reference, customer_emails[0].body)

    def test_guest_limit_shows_error_and_creates_nothing(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('book_package', args=[self.package.slug]),
                                    self.booking_form_data(guests=9, schedule=self.slot().pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'at most 4 guests')
        self.assertFalse(Booking.objects.exists())

    def test_inactive_tour_cannot_be_booked(self):
        self.package.is_active = False
        self.package.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('book_package', args=[self.package.slug])).status_code, 404)

    def test_customer_cannot_view_someone_elses_booking(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(booking.get_absolute_url()).status_code, 404)

    def test_staff_can_view_any_booking(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(booking.get_absolute_url()).status_code, 200)

    def test_cancel_is_post_only_and_owner_only(self):
        booking = self.make_booking(user=self.user)
        url = reverse('cancel_booking', args=[booking.reference])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url).status_code, 404)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

        self.client.force_login(self.user)
        self.client.post(url)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    def test_past_booking_cannot_be_cancelled(self):
        booking = self.make_booking(user=self.user, travel_date=timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.user)
        self.client.post(reverse('cancel_booking', args=[booking.reference]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_clients_have_no_online_payment_route(self):
        from django.urls import NoReverseMatch
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        with self.assertRaises(NoReverseMatch):
            reverse('report_payment', args=[booking.reference])
        with self.assertRaises(NoReverseMatch):
            reverse('admin_payment_action', args=[1])

    def test_staff_record_payment_and_set_status_and_client_sees_it(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED, guests=2)  # total 70
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_record_payment', args=[booking.pk]),
                         {'amount': '35', 'method': 'mobile_money', 'transaction_reference': 'MM-1', 'note': ''})
        self.assertEqual(Payment.objects.get().booking, booking)
        self.client.post(reverse('admin_booking_payment_status', args=[booking.pk]),
                         {'payment_status': Booking.PaymentStatus.PARTIAL})
        booking.refresh_from_db()
        self.assertEqual(booking.payment_state, 'partial')
        self.client.force_login(self.user)
        page = self.client.get(booking.get_absolute_url())
        self.assertContains(page, 'Partially paid')
        self.assertContains(page, 'Balance due')
        self.assertNotContains(page, 'Record a payment received')

    def test_clients_cannot_change_payment_status(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        response = self.client.post(reverse('admin_booking_payment_status', args=[booking.pk]),
                                    {'payment_status': Booking.PaymentStatus.VERIFIED})
        self.assertEqual(response.status_code, 403)
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)

    def test_payment_status_change_notifies_the_client(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_booking_payment_status', args=[booking.pk]),
                         {'payment_status': Booking.PaymentStatus.VERIFIED})
        self.assertTrue(Notification.objects.filter(recipient=self.user,
                                                    title__icontains='Payment verified').exists())
        self.assertTrue(any('amina@example.com' in m.to for m in mail.outbox))

    def test_booking_needs_no_payment_to_submit(self):
        self.client.force_login(self.user)
        self.client.post(reverse('book_package', args=[self.package.slug]),
                         self.booking_form_data(schedule=self.slot().pk))
        booking = Booking.objects.get()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(booking.payment_status, Booking.PaymentStatus.UNPAID)
        self.assertFalse(Payment.objects.exists())

    def test_customer_dashboard_counts(self):
        self.make_booking(user=self.user, status=Booking.Status.CONFIRMED, guests=1)
        self.make_booking(user=self.user, status=Booking.Status.PENDING, guests=2)
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['total_bookings'], 2)
        self.assertEqual(response.context['total_spent'], Decimal('35.00'))


class ReviewTests(DataMixin, TestCase):
    def post_review(self, rating=5, comment='Lovely day'):
        return self.client.post(reverse('review_create', args=[self.package.slug]),
                                {'rating': rating, 'comment': comment})

    def test_review_needs_a_confirmed_booking(self):
        self.make_booking(user=self.user, status=Booking.Status.PENDING)
        self.client.force_login(self.user)
        self.post_review()
        self.assertFalse(Review.objects.exists())

    def test_review_after_confirmed_booking_and_only_once(self):
        self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        self.post_review(rating=5)
        self.post_review(rating=1)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Review.objects.get().rating, 5)
        self.assertEqual(self.package.average_rating, 5.0)

    def test_review_rating_must_be_valid(self):
        self.make_booking(user=self.user, status=Booking.Status.COMPLETED)
        self.client.force_login(self.user)
        self.post_review(rating=9)
        self.assertFalse(Review.objects.exists())


class StaffTests(DataMixin, TestCase):
    def test_anonymous_user_is_sent_to_admin_sign_in(self):
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_customer_gets_403_on_staff_pages(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('admin_dashboard')).status_code, 403)

    def test_staff_dashboard_loads(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Control Panel')

    def test_staff_confirms_booking_and_customer_is_emailed(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_booking_status', args=[booking.pk]), {'status': 'confirmed'})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertTrue(any('amina@example.com' in m.to and 'confirmed' in m.subject.lower() for m in mail.outbox))

    def test_invalid_status_is_rejected(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_booking_status', args=[booking.pk]), {'status': 'bogus'})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_staff_resolves_inquiry(self):
        inquiry = Inquiry.objects.create(name='Lee', email='lee@example.com', message='Hi')
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_inquiry_resolve', args=[inquiry.pk]))
        inquiry.refresh_from_db()
        self.assertTrue(inquiry.is_resolved)


class PublicPageTests(DataMixin, TestCase):
    def test_public_pages_load(self):
        for name in ['home', 'about', 'faq', 'contact', 'destinations', 'packages', 'login', 'register',
                     'password_reset']:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_detail_pages_load(self):
        self.assertEqual(self.client.get(self.package.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(self.destination.get_absolute_url()).status_code, 200)

    def test_package_search_and_price_filter(self):
        TourPackage.objects.create(title='Luxury Safari', summary='Expensive', description='d', price=Decimal('500'))
        response = self.client.get(reverse('packages'), {'max_price': '100'})
        titles = [p.title for p in response.context['page'].object_list]
        self.assertEqual(titles, ['Kigali Highlights'])

        response = self.client.get(reverse('packages'), {'q': 'safari'})
        self.assertEqual([p.title for p in response.context['page'].object_list], ['Luxury Safari'])

    def test_invalid_price_range_is_reported(self):
        response = self.client.get(reverse('packages'), {'min_price': '100', 'max_price': '10'})
        self.assertContains(response, 'Minimum price cannot be higher')

    def test_inactive_package_is_hidden(self):
        self.package.is_active = False
        self.package.save()
        self.assertEqual(self.client.get(self.package.get_absolute_url()).status_code, 404)

    def test_contact_form_saves_inquiry_and_notifies_staff(self):
        site = SiteSettings.load()
        site.staff_notification_email = 'staff@example.com'
        site.save()
        response = self.client.post(reverse('contact'), {
            'name': 'Lee', 'email': 'lee@example.com', 'phone': '', 'package': self.package.pk,
            'message': 'Do you run tours on Sundays?'})
        self.assertRedirects(response, reverse('contact'))
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.package, self.package)
        self.assertTrue(any('staff@example.com' in m.to for m in mail.outbox))

    def test_contact_form_shows_errors(self):
        response = self.client.post(reverse('contact'), {'name': '', 'email': 'bad', 'message': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Inquiry.objects.exists())

    def test_unknown_page_uses_custom_404(self):
        response = self.client.get('/no-such-page/')
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'We could not find that page', status_code=404)

    def test_seed_command_is_idempotent(self):
        call_command('seed_demo', verbosity=0)
        counts = (Destination.objects.count(), TourPackage.objects.count(), FAQ.objects.count())
        call_command('seed_demo', verbosity=0)
        self.assertEqual(counts, (Destination.objects.count(), TourPackage.objects.count(), FAQ.objects.count()))
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Kigali Genocide Memorial')


class ContentManagementTests(DataMixin, TestCase):
    """Everything published on the website can be managed by staff without the Django admin."""

    def setUp(self):
        self.client.force_login(self.staff)

    def test_customers_cannot_open_content_pages(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('content_index')).status_code, 403)
        self.assertEqual(self.client.get(reverse('site_settings')).status_code, 403)
        self.assertEqual(self.client.get(reverse('content_list', args=['tours'])).status_code, 403)

    def test_anonymous_visitors_are_sent_to_admin_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('content_index')).status_code, 302)

    def test_unknown_section_is_404(self):
        self.assertEqual(self.client.get(reverse('content_list', args=['nonsense'])).status_code, 404)

    def test_staff_creates_destination_with_every_field(self):
        response = self.client.post(reverse('content_new', args=['destinations']), {
            'name': 'Lake Kivu', 'slug': '', 'location': 'Rubavu', 'description': 'Lakeside town.',
            'image_url': 'https://example.com/kivu.jpg', 'is_featured': 'on', 'is_active': 'on',
        })
        self.assertRedirects(response, reverse('content_list', args=['destinations']))
        destination = Destination.objects.get(name='Lake Kivu')
        self.assertEqual(destination.slug, 'lake-kivu')
        self.assertTrue(destination.is_featured)
        self.assertEqual(destination.photo_url, 'https://example.com/kivu.jpg')

    def test_staff_creates_tour_and_it_appears_publicly(self):
        response = self.client.post(reverse('content_new', args=['tours']), {
            'title': 'Lake Kivu Escape', 'slug': '', 'category': self.category.pk,
            'destinations': [self.destination.pk], 'summary': 'Relax by the lake.', 'description': 'Full day.',
            'highlights': 'Boat ride', 'price': '80.00', 'duration_days': 1, 'max_guests': 6,
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(reverse('packages')), 'Lake Kivu Escape')

    def test_unpublished_tour_is_hidden_from_customers(self):
        self.client.post(reverse('content_edit', args=['tours', self.package.pk]), {
            'title': self.package.title, 'slug': self.package.slug, 'category': self.category.pk,
            'destinations': [self.destination.pk], 'summary': 'x', 'description': 'y', 'highlights': '',
            'price': '35.00', 'duration_days': 1, 'max_guests': 4,
            # is_active not ticked
        })
        self.package.refresh_from_db()
        self.assertFalse(self.package.is_active)
        self.assertEqual(self.client.get(self.package.get_absolute_url()).status_code, 404)

    def test_invalid_tour_is_not_saved(self):
        response = self.client.post(reverse('content_new', args=['tours']), {
            'title': 'Broken', 'summary': 'x', 'description': 'y', 'price': '-5',
            'duration_days': 1, 'max_guests': 2,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TourPackage.objects.filter(title='Broken').exists())

    def test_tour_with_bookings_cannot_be_deleted(self):
        self.make_booking(user=self.user)
        response = self.client.post(reverse('content_delete', args=['tours', self.package.pk]), follow=True)
        self.assertTrue(TourPackage.objects.filter(pk=self.package.pk).exists())
        self.assertContains(response, 'cannot be deleted')

    def test_faq_create_edit_and_delete(self):
        self.client.post(reverse('content_new', args=['faqs']), {
            'question': 'Do you pick up guests?', 'answer': 'Yes, in Kigali.', 'order': 3, 'is_active': 'on'})
        faq = FAQ.objects.get(question='Do you pick up guests?')
        self.assertEqual(self.client.get(reverse('faq')).status_code, 200)
        self.assertContains(self.client.get(reverse('faq')), 'Do you pick up guests?')
        self.client.post(reverse('content_delete', args=['faqs', faq.pk]))
        self.assertFalse(FAQ.objects.filter(pk=faq.pk).exists())

    def test_get_delete_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse('content_delete', args=['faqs', 1])).status_code, 405)

    def test_site_text_changes_show_on_home_about_and_contact(self):
        site = SiteSettings.load()
        self.client.post(reverse('site_settings'), {
            'site_name': 'Kigali Guides', 'footer_tagline': 'Footer line here.',
            'hero_eyebrow': 'Hello', 'hero_title': 'Welcome to Kigali Guides', 'hero_text': 'Hero body.',
            'intro_kicker': 'Kicker', 'intro_title': 'Intro heading', 'intro_text': 'Intro body.',
            'culture_intro': 'Culture intro.', 'history_note': 'History note.',
            'booking_notice': 'Notice text.',
            'final_cta_title': 'Closing title', 'final_cta_text': 'Closing text.',
            'payment_instructions': 'Bank: ABC 123', 'staff_notification_email': 'ops@example.com',
        })
        self.client.post(reverse('about_manage'), {'about_title': 'About the team', 'about_text': 'Our story.'})
        self.client.post(reverse('contact_manage'), {
            'contact_intro': 'Contact intro text.', 'contact_email': 'hello@example.com',
            'contact_phone': '+250 700 000 000', 'contact_address': '', 'whatsapp_number': '250700000000',
            'payment_phone': ''})
        site.refresh_from_db()
        self.assertEqual(site.hero_title, 'Welcome to Kigali Guides')
        self.assertContains(self.client.get(reverse('home')), 'Welcome to Kigali Guides')
        self.assertContains(self.client.get(reverse('about')), 'About the team')
        contact_page = self.client.get(reverse('contact'))
        self.assertContains(contact_page, 'hello@example.com')
        self.assertContains(contact_page, 'https://wa.me/250700000000')

    def test_culture_cards_follow_published_flag(self):
        CultureItem.objects.create(icon='🎵', title='Drums', text='Drum circles.', order=1, is_active=True)
        CultureItem.objects.create(icon='🎵', title='Hidden card', text='Not shown.', order=2, is_active=False)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Drums')
        self.assertNotContains(response, 'Hidden card')

    def test_staff_notification_goes_to_address_set_in_site_settings(self):
        site = SiteSettings.load()
        site.staff_notification_email = 'ops@example.com'
        site.save()
        self.client.logout()
        self.client.force_login(self.user)
        self.client.post(reverse('book_package', args=[self.package.slug]),
                         self.booking_form_data(schedule=self.slot().pk))
        self.assertTrue(any('ops@example.com' in m.to for m in mail.outbox))

    def test_hiding_a_review_removes_it_from_the_tour_page(self):
        review = Review.objects.create(package=self.package, user=self.other, rating=4, comment='Nice tour')
        self.assertContains(self.client.get(self.package.get_absolute_url()), 'Nice tour')
        self.client.post(reverse('content_edit', args=['reviews', review.pk]), {
            'rating': 4, 'comment': 'Nice tour'})  # is_published not ticked
        self.assertNotContains(self.client.get(self.package.get_absolute_url()), 'Nice tour')
        self.assertIsNone(self.package.average_rating)

    def test_staff_cannot_create_reviews_from_content_pages(self):
        response = self.client.get(reverse('content_new', args=['reviews']), follow=True)
        self.assertContains(response, 'created by customers')

    def test_every_content_list_loads(self):
        for section in ['destinations', 'tours', 'categories', 'faqs', 'culture', 'reviews']:
            with self.subTest(section=section):
                self.assertEqual(self.client.get(reverse('content_list', args=[section])).status_code, 200)
                self.assertEqual(self.client.get(reverse('content_new', args=[section])).status_code,
                                 200 if section != 'reviews' else 302)
        self.assertEqual(self.client.get(reverse('site_settings')).status_code, 200)
        self.assertEqual(self.client.get(reverse('content_index')).status_code, 200)


ADMIN_PAGE_NAMES = [
    'admin_dashboard', 'admin_bookings', 'admin_payments', 'admin_customers', 'admin_create',
    'admin_inquiries', 'admin_activity', 'content_index', 'site_settings',
]


class RoleAuthenticationTests(DataMixin, TestCase):
    """One login system, two roles, two dashboards, and strict access control."""

    def setUp(self):
        cache.clear()  # sign-in throttling uses the cache; start every test clean

    def login_as(self, user, password=PASSWORD):
        return self.client.post(reverse('login'), {'username': user.username, 'password': password})

    # --- redirects after sign-in ---

    def test_client_is_redirected_to_client_dashboard(self):
        self.assertRedirects(self.login_as(self.user), reverse('dashboard'))

    def test_administrator_is_redirected_to_admin_dashboard_from_client_login(self):
        self.assertRedirects(self.login_as(self.staff), reverse('admin_dashboard'))

    def test_client_cannot_use_next_to_enter_admin_area(self):
        response = self.client.post(reverse('login') + '?next=/manage/', {'username': 'amina', 'password': PASSWORD})
        self.assertRedirects(response, reverse('dashboard'))

    def test_client_can_return_to_a_normal_page_after_login(self):
        response = self.client.post(reverse('login') + '?next=/tours/', {'username': 'amina', 'password': PASSWORD})
        self.assertRedirects(response, '/tours/')

    def test_administrator_signs_in_at_admin_page_and_lands_on_admin_dashboard(self):
        response = self.client.post(reverse('login'), {'username': 'staffer', 'password': PASSWORD})
        self.assertRedirects(response, reverse('admin_dashboard'))



    def test_admin_visiting_client_dashboard_is_redirected_to_admin_dashboard(self):
        self.client.force_login(self.staff)
        self.assertRedirects(self.client.get(reverse('dashboard')), reverse('admin_dashboard'))

    # --- access control ---

    def test_clients_cannot_open_any_admin_page(self):
        self.client.force_login(self.user)
        for name in ADMIN_PAGE_NAMES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_anonymous_visitors_cannot_open_any_admin_page(self):
        for name in ADMIN_PAGE_NAMES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('login'), response.url)

    def test_administrators_can_open_every_admin_page(self):
        self.client.force_login(self.staff)
        for name in ADMIN_PAGE_NAMES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_client_cannot_change_booking_status_or_payments(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('admin_booking_status', args=[booking.pk]),
                                          {'status': 'confirmed'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin_record_payment', args=[booking.pk]),
                                          {'amount': '5', 'method': 'cash'}).status_code, 403)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_client_cannot_broadcast_or_create_administrators(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('admin_broadcast'), {'title': 'x', 'message': 'y'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin_create'), {
            'username': 'sneaky', 'first_name': 'S', 'last_name': 'S', 'email': 's@example.com',
            'password1': 'Sneaky-pass-123', 'password2': 'Sneaky-pass-123'}).status_code, 403)
        self.assertFalse(User.objects.filter(username='sneaky').exists())

    def test_registration_cannot_make_an_administrator(self):
        self.client.post(reverse('register'), {
            'full_name': 'W B', 'email': 'w@example.com',
            'password1': 'Brand-New-pass-99', 'password2': 'Brand-New-pass-99', 'is_staff': 'on',
            'is_superuser': 'on'})
        new_user = User.objects.get(email='w@example.com')
        self.assertFalse(new_user.is_staff)
        self.assertFalse(new_user.is_superuser)

    def test_only_administrators_can_create_administrator_accounts(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_create'), {
            'username': 'newboss', 'first_name': 'New', 'last_name': 'Boss', 'email': 'boss@example.com',
            'password1': 'Boss-pass-12345', 'password2': 'Boss-pass-12345'})
        boss = User.objects.get(username='newboss')
        self.assertTrue(boss.is_staff)
        self.assertFalse(boss.is_superuser)
        self.client.logout()
        response = self.client.post(reverse('login'), {'username': 'newboss', 'password': 'Boss-pass-12345'})
        self.assertRedirects(response, reverse('admin_dashboard'))

    def test_administrator_can_grant_and_remove_admin_access(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_role', args=[self.user.pk]), {'action': 'grant'})
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.client.post(reverse('admin_customer_role', args=[self.user.pk]), {'action': 'revoke'})
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_administrator_cannot_change_own_role_or_deactivate_self(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_role', args=[self.staff.pk]), {'action': 'revoke'})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_staff)
        self.client.post(reverse('admin_customer_detail', args=[self.staff.pk]), {
            'first_name': 'S', 'last_name': 'T', 'email': 'staff@example.com', 'phone': '', 'country': ''})
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)

    # --- sign-out and password recovery for both roles ---

    def test_logout_works_for_both_roles(self):
        for user in (self.user, self.staff):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertRedirects(self.client.post(reverse('logout')), reverse('home'))
                self.assertEqual(self.client.get(reverse('profile')).status_code, 302)

    def test_logout_requires_post_for_both_roles(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)

    def test_password_reset_works_for_administrators(self):
        self.client.post(reverse('password_reset'), {'email': 'staff@example.com'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['staff@example.com'])

    def test_deactivated_account_cannot_sign_in(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_detail', args=[self.user.pk]), {
            'first_name': 'A', 'last_name': 'K', 'email': 'amina@example.com', 'is_active': '', 'phone': '', 'country': ''})
        self.client.logout()
        self.assertEqual(self.login_as(self.user).status_code, 200)

    def test_repeated_failed_sign_ins_are_throttled(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'amina', 'password': 'wrong-guess'})
        response = self.client.post(reverse('login'), {'username': 'amina', 'password': PASSWORD})
        self.assertContains(response, 'Too many failed sign-in attempts')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_admin_sign_in_is_also_throttled(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'staffer', 'password': 'wrong-guess'})
        response = self.client.post(reverse('login'), {'username': 'staffer', 'password': PASSWORD})
        self.assertContains(response, 'Too many failed sign-in attempts')

    # --- dashboards show only the right features ---

    def test_client_dashboard_has_no_admin_controls(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'My bookings')
        self.assertNotContains(response, 'Customer accounts')
        self.assertNotContains(response, reverse('admin_bookings'))
        self.assertNotContains(response, reverse('content_index'))

    def test_admin_dashboard_has_admin_controls_and_no_client_only_links(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertContains(response, 'Customer accounts')
        self.assertContains(response, reverse('admin_bookings'))
        self.assertContains(response, reverse('content_index'))

    def test_each_dashboard_has_its_own_sidebar(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('dashboard')), 'dash-sidebar')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_dashboard')), 'dash-sidebar')

    # --- client services ---

    def test_client_booking_creates_notifications_and_activity(self):
        self.client.force_login(self.user)
        self.client.post(reverse('book_package', args=[self.package.slug]),
                         self.booking_form_data(schedule=self.slot().pk))
        booking = Booking.objects.get()
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Booking request received').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.staff, title='New booking').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.user, title='New booking').exists())
        self.assertTrue(ActivityLog.objects.filter(action='booking.created', description__contains=booking.reference).exists())

    def test_customer_sees_notifications_and_can_mark_them_read(self):
        item = Notification.objects.create(recipient=self.user, title='Booking confirmed', link='/bookings/')
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('dashboard')), 'Booking confirmed')
        self.client.post(reverse('notification_read', args=[item.pk]))
        item.refresh_from_db()
        self.assertTrue(item.is_read)

    def test_customer_cannot_read_someone_elses_notification(self):
        item = Notification.objects.create(recipient=self.other, title='Private')
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('notification_read', args=[item.pk])).status_code, 404)

    def test_confirmation_notifies_the_customer(self):
        booking = self.make_booking(user=self.user)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_booking_status', args=[booking.pk]), {'status': 'confirmed'})
        self.assertTrue(Notification.objects.filter(recipient=self.user, title__icontains='confirmed').exists())
        self.assertTrue(ActivityLog.objects.filter(action='booking.status', actor=self.staff).exists())

    def test_customer_payments_and_reviews_pages_load(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('my_payments')), booking.reference)
        self.assertContains(self.client.get(reverse('my_reviews')), 'Write a review')

    def test_customer_can_contact_administration(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('contact')).status_code, 200)

    # --- administrator management ---

    def test_admin_broadcast_reaches_clients_only(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_broadcast'), {'title': 'Office closed', 'message': 'Monday holiday.'})
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Office closed').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.other, title='Office closed').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.staff, title='Office closed').exists())

    def test_admin_records_a_payment_received_directly(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED, guests=2)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_record_payment', args=[booking.pk]),
                         {'amount': '70', 'method': 'cash', 'transaction_reference': '', 'note': ''})
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal('70'))
        self.assertEqual(payment.recorded_by, self.staff)
        self.assertEqual(booking.balance_due, Decimal('0'))
        self.assertEqual(booking.payment_state, 'unpaid')  # status is still set by hand

    def test_admin_resets_a_customers_password_by_email(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_reset', args=[self.user.pk]))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['amina@example.com'])

    def test_admin_can_reopen_and_resolve_enquiries(self):
        inquiry = Inquiry.objects.create(name='Kim', email='kim@example.com', message='Hello')
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_inquiry_resolve', args=[inquiry.pk]))
        inquiry.refresh_from_db()
        self.assertTrue(inquiry.is_resolved)
        self.client.post(reverse('admin_inquiry_reopen', args=[inquiry.pk]))
        inquiry.refresh_from_db()
        self.assertFalse(inquiry.is_resolved)

    def test_admin_actions_are_recorded_in_activity_log(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_customer_role', args=[self.user.pk]), {'action': 'grant'})
        self.assertTrue(ActivityLog.objects.filter(action='admin.granted', actor=self.staff).exists())

    def test_admin_bookings_page_filters_by_status_and_search(self):
        self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.make_booking(user=self.other, status=Booking.Status.PENDING)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin_bookings'), {'status': 'confirmed'})
        self.assertEqual(response.context['page'].paginator.count, 1)
        # Both test bookings are for "Amina K", so a name search finds both.
        response = self.client.get(reverse('admin_bookings'), {'q': 'Amina'})
        self.assertEqual(response.context['page'].paginator.count, 2)
        response = self.client.get(reverse('admin_bookings'), {'q': 'no-such-person'})
        self.assertEqual(response.context['page'].paginator.count, 0)

    def test_pages_render_with_the_right_layout_for_each_role(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        # Client: own booking, notifications and profile use the client sidebar.
        self.client.force_login(self.user)
        for url in [reverse('booking_detail', args=[booking.reference]), reverse('notifications'),
                    reverse('profile'), reverse('password_change')]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'dash-sidebar')
                self.assertNotContains(response, reverse('admin_bookings'))
        # Administrator: the same pages use the admin sidebar, and the admin tools appear.
        self.client.force_login(self.staff)
        response = self.client.get(reverse('booking_detail', args=[booking.reference]))
        self.assertContains(response, 'Confirm or cancel')
        self.assertContains(response, reverse('admin_bookings'))
        self.assertEqual(self.client.get(reverse('admin_customer_detail', args=[self.user.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin_create')).status_code, 200)
        self.assertContains(self.client.get(reverse('notifications')), 'Send a notification to all clients')


class BookingCalendarTests(DataMixin, TestCase):
    """Booking calendar: availability, capacity, duplicates, registration continuity, expiry and policy."""

    def setUp(self):
        cache.clear()

    def hold(self, user, schedule, guests=1, status=Booking.Status.PENDING):
        return create_booking(user=user, schedule=schedule, guests=guests, status=status,
                              full_name='Guest', email=user.email or 'guest@example.com',
                              phone='+250700000001', country='Rwanda')

    def status_on(self, schedule):
        data = availability_payload(schedule.date.year, schedule.date.month, self.package)
        return data['days'][schedule.date.isoformat()]['status']

    def book_url(self, schedule, guests=None):
        url = f"{reverse('book_package', args=[self.package.slug])}?schedule={schedule.pk}"
        return url + (f'&guests={guests}' if guests else '')

    def post_booking(self, schedule, guests=1):
        return self.client.post(reverse('book_package', args=[self.package.slug]),
                                self.booking_form_data(schedule=schedule.pk, guests=guests))

    # --- colours and live availability ---

    def test_calendar_colours_follow_availability(self):
        open_day = self.slot(capacity=10, days=7)
        self.assertEqual(self.status_on(open_day), 'available')            # green

        low_day = self.slot(capacity=10, days=8)
        self.hold(self.other, low_day, guests=9, status=Booking.Status.CONFIRMED)
        self.assertEqual(self.status_on(low_day), 'limited')               # yellow: one seat left

        pending_day = self.slot(capacity=10, days=9)
        self.hold(self.other, pending_day, guests=1)                       # pending request
        self.assertEqual(self.status_on(pending_day), 'limited')           # yellow: pending

        full_day = self.slot(capacity=2, days=10)
        self.hold(self.other, full_day, guests=2, status=Booking.Status.CONFIRMED)
        self.assertEqual(self.status_on(full_day), 'full')                 # red

        past_day = TourSchedule.objects.create(package=self.package, date=timezone.localdate() - timedelta(days=1),
                                               start_time=clock(9, 0), capacity=5)
        self.assertEqual(self.status_on(past_day), 'past')                 # grey

    def test_calendar_page_and_live_feed_show_status(self):
        schedule = self.slot(capacity=1)
        self.client.get(reverse('tour_calendar', args=[self.package.slug]))  # warm-up, no assertion
        response = self.client.get(reverse('tour_calendar', args=[self.package.slug]),
                                   {'date': schedule.date.isoformat()})
        self.assertContains(response, 'day-limited')
        self.hold(self.other, schedule, guests=1, status=Booking.Status.CONFIRMED)
        response = self.client.get(reverse('tour_calendar', args=[self.package.slug]),
                                   {'date': schedule.date.isoformat()})
        self.assertContains(response, 'day-full')
        feed = self.client.get(reverse('tour_availability', args=[self.package.slug]),
                               {'month': schedule.date.strftime('%Y-%m')}).json()
        self.assertEqual(feed['days'][schedule.date.isoformat()]['status'], 'full')
        self.assertFalse(feed['slots'][str(schedule.pk)]['bookable'])

    def test_calendar_cannot_show_months_before_this_one(self):
        response = self.client.get(reverse('tour_calendar', args=[self.package.slug]), {'month': '2000-01'})
        self.assertEqual(response.context['cal']['month_label'], timezone.localdate().strftime('%B %Y'))

    # --- capacity and overbooking ---

    def test_booking_takes_seats_and_blocks_overbooking(self):
        schedule = self.slot(capacity=3)
        self.client.force_login(self.user)
        self.post_booking(schedule, guests=2)
        self.assertEqual(Booking.objects.filter(schedule=schedule).count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 2)

        self.client.force_login(self.other)
        response = self.post_booking(schedule, guests=2)           # only one seat is left
        self.assertContains(response, 'Only 1 seat(s) are left')
        self.assertEqual(Booking.objects.filter(schedule=schedule).count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 2)

    def test_fully_booked_slot_cannot_be_opened_or_booked(self):
        schedule = self.slot(capacity=1)
        self.hold(self.other, schedule, guests=1, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        response = self.client.get(self.book_url(schedule))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/calendar/', response.url)
        response = self.post_booking(schedule)
        self.assertContains(response, 'fully booked')
        self.assertFalse(Booking.objects.filter(user=self.user).exists())

    def test_last_seat_is_given_to_only_one_client(self):
        schedule = self.slot(capacity=1)
        first = self.hold(self.user, schedule, guests=1)
        with self.assertRaises(BookingUnavailable):
            self.hold(self.other, schedule, guests=1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 1)
        self.assertEqual(first.schedule, schedule)

    def test_database_refuses_to_store_overbooking(self):
        schedule = self.slot(capacity=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TourSchedule.objects.filter(pk=schedule.pk).update(seats_booked=5)

    def test_duplicate_booking_for_same_slot_is_refused_and_leaves_seats_unchanged(self):
        schedule = self.slot(capacity=10)
        self.hold(self.user, schedule, guests=1)
        with self.assertRaises(BookingUnavailable):
            self.hold(self.user, schedule, guests=2)
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 1)

    def test_closed_slot_cannot_be_booked(self):
        schedule = self.slot(capacity=5)
        schedule.is_open = False
        schedule.save()
        self.client.force_login(self.user)
        response = self.post_booking(schedule)
        self.assertContains(response, 'no longer open for booking')

    def test_started_slot_is_grey_and_not_bookable(self):
        started = TourSchedule.objects.create(package=self.package, date=timezone.localdate(),
                                              start_time=clock(0, 0), capacity=5)
        self.assertEqual(self.status_on(started), 'none')

    # --- selection kept through registration and sign-in ---

    def test_visitor_selects_a_slot_registers_and_continues(self):
        schedule = self.slot(capacity=5)
        response = self.client.get(self.book_url(schedule, guests=2))
        self.assertEqual(response.url, reverse('booking_start'))        # nothing booked yet
        page = self.client.get(reverse('booking_start'))
        self.assertContains(page, 'create my account')
        self.assertContains(page, schedule.package.title)

        response = self.client.post(reverse('register') + f"?next={reverse('booking_resume')}", {
            'full_name': 'New Bie', 'email': 'newbie@example.com',
            'password1': 'Brand-New-pass-99', 'password2': 'Brand-New-pass-99'})
        self.assertRedirects(response, reverse('verify_email'))
        # The visitor is signed in only after the emailed code is accepted, then returns to the booking.
        response = self.client.post(reverse('verify_email'), code_post(emailed_code(mail.outbox[-1])))
        self.assertEqual(response.url, reverse('booking_resume'))

        response = self.client.get(reverse('booking_resume'))
        self.assertIn(f'schedule={schedule.pk}', response.url)
        self.assertIn('guests=2', response.url)
        form_page = self.client.get(response.url)
        self.assertContains(form_page, 'Complete your booking')
        self.assertContains(form_page, f'value="{schedule.pk}"')

        response = self.client.post(reverse('book_package', args=[self.package.slug]),
                                    self.booking_form_data(schedule=schedule.pk, guests=2))
        booking = Booking.objects.get(user__email='newbie@example.com')
        self.assertRedirects(response, booking.get_absolute_url())
        self.assertEqual(booking.schedule, schedule)
        self.assertNotIn('booking_intent', self.client.session)

    def test_existing_client_signs_in_and_continues(self):
        schedule = self.slot(capacity=5)
        self.client.get(self.book_url(schedule))
        response = self.client.post(reverse('login') + f"?next={reverse('booking_resume')}",
                                    {'username': 'amina', 'password': PASSWORD})
        self.assertEqual(response.url, reverse('booking_resume'))
        response = self.client.get(reverse('booking_resume'))
        self.assertIn(f'schedule={schedule.pk}', response.url)

    def test_resume_rechecks_availability_after_sign_in(self):
        schedule = self.slot(capacity=1)
        self.client.get(self.book_url(schedule))
        self.hold(self.other, schedule, guests=1, status=Booking.Status.CONFIRMED)  # filled meanwhile
        self.client.post(reverse('login') + f"?next={reverse('booking_resume')}",
                         {'username': 'amina', 'password': PASSWORD})
        response = self.client.get(reverse('booking_resume'))
        self.assertIn('/calendar/', response.url)
        self.assertNotIn('booking_intent', self.client.session)

    def test_remembered_selection_expires_after_two_hours(self):
        schedule = self.slot(capacity=5)
        self.client.get(self.book_url(schedule))
        session = self.client.session
        session['booking_intent']['saved_at'] -= 3 * 60 * 60
        session.save()
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get(reverse('booking_resume')), reverse('packages'))

    # --- pending reservations and expiry ---

    def test_pending_request_expires_and_frees_its_seats(self):
        schedule = self.slot(capacity=1)
        request = self.hold(self.user, schedule, guests=1)
        self.assertIsNotNone(request.expires_at)
        Booking.objects.filter(pk=request.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(expire_stale_holds(), 1)
        request.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(request.status, Booking.Status.EXPIRED)
        self.assertEqual(schedule.seats_booked, 0)
        self.hold(self.other, schedule, guests=1)                     # the seat can be sold again

    def test_confirmed_booking_is_never_expired(self):
        schedule = self.slot(capacity=2)
        booking = self.hold(self.user, schedule, guests=1, status=Booking.Status.CONFIRMED)
        Booking.objects.filter(pk=booking.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(expire_stale_holds(), 0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)

    def test_expire_command_runs(self):
        call_command('expire_holds', verbosity=0)

    # --- administrator and client actions change seats ---

    def test_admin_confirm_keeps_seats_and_cancel_frees_them(self):
        schedule = self.slot(capacity=2)
        booking = self.hold(self.user, schedule, guests=2)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_booking_status', args=[booking.pk]), {'status': 'confirmed'})
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 2)
        self.client.post(reverse('admin_booking_status', args=[booking.pk]), {'status': 'cancelled'})
        schedule.refresh_from_db()
        self.assertEqual(schedule.seats_booked, 0)

    def test_cancelled_booking_cannot_be_reopened_when_the_slot_is_full(self):
        schedule = self.slot(capacity=2)
        booking = self.hold(self.user, schedule, guests=2)
        set_booking_status(booking, Booking.Status.CANCELLED)
        self.hold(self.other, schedule, guests=2)                     # someone else takes the seats
        with self.assertRaises(BookingUnavailable):
            set_booking_status(booking, Booking.Status.CONFIRMED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    def test_customer_cancel_releases_seats_within_policy(self):
        schedule = self.slot(capacity=2, days=7)
        booking = self.hold(self.user, schedule, guests=2)
        self.client.force_login(self.user)
        self.client.post(reverse('cancel_booking', args=[booking.reference]))
        booking.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(schedule.seats_booked, 0)

    def test_customer_cannot_cancel_inside_the_notice_period(self):
        schedule = self.slot(capacity=2, days=1)                      # tomorrow: inside the 48-hour notice
        booking = self.hold(self.user, schedule, guests=1)
        self.client.force_login(self.user)
        self.client.post(reverse('cancel_booking', args=[booking.reference]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    # --- administrator schedule management ---

    def test_admin_adds_a_date_and_capacity_cannot_drop_below_bookings(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_schedule_new'), {
            'package': self.package.pk, 'date': (timezone.localdate() + timedelta(days=12)).isoformat(),
            'start_time': '10:30', 'capacity': 5, 'is_open': 'on', 'notes': ''})
        schedule = TourSchedule.objects.get(start_time=clock(10, 30))
        self.hold(self.other, schedule, guests=3)
        self.client.post(reverse('admin_schedule_edit', args=[schedule.pk]), {
            'package': self.package.pk, 'date': schedule.date.isoformat(), 'start_time': '10:30',
            'capacity': 2, 'is_open': 'on', 'notes': ''})
        schedule.refresh_from_db()
        self.assertEqual(schedule.capacity, 5)

    def test_admin_bulk_adds_every_chosen_weekday(self):
        self.client.force_login(self.staff)
        start = timezone.localdate() + timedelta(days=1)
        self.client.post(reverse('admin_schedule_bulk'), {
            'package': self.package.pk, 'start_date': start.isoformat(),
            'end_date': (start + timedelta(days=13)).isoformat(), 'weekdays': ['5'],
            'start_time': '09:00', 'capacity': 8, 'is_open': 'on'})
        self.assertEqual(TourSchedule.objects.filter(package=self.package, start_time=clock(9, 0),
                                                     capacity=8).count(), 2)

    def test_deleting_a_date_with_bookings_is_refused(self):
        schedule = self.slot(capacity=5)
        self.hold(self.user, schedule, guests=1)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_schedule_delete', args=[schedule.pk]), follow=True)
        self.assertTrue(TourSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertContains(response, 'cannot be deleted')

    def test_moving_a_date_moves_its_bookings(self):
        schedule = self.slot(capacity=5, days=7)
        booking = self.hold(self.user, schedule, guests=1)
        new_day = timezone.localdate() + timedelta(days=20)
        self.client.force_login(self.staff)
        self.client.post(reverse('admin_schedule_edit', args=[schedule.pk]), {
            'package': self.package.pk, 'date': new_day.isoformat(), 'start_time': '09:00',
            'capacity': 5, 'is_open': 'on', 'notes': ''})
        booking.refresh_from_db()
        self.assertEqual(booking.travel_date, new_day)

    def test_administrator_pages_render_and_clients_are_refused(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('admin_schedules')).status_code, 403)
        self.assertEqual(self.client.get(reverse('admin_calendar')).status_code, 403)
        self.client.force_login(self.staff)
        schedule = self.slot(capacity=4)
        self.hold(self.user, schedule, guests=2, status=Booking.Status.CONFIRMED)
        self.assertEqual(self.client.get(reverse('admin_schedules')).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin_schedule_bulk')).status_code, 200)
        response = self.client.get(reverse('admin_calendar'), {
            'tour': self.package.pk, 'month': schedule.date.strftime('%Y-%m'),
            'date': schedule.date.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1 booking')   # the day shows its bookings
        self.assertContains(response, 'Guest')       # and the customer on each one (set by hold())
        self.assertContains(self.client.get(reverse('admin_dashboard')), 'Booking calendar')

    def test_client_booking_pages_show_the_slot_and_policy(self):
        schedule = self.slot(capacity=4)
        booking = self.hold(self.user, schedule, guests=1)
        self.client.force_login(self.user)
        response = self.client.get(booking.get_absolute_url())
        self.assertContains(response, 'Seats held until')
        self.assertContains(response, 'Cancel this booking')


class FooterAndIntegrationTests(DataMixin, TestCase):
    """Footer content and the whole site working together: links, roles, uploads and live updates."""

    LINK_RE = re.compile(r'href="(/[^"#?]*)')

    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media_root)
        self._media.enable()

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def png(self, name='logo.png'):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), (18, 61, 50)).save(buffer, format='PNG')
        return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')

    def site(self):
        return SiteSettings.load()

    def cohost_form(self, **overrides):
        data = {'name': 'Kigali Hills Travel', 'logo': self.png(), 'description': 'Local partner',
                'website_url': 'https://partner.example.com', 'order': 1, 'is_active': 'on'}
        data.update(overrides)
        return data

    # --- social media ---

    def test_social_icons_appear_only_when_links_are_set(self):
        self.assertNotContains(self.client.get(reverse('home')), 'Follow us')
        SocialLink.objects.create(platform='facebook', url='https://facebook.com/explorerwanda')
        SocialLink.objects.create(platform='instagram', url='https://instagram.com/explorerwanda')
        SocialLink.objects.create(platform='x', url='https://x.com/explorerwanda')
        inactive = SocialLink.objects.create(platform='tiktok', url='https://tiktok.com/@hidden', is_active=False)
        page = self.client.get(reverse('home'))
        self.assertContains(page, 'Follow us')
        self.assertContains(page, 'href="https://facebook.com/explorerwanda"')
        self.assertContains(page, 'href="https://instagram.com/explorerwanda"')
        self.assertContains(page, 'href="https://x.com/explorerwanda"')
        self.assertNotContains(page, 'tiktok.com/@hidden')  # inactive accounts are hidden
        inactive.delete()

    def test_social_links_open_in_a_new_tab(self):
        SocialLink.objects.create(platform='x', url='https://x.com/explorerwanda')
        html = self.client.get(reverse('home')).content.decode()
        anchor = re.search(r'<a class="social-link social-x"[^>]*>', html).group(0)
        self.assertIn('target="_blank"', anchor)
        self.assertIn('rel="noopener noreferrer"', anchor)
        self.assertIn('aria-label="X (formerly Twitter) (opens in a new tab)"', anchor)

    def test_footer_is_on_public_and_dashboard_pages(self):
        self.assertContains(self.client.get(reverse('contact')), 'site-footer')
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('dashboard')), 'site-footer')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_dashboard')), 'site-footer')

    # --- hosted by ---

    def test_hosted_by_appears_with_name_logo_and_link(self):
        self.assertNotContains(self.client.get(reverse('home')), 'Hosted by')
        site = self.site()
        site.hosted_by_name = 'Kigali Web Studio'
        site.hosted_by_url = 'https://studio.example.com'
        site.hosted_by_logo = self.png()
        site.save()
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn('Hosted by', page)
        self.assertIn('Kigali Web Studio', page)
        self.assertIn('href="https://studio.example.com"', page)
        self.assertRegex(page, r'<img class="partner-logo" src="/media/site/[^"]+" alt="Kigali Web Studio logo"')

    def test_administrator_updates_contact_social_and_hosting_from_the_control_panel(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('contact_manage')).status_code, 200)
        self.client.post(reverse('contact_manage'), {
            'contact_intro': 'Write to us any time.', 'contact_email': 'hello@example.com',
            'contact_phone': '+250 788 000 000', 'contact_address': 'KG 1 Ave, Kigali',
            'whatsapp_number': '+250 788 000 000', 'payment_phone': '+250 788 111 222'})
        self.client.post(reverse('site_settings'), {
            'site_name': 'Explore Rwanda', 'footer_tagline': 'Tagline', 'hero_eyebrow': 'h', 'hero_title': 't',
            'hero_text': 'x', 'intro_kicker': 'k', 'intro_title': 'i', 'intro_text': 'x', 'culture_intro': 'c',
            'history_note': 'h', 'booking_notice': 'n', 'final_cta_title': 'f', 'final_cta_text': 'f',
            'payment_instructions': 'Pay by phone.', 'staff_notification_email': '',
            'hosted_by_name': 'Kigali Web Studio', 'hosted_by_url': ''})
        self.client.post(reverse('content_new', args=['social']),
                         {'platform': 'facebook', 'label': '', 'url': 'facebook.com/x', 'order': 0, 'is_active': 'on'})
        self.client.logout()
        site = self.site()
        self.assertEqual(site.whatsapp_number, '250788000000')  # digits only are stored
        self.assertEqual(SocialLink.objects.get().url, 'https://facebook.com/x')
        page = self.client.get(reverse('home'))
        self.assertContains(page, 'Kigali Web Studio')
        self.assertContains(page, '+250 788 000 000')
        self.assertContains(page, 'href="https://facebook.com/x"')

    # --- co-hosts ---

    def test_no_cohosts_means_the_footer_section_is_hidden(self):
        self.assertNotContains(self.client.get(reverse('home')), 'Co-hosts')

    def test_administrator_adds_edits_and_deletes_cohosts(self):
        self.client.force_login(self.staff)
        with self.settings(MEDIA_ROOT=self.media_root):
            self.client.post(reverse('content_new', args=['cohosts']), self.cohost_form())
        cohost = CoHost.objects.get(name='Kigali Hills Travel')
        self.assertTrue(cohost.logo.name.startswith('cohosts/'))
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn('Co-hosts', page)
        self.assertIn('Kigali Hills Travel', page)
        self.assertIn('href="https://partner.example.com"', page)
        self.assertRegex(page, r'src="/media/cohosts/[^"]+" alt="Kigali Hills Travel logo"')

        edit = self.client.post(reverse('content_edit', args=['cohosts', cohost.pk]),
                                self.cohost_form(is_active=''))      # deactivate
        self.assertEqual(edit.status_code, 302, 'the edit form did not save')
        cohost.refresh_from_db()
        self.assertFalse(cohost.is_active)
        self.client.logout()  # a fresh visit: the success message from the edit is not shown here
        self.assertNotContains(self.client.get(reverse('home')), 'Kigali Hills Travel')
        self.client.force_login(self.staff)

        self.client.post(reverse('content_delete', args=['cohosts', cohost.pk]))
        self.assertFalse(CoHost.objects.filter(pk=cohost.pk).exists())

    def test_several_cohosts_are_listed_in_order(self):
        with self.settings(MEDIA_ROOT=self.media_root):
            CoHost.objects.create(name='Beta Partner', order=2)
            CoHost.objects.create(name='Alpha Partner', order=1)
        page = self.client.get(reverse('home')).content.decode()
        self.assertLess(page.index('Alpha Partner'), page.index('Beta Partner'))

    def test_cohost_upload_rejects_files_that_are_not_images(self):
        self.client.force_login(self.staff)
        fake = SimpleUploadedFile('logo.png', b'this is not an image', content_type='image/png')
        with self.settings(MEDIA_ROOT=self.media_root):
            self.client.post(reverse('content_new', args=['cohosts']), self.cohost_form(logo=fake))
        self.assertFalse(CoHost.objects.exists())

    def test_cohost_upload_rejects_wrong_extension_and_oversized_files(self):
        self.client.force_login(self.staff)
        with self.settings(MEDIA_ROOT=self.media_root):
            exe = SimpleUploadedFile('logo.exe', b'MZ', content_type='application/octet-stream')
            self.client.post(reverse('content_new', args=['cohosts']), self.cohost_form(logo=exe))
            big = SimpleUploadedFile('big.png', b'0' * (5 * 1024 * 1024 + 10), content_type='image/png')
            self.client.post(reverse('content_new', args=['cohosts']), self.cohost_form(logo=big))
        self.assertFalse(CoHost.objects.exists())

    # --- the site as one system ---

    def test_footer_destinations_come_from_the_database(self):
        self.assertContains(self.client.get(reverse('home')), self.destination.name)

    def test_contact_details_update_the_footer_everywhere(self):
        site = self.site()
        site.contact_phone = '+250 700 999 888'
        site.save()
        for url in [reverse('home'), reverse('about'), reverse('faq')]:
            with self.subTest(page=url):
                self.assertContains(self.client.get(url), '+250 700 999 888')

    def test_payment_phone_appears_for_clients(self):
        site = self.site()
        site.payment_phone = '+250 700 555 444'
        site.save()
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(booking.get_absolute_url()), '+250 700 555 444')
        self.assertContains(self.client.get(reverse('my_payments')), '+250 700 555 444')

    def test_admin_changes_reach_the_public_site(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('content_edit', args=['tours', self.package.pk]), {
            'title': 'Kigali Highlights Deluxe', 'slug': self.package.slug, 'category': self.category.pk,
            'destinations': [self.destination.pk], 'summary': 'New summary', 'description': 'Desc',
            'highlights': '', 'price': '40.00', 'duration_days': 1, 'max_guests': 4, 'is_active': 'on'})
        self.assertContains(self.client.get(reverse('packages')), 'Kigali Highlights Deluxe')
        self.assertContains(self.client.get(self.package.get_absolute_url()), 'USD 40.00')

    def test_booking_shows_on_client_and_admin_dashboards(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.PENDING)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('dashboard')), booking.reference)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('admin_dashboard')), booking.reference)
        self.assertContains(self.client.get(reverse('admin_bookings')), self.user.email)

    def crawl(self, client, start_urls):
        """Follow every internal link reachable from the start pages. Returns the broken ones."""
        seen, broken, queue = set(), [], list(start_urls)
        while queue and len(seen) < 400:
            url = queue.pop()
            if url in seen:
                continue
            seen.add(url)
            response = client.get(url)
            if response.status_code in (301, 302):
                continue
            if response.status_code >= 400:
                broken.append((url, response.status_code))
                continue
            for link in self.LINK_RE.findall(response.content.decode('utf-8', 'ignore')):
                if link not in seen and not link.startswith(('/static/', '/media/')):
                    queue.append(link)
        return broken, seen

    def test_every_internal_link_works_for_visitors(self):
        starts = [reverse(name) for name in ['home', 'about', 'faq', 'contact', 'destinations', 'packages',
                                             'login', 'register', 'password_reset']]
        starts.append(self.package.get_absolute_url())
        starts.append(reverse('tour_calendar', args=[self.package.slug]))
        broken, seen = self.crawl(self.client, starts)
        self.assertEqual(broken, [])
        self.assertGreater(len(seen), 10)

    def test_every_internal_link_works_for_clients(self):
        self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.user)
        starts = [reverse(name) for name in ['dashboard', 'booking_history', 'my_payments', 'my_reviews',
                                             'notifications', 'profile', 'password_change', 'contact']]
        broken, _ = self.crawl(self.client, starts)
        self.assertEqual(broken, [])

    def test_every_internal_link_works_for_administrators(self):
        booking = self.make_booking(user=self.user, status=Booking.Status.CONFIRMED)
        self.client.force_login(self.staff)
        starts = [reverse(name) for name in [
            'admin_dashboard', 'admin_bookings', 'admin_payments', 'admin_customers', 'admin_create',
            'admin_inquiries', 'admin_activity', 'admin_schedules', 'admin_calendar', 'admin_schedule_bulk',
            'content_index', 'site_settings', 'notifications', 'profile']]
        starts += [reverse('content_list', args=[s]) for s in
                   ['destinations', 'tours', 'categories', 'reviews', 'faqs', 'culture', 'cohosts']]
        starts += [reverse('booking_detail', args=[booking.reference]),
                   reverse('admin_customer_detail', args=[self.user.pk])]
        broken, _ = self.crawl(self.client, starts)
        self.assertEqual(broken, [])

