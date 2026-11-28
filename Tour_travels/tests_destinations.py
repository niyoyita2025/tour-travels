"""
Destinations and Tours are separate sections: separate pages, navigation, designs and management.
They are connected only where needed: a tour lists the places it visits, and a booking records the
destination the client chose.
"""

from datetime import time as clock, timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from django.utils.html import escape

from .bookings import create_booking
from .calendar_service import next_bookable_slots, upcoming_bookable_slots
from .models import Booking, Destination, TourPackage, TourSchedule
from .tests import DataMixin


class DestinationsSectionTests(DataMixin, TestCase):
    """Destinations: places to explore, with maps and details. No tour booking here."""

    def setUp(self):
        cache.clear()
        self.slot(capacity=10, days=7)

    def test_destinations_page_shows_images_names_locations_maps_and_details_buttons(self):
        response = self.client.get(reverse('destinations'))
        self.assertContains(response, self.destination.name)
        self.assertContains(response, self.destination.location)
        self.assertContains(response, 'dest-tile-img')
        self.assertContains(response, escape(self.destination.map_url))
        self.assertContains(response, '>Details</a>')
        self.assertContains(response, self.destination.get_absolute_url())

    def test_destinations_page_has_no_tour_cards_or_categories(self):
        # The header's "Book Now" link is on every page; the destinations list itself shows places only.
        response = self.client.get(reverse('destinations'))
        for text in ['seats left', 'No open dates', 'cat-chip', 'slot-chip', 'Book this tour',
                     self.category.name, self.package.title]:
            with self.subTest(text=text):
                self.assertNotContains(response, text)

    def test_destination_page_describes_the_place_and_shows_its_map(self):
        response = self.client.get(self.destination.get_absolute_url())
        self.assertContains(response, self.destination.description)
        self.assertContains(response, 'maps.google.com/maps?')
        self.assertContains(response, 'Open in Google Maps')

    def test_destination_page_lists_its_published_tours_with_prices_and_booking_links(self):
        self.package.destinations.add(self.destination)
        hidden = TourPackage.objects.create(title='Hidden Tour', summary='s', description='d', price=5,
                                            is_active=False)
        hidden.destinations.add(self.destination)
        response = self.client.get(self.destination.get_absolute_url())
        self.assertContains(response, f'Tours to {self.destination.name}')
        self.assertContains(response, self.package.title)
        self.assertContains(response, f'USD {self.package.price} / guest')
        self.assertContains(response, reverse('tour_calendar', args=[self.package.slug]))
        self.assertContains(response, 'Book this tour')
        self.assertNotContains(response, 'Hidden Tour')

    def test_destination_page_without_tours_has_no_tour_section(self):
        self.destination.packages.clear()
        response = self.client.get(self.destination.get_absolute_url())
        self.assertNotContains(response, 'Book this tour')

    def test_destination_map_link_can_be_set_by_the_administrator(self):
        self.destination.map_link = 'https://maps.google.com/?cid=123'
        self.destination.save()
        self.assertEqual(self.destination.map_url, 'https://maps.google.com/?cid=123')
        self.assertContains(self.client.get(reverse('destinations')), 'https://maps.google.com/?cid=123')

    def test_destination_search_still_works(self):
        response = self.client.get(reverse('destinations'), {'q': 'kimironko'})
        self.assertContains(response, self.destination.name)
        response = self.client.get(reverse('destinations'), {'q': 'zzz-nothing'})
        self.assertContains(response, 'No destinations match')


class ToursSectionTests(DataMixin, TestCase):
    """Tours: booking-first. Places are described only on the tour page, with links to the destinations."""

    def setUp(self):
        cache.clear()
        self.slot(capacity=10, days=7)

    def test_tours_page_shows_categories_images_prices_and_book_now(self):
        response = self.client.get(reverse('packages'))
        self.assertContains(response, 'Book Now')
        self.assertContains(response, self.category.name)
        self.assertContains(response, 'tour-card-img')
        self.assertContains(response, 'USD 35.00')
        self.assertContains(response, 'Tours and open dates')

    def test_tours_page_has_no_destination_section(self):
        response = self.client.get(reverse('packages'))
        self.assertNotContains(response, 'Places to visit')
        self.assertNotContains(response, 'dest-tile')
        self.assertNotContains(response, 'Visits:')

    def test_tour_page_links_to_the_places_it_visits(self):
        response = self.client.get(self.package.get_absolute_url())
        self.assertContains(response, 'Places this tour visits')
        self.assertContains(response, self.destination.get_absolute_url())

    def test_tour_page_has_its_own_booking_and_open_dates(self):
        slot = self.slot(capacity=10, days=8)
        response = self.client.get(self.package.get_absolute_url())
        self.assertContains(response, 'Open dates and times')
        self.assertContains(response, f'?date={slot.date.isoformat()}')
        self.assertContains(response, 'Book Now')

    def test_booking_does_not_need_the_destinations_page_first(self):
        slot = self.slot(capacity=5, days=9)
        self.client.force_login(self.user)
        form = self.client.get(f"{reverse('book_package', args=[self.package.slug])}?schedule={slot.pk}")
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, 'Complete your booking')

    def test_next_open_date_skips_closed_full_and_past(self):
        closed = self.slot(capacity=5, days=7)
        closed.is_open = False
        closed.save()
        full = self.slot(capacity=1, days=8)
        create_booking(user=self.other, schedule=full, guests=1, duration_hours=2,
                       status=Booking.Status.CONFIRMED, full_name='X', email='x@example.com', phone='1', country='RW')
        open_day = self.slot(capacity=5, days=9)
        past = TourSchedule.objects.create(package=self.package, date=timezone.localdate() - timedelta(days=2),
                                           start_time=clock(9, 0), capacity=5)
        best = next_bookable_slots([self.package.pk])
        self.assertEqual(best[self.package.pk], open_day)
        self.assertEqual([s.pk for s in upcoming_bookable_slots(self.package)], [open_day.pk])
        self.assertNotIn(past.pk, [s.pk for s in upcoming_bookable_slots(self.package)])


class BookingDestinationTests(DataMixin, TestCase):
    """The client chooses the destination inside the booking form. The tour and destination stay separate."""

    def setUp(self):
        cache.clear()
        self.slot_ = self.slot(capacity=5, days=7)
        self.client.force_login(self.user)

    def booking_url(self):
        return reverse('book_package', args=[self.package.slug])

    def test_booking_form_lists_the_places_the_tour_visits(self):
        other_place = Destination.objects.create(name='Lake Kivu', description='Lakeside.')
        response = self.client.get(f"{self.booking_url()}?schedule={self.slot_.pk}")
        self.assertContains(response, 'Destination you want to visit')
        choices = list(response.context['form'].fields['destination'].queryset)
        self.assertEqual(choices, [self.destination])  # only the places this tour visits
        self.assertNotIn(other_place, choices)

    def test_destination_is_required_when_the_tour_has_places(self):
        response = self.client.post(self.booking_url(), {
            'schedule': self.slot_.pk, 'guests': 1, 'duration_hours': 2, 'full_name': 'Amina K',
            'email': 'amina@example.com', 'phone': '+250700000000', 'country': 'Rwanda',
            'special_requests': '', 'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Booking.objects.exists())

    def test_chosen_destination_is_saved_on_the_booking_and_shown(self):
        self.client.post(self.booking_url(), self.booking_form_data(
            schedule=self.slot_.pk, destination=self.destination.pk))
        booking = Booking.objects.get()
        self.assertEqual(booking.destination, self.destination)
        self.assertContains(self.client.get(booking.get_absolute_url()), self.destination.name)

    def test_summary_shows_the_chosen_destination_before_saving(self):
        response = self.client.post(self.booking_url(), self.booking_form_data(
            schedule=self.slot_.pk, destination=self.destination.pk, confirm=''))
        self.assertContains(response, 'Review your booking')
        self.assertContains(response, self.destination.name)
        self.assertFalse(Booking.objects.exists())

    def test_a_tour_without_places_lets_the_client_choose_any_destination(self):
        lonely = TourPackage.objects.create(title='Solo Walk', summary='s', description='d', price=15)
        slot = TourSchedule.objects.create(package=lonely, date=timezone.localdate() + timedelta(days=12),
                                           start_time=clock(9, 0), capacity=5)
        response = self.client.get(f"{reverse('book_package', args=[lonely.slug])}?schedule={slot.pk}")
        self.assertContains(response, 'Destination you want to visit')
        self.client.post(reverse('book_package', args=[lonely.slug]), self.booking_form_data(
            schedule=slot.pk, destination=''))
        self.assertTrue(Booking.objects.filter(package=lonely).exists())


class SeparateAdministrationTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)

    def test_updating_a_destination_does_not_change_its_tours(self):
        self.client.post(reverse('content_edit', args=['destinations', self.destination.pk]), {
            'name': 'Kimironko Market Renamed', 'slug': self.destination.slug, 'location': 'Kimironko',
            'description': 'New text.', 'map_link': '', 'image_url': '', 'is_active': 'on'})
        self.destination.refresh_from_db()
        self.assertEqual(self.destination.name, 'Kimironko Market Renamed')
        self.assertEqual(list(self.package.destinations.all()), [self.destination])
        self.package.refresh_from_db()
        self.assertEqual(self.package.title, 'Kigali Highlights')

    def test_a_destination_used_by_tours_cannot_be_deleted(self):
        response = self.client.post(reverse('content_delete', args=['destinations', self.destination.pk]),
                                    follow=True)
        self.assertTrue(Destination.objects.filter(pk=self.destination.pk).exists())
        self.assertTrue(TourPackage.objects.filter(pk=self.package.pk).exists())
        self.assertContains(response, 'cannot be deleted')

    def test_an_unused_destination_can_be_deleted(self):
        spare = Destination.objects.create(name='Spare Place', description='Not used.')
        self.client.post(reverse('content_delete', args=['destinations', spare.pk]))
        self.assertFalse(Destination.objects.filter(pk=spare.pk).exists())

    def test_tours_are_managed_with_their_places_separately(self):
        self.client.post(reverse('content_new', args=['tours']), {
            'title': 'Lake Day', 'slug': '', 'category': self.category.pk, 'destinations': [self.destination.pk],
            'summary': 'Lake', 'description': 'Full day.', 'highlights': '', 'price': '60.00',
            'duration_days': 1, 'max_guests': 6, 'is_active': 'on'})
        tour = TourPackage.objects.get(title='Lake Day')
        self.assertEqual(list(tour.destinations.all()), [self.destination])

    def test_management_areas_are_separate_in_the_sidebar(self):
        page = self.client.get(reverse('admin_dashboard')).content.decode()
        self.assertIn('Destination Management', page)
        self.assertIn('Tour Management', page)
        self.assertIn('Booking Management', page)
