"""
Every destination gets its own bookable tour automatically. It is created when the administrator adds the
destination (Control Panel or Django admin), keeps open dates ahead, is left alone when the destination is
edited, and goes with the destination only when nobody has booked it and it visits no other place.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .bookings import create_booking
from .destination_tours import ensure_destination_tour
from .models import Booking, Destination, TourPackage, TourSchedule
from .tests import DataMixin

NEW_PLACE = {
    'name': 'Lake Kivu', 'slug': '', 'location': 'Rusizi, Rwanda', 'description': 'Calm water and fishing villages.',
    'map_link': '', 'image_url': '', 'is_active': 'on',
}


def auto_tours():
    return TourPackage.objects.filter(auto_for__isnull=False)


class AutoTourCreationTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)

    def add_place_in_control_panel(self, **overrides):
        data = dict(NEW_PLACE, **overrides)
        return self.client.post(reverse('content_new', args=['destinations']), data, follow=True)

    def test_adding_a_destination_creates_its_bookable_tour_with_open_dates(self):
        response = self.add_place_in_control_panel()
        destination = Destination.objects.get(name='Lake Kivu')
        tour = TourPackage.objects.get(auto_for=destination)
        self.assertEqual(tour.title, 'Visit Lake Kivu')
        self.assertEqual(list(tour.destinations.all()), [destination])
        self.assertTrue(tour.is_active)
        self.assertEqual(tour.price, Decimal(settings.AUTO_TOUR_PRICE))
        self.assertEqual(tour.max_guests, settings.AUTO_TOUR_MAX_GUESTS)

        dates = TourSchedule.objects.filter(package=tour).order_by('date')
        self.assertEqual(dates.count(), settings.AUTO_TOUR_DAYS_AHEAD)
        self.assertEqual(dates.first().date, timezone.localdate())
        self.assertEqual(dates.first().capacity, settings.AUTO_TOUR_CAPACITY)
        self.assertContains(response, 'bookable tour')

    def test_the_new_tour_is_listed_with_book_now_and_its_own_page(self):
        self.add_place_in_control_panel()
        tour = TourPackage.objects.get(auto_for__name='Lake Kivu')
        self.assertContains(self.client.get(reverse('packages')), 'Visit Lake Kivu')
        page = self.client.get(tour.get_absolute_url())
        self.assertContains(page, 'Open dates and times')
        self.assertContains(page, 'Book Now')
        self.assertContains(page, 'Lake Kivu')

    def test_clients_can_book_the_new_tour_straight_away(self):
        self.add_place_in_control_panel()
        destination = Destination.objects.get(name='Lake Kivu')
        tour = TourPackage.objects.get(auto_for=destination)
        slot = TourSchedule.objects.get(package=tour, date=timezone.localdate() + timedelta(days=3))
        self.client.force_login(self.user)
        self.client.post(reverse('book_package', args=[tour.slug]), self.booking_form_data(
            schedule=slot.pk, destination=destination.pk, confirm='1'))
        booking = Booking.objects.get(package=tour)
        self.assertEqual(booking.destination, destination)
        self.assertEqual(booking.schedule, slot)

    def test_adding_through_the_django_admin_also_creates_the_tour(self):
        boss = get_user_model().objects.create_superuser(
            username='boss', email='boss@example.com', password='Sup3r-Secret-pass-2')
        self.client.force_login(boss)
        self.client.post(reverse('admin:Tour_travels_destination_add'), {
            'name': 'Nyungwe Forest', 'slug': '', 'location': 'Southern Province', 'country': 'Rwanda', 'description': 'Canopy walk.',
            'map_link': '', 'image_url': '', 'is_featured': '', 'is_active': 'on'})
        destination = Destination.objects.get(name='Nyungwe Forest')
        tour = TourPackage.objects.get(auto_for=destination)
        self.assertEqual(tour.title, 'Visit Nyungwe Forest')
        self.assertEqual(TourSchedule.objects.filter(package=tour).count(), settings.AUTO_TOUR_DAYS_AHEAD)

    def test_editing_a_destination_does_not_create_a_second_tour(self):
        self.add_place_in_control_panel()
        destination = Destination.objects.get(name='Lake Kivu')
        self.client.post(reverse('content_edit', args=['destinations', destination.pk]), dict(
            NEW_PLACE, name='Lake Kivu Shore', slug=destination.slug))
        self.assertEqual(auto_tours().filter(auto_for=destination).count(), 1)


class AutoTourEditingTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.tour = ensure_destination_tour(self.destination)

    def edit_destination(self, **changes):
        data = {'name': self.destination.name, 'slug': self.destination.slug, 'location': self.destination.location,
                'description': self.destination.description, 'map_link': '', 'image_url': '', 'is_active': 'on'}
        data.update(changes)
        return self.client.post(reverse('content_edit', args=['destinations', self.destination.pk]), data)

    def test_editing_a_destination_leaves_its_tour_unchanged(self):
        before = (self.tour.title, self.tour.summary, self.tour.description, self.tour.price)
        self.edit_destination(name='Kimironko Renamed', location='Gasabo', description='Completely new text.')
        self.destination.refresh_from_db()
        self.assertEqual(self.destination.name, 'Kimironko Renamed')
        self.tour.refresh_from_db()
        self.assertEqual((self.tour.title, self.tour.summary, self.tour.description, self.tour.price), before)
        self.assertEqual(self.tour.auto_for, self.destination)
        self.assertEqual(list(self.tour.destinations.all()), [self.destination])

    def test_changes_made_to_the_tour_survive_editing_the_destination(self):
        self.tour.title = 'Sunrise Walk'
        self.tour.save()
        self.edit_destination(name='Kimironko Renamed')
        self.tour.refresh_from_db()
        self.assertEqual(self.tour.title, 'Sunrise Walk')


class AutoTourDeletionTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.spare = Destination.objects.create(name='Spare Place', description='Not used.')
        self.tour = ensure_destination_tour(self.spare)

    def delete_in_control_panel(self):
        return self.client.post(reverse('content_delete', args=['destinations', self.spare.pk]), follow=True)

    def test_an_unbooked_destination_and_its_automatic_tour_are_deleted_together(self):
        self.delete_in_control_panel()
        self.assertFalse(Destination.objects.filter(pk=self.spare.pk).exists())
        self.assertFalse(TourPackage.objects.filter(pk=self.tour.pk).exists())
        self.assertFalse(TourSchedule.objects.filter(package_id=self.tour.pk).exists())

    def test_a_destination_whose_tour_has_bookings_cannot_be_deleted(self):
        slot = TourSchedule.objects.get(package=self.tour, date=timezone.localdate() + timedelta(days=3))
        create_booking(user=self.user, schedule=slot, guests=1, duration_hours=2,
                       status=Booking.Status.CONFIRMED, full_name='Amina K', email='amina@example.com',
                       phone='+250700000000', country='Rwanda')
        response = self.delete_in_control_panel()
        self.assertTrue(Destination.objects.filter(pk=self.spare.pk).exists())
        self.assertTrue(TourPackage.objects.filter(pk=self.tour.pk).exists())
        self.assertContains(response, 'cannot be deleted')

    def test_a_tour_that_also_visits_another_place_keeps_the_destination(self):
        self.tour.destinations.add(self.destination)
        self.delete_in_control_panel()
        self.assertTrue(Destination.objects.filter(pk=self.spare.pk).exists())
        self.assertTrue(TourPackage.objects.filter(pk=self.tour.pk).exists())

    def test_a_hand_made_tour_that_visits_the_destination_keeps_it(self):
        self.package.destinations.add(self.spare)
        self.delete_in_control_panel()
        self.assertTrue(Destination.objects.filter(pk=self.spare.pk).exists())

    def test_deleting_from_the_django_admin_removes_the_automatic_tour_too(self):
        boss = get_user_model().objects.create_superuser(
            username='boss', email='boss@example.com', password='Sup3r-Secret-pass-2')
        self.client.force_login(boss)
        self.client.post(reverse('admin:Tour_travels_destination_delete', args=[self.spare.pk]), {'post': 'yes'})
        self.assertFalse(Destination.objects.filter(pk=self.spare.pk).exists())
        self.assertFalse(TourPackage.objects.filter(pk=self.tour.pk).exists())


class OpenDatesTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()

    def test_dates_are_opened_for_the_configured_number_of_days(self):
        tour = ensure_destination_tour(self.destination)
        self.assertEqual(TourSchedule.objects.filter(package=tour).count(), settings.AUTO_TOUR_DAYS_AHEAD)

    def test_a_tour_whose_dates_have_run_out_gets_new_dates_when_visited(self):
        tour = ensure_destination_tour(self.destination)
        TourSchedule.objects.filter(package=tour).delete()
        self.client.get(tour.get_absolute_url())
        self.assertEqual(TourSchedule.objects.filter(package=tour).count(), settings.AUTO_TOUR_DAYS_AHEAD)

    def test_the_calendar_also_opens_dates_for_automatic_tours(self):
        tour = ensure_destination_tour(self.destination)
        TourSchedule.objects.filter(package=tour).delete()
        self.client.get(reverse('tour_calendar', args=[tour.slug]))
        self.assertEqual(TourSchedule.objects.filter(package=tour).count(), settings.AUTO_TOUR_DAYS_AHEAD)

    def test_dates_are_not_duplicated_when_topped_up(self):
        tour = ensure_destination_tour(self.destination)
        count = TourSchedule.objects.filter(package=tour).count()
        ensure_destination_tour(self.destination)
        self.client.get(tour.get_absolute_url())
        self.assertEqual(TourSchedule.objects.filter(package=tour).count(), count)

    def test_hand_made_tours_are_not_given_dates_automatically(self):
        self.client.get(self.package.get_absolute_url())
        self.assertFalse(TourSchedule.objects.filter(package=self.package).exists())


class OpenDestinationToursCommandTests(DataMixin, TestCase):
    def test_every_destination_gets_a_bookable_tour_and_running_it_again_changes_nothing(self):
        Destination.objects.create(name='Lake Kivu', description='Lakeside.')
        out = StringIO()
        call_command('open_destination_tours', stdout=out)
        self.assertEqual(auto_tours().count(), 2)  # Kimironko Market and Lake Kivu
        self.assertIn('2 tours created', out.getvalue())
        dates = TourSchedule.objects.filter(package__auto_for__isnull=False).count()
        self.assertEqual(dates, 2 * settings.AUTO_TOUR_DAYS_AHEAD)

        again = StringIO()
        call_command('open_destination_tours', stdout=again)
        self.assertEqual(auto_tours().count(), 2)
        self.assertEqual(TourSchedule.objects.filter(package__auto_for__isnull=False).count(), dates)
        self.assertIn('0 tours created', again.getvalue())
