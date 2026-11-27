"""
Tests for automatic tour timetables: start times every interval, one group per start time,
overlaps by tour length, daily guest capacity, group sizes, suggestions on the calendar, and the
Automatic timetables pages for administrators.
"""

from datetime import date, timedelta
from datetime import time as clock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import scheduling
from .bookings import BookingUnavailable, create_booking, expire_stale_holds, set_booking_status
from .calendar_service import schedules_in_range
from .models import Booking, ScheduleRule, TourSchedule
from .tests import DataMixin


class SchedulingTestCase(DataMixin, TestCase):
    """A tour that runs 08:00 to 17:00 every 3 hours, takes 3-hour tours, and groups of 2 to 4."""

    def setUp(self):
        cache.clear()
        self.package.min_guests = 2
        self.package.max_guests = 4
        self.package.save()
        self.rule = ScheduleRule.objects.create(
            package=self.package, default_start=clock(8, 0), last_start=clock(17, 0),
            interval_hours=3, tour_hours=3, daily_capacity=12, days_ahead=30)
        self.day = timezone.localdate() + timedelta(days=3)
        scheduling.generate_window(self.package, self.rule)

    def use_interval(self, hours):
        self.rule.interval_hours = hours
        self.rule.save()
        scheduling.sync_rule(self.package)

    def slot(self, hour, day=None):
        return TourSchedule.objects.get(package=self.package, date=day or self.day, start_time=clock(hour, 0))

    def book(self, hour, guests=3, user=None, day=None):
        return create_booking(user=user or self.user, schedule=self.slot(hour, day), guests=guests,
                              duration_hours=self.rule.tour_hours, full_name='Amina K',
                              email='amina@example.com', phone='+250700000000', country='Rwanda')

    def post_booking(self, hour, **data):
        self.client.force_login(self.user)
        payload = {'schedule': self.slot(hour).pk, 'guests': 2, 'duration_hours': 1,
                   'destination': self.destination.pk, 'full_name': 'Amina K', 'email': 'amina@example.com',
                   'phone': '+250700000000', 'country': 'Rwanda', 'special_requests': '', 'confirm': '1'}
        payload.update(data)
        return self.client.post(reverse('book_package', args=[self.package.slug]), payload)

    def timetable_data(self, **overrides):
        data = {'min_guests': 2, 'max_guests': 4, 'default_start': '08:00', 'last_start': '17:00',
                'interval_hours': 3, 'tour_hours': 3, 'weekdays': ['0', '1', '2', '3', '4', '5', '6'],
                'daily_capacity': 12, 'days_ahead': 30}
        data.update(overrides)
        return data


# ---------- the timetable itself ----------

class TimetableTests(SchedulingTestCase):
    def test_start_times_run_from_the_first_to_the_last_every_interval(self):
        self.assertEqual(self.rule.start_times(), [clock(8, 0), clock(11, 0), clock(14, 0), clock(17, 0)])

    def test_a_two_hour_interval_gives_more_start_times(self):
        self.rule.interval_hours = 2
        self.assertEqual([t.hour for t in self.rule.start_times()], [8, 10, 12, 14, 16])

    def test_times_are_opened_for_every_start_time_on_the_operating_days_only(self):
        TourSchedule.objects.filter(package=self.package).delete()
        self.rule.operating_days = '0,1,2,3,4'  # Monday to Friday
        self.rule.days_ahead = 14
        monday = date(2030, 1, 7)  # a Monday, so the 14 days hold exactly ten weekdays
        added = scheduling.generate_window(self.package, self.rule, today=monday)
        self.assertEqual(added, 10 * 4)
        created = TourSchedule.objects.filter(package=self.package)
        self.assertTrue(all(s.date.weekday() < 5 for s in created))
        self.assertTrue(all(s.group_only for s in created))
        self.assertTrue(all(s.capacity == self.package.max_guests for s in created))

    def test_opening_the_same_window_again_adds_nothing(self):
        self.assertEqual(scheduling.generate_window(self.package, self.rule), 0)

    def test_changing_the_interval_closes_only_open_times_that_no_longer_fit(self):
        self.book(11, guests=2, user=self.other)  # 11:00 is booked, so it stays even off the new grid
        self.rule.interval_hours = 2
        self.rule.save()
        scheduling.sync_rule(self.package)
        starts = set(TourSchedule.objects.filter(package=self.package, date=self.day)
                     .values_list('start_time', flat=True))
        self.assertIn(clock(11, 0), starts)  # booked: never removed
        self.assertNotIn(clock(17, 0), starts)  # open and off the new grid: removed
        self.assertIn(clock(10, 0), starts)  # new start time on the 2-hour grid: opened

    def test_a_new_group_size_applies_to_open_times(self):
        self.package.max_guests = 6
        self.package.save()
        scheduling.sync_rule(self.package)
        self.assertEqual(self.slot(14).capacity, 6)

    def test_the_last_start_cannot_be_before_the_first(self):
        bad = ScheduleRule(package=self.package, default_start=clock(10, 0), last_start=clock(9, 0))
        with self.assertRaises(ValidationError):
            bad.full_clean()


# ---------- booking rules ----------

class RuleBookingTests(SchedulingTestCase):
    def test_a_group_closes_the_start_time_and_overlapping_starts(self):
        self.book(8, guests=3)
        by_time = {s.start_time.hour: s for s in schedules_in_range(self.day, self.day, self.package)}
        self.assertEqual(by_time[8].seats_remaining, 0)  # taken by the group
        self.assertTrue(by_time[11].is_bookable)  # the tour ends at 11:00, so 11:00 is free
        self.assertFalse(by_time[11].reserved_by_time)

    def test_a_start_that_runs_into_another_tour_is_refused_by_the_server(self):
        self.use_interval(2)  # starts at 08:00, 10:00, 12:00 ...; the 08:00 tour lasts until 11:00
        self.book(8, guests=3)
        with self.assertRaisesMessage(BookingUnavailable, 'already reserved'):
            self.book(10, guests=2, user=self.other)
        self.book(12, guests=2, user=self.other)  # 12:00 starts after the tour has ended

    def test_a_second_group_cannot_take_a_time_already_taken(self):
        self.book(8, guests=3)
        with self.assertRaisesMessage(BookingUnavailable, 'another group'):
            self.book(8, guests=2, user=self.other)

    def test_the_suggested_time_is_the_first_free_start_after_a_tour(self):
        self.book(8, guests=3, user=self.other)
        self.assertEqual(scheduling.first_fitting_slot(self.package, self.day, 2).start_time, clock(11, 0))
        self.assertIsNone(scheduling.first_fitting_slot(self.package, self.day, 5))  # no group is bigger than 4

    def test_the_daily_guest_capacity_refuses_a_group_that_would_exceed_it(self):
        self.rule.daily_capacity = 6
        self.rule.save()
        self.book(8, guests=4)
        with self.assertRaisesMessage(BookingUnavailable, 'no room left'):
            self.book(11, guests=3, user=self.other)
        self.book(11, guests=2, user=self.other)  # 4 + 2 is exactly the daily capacity

    def test_cancelling_a_group_frees_its_start_time_and_its_guests(self):
        booking = self.book(8, guests=4)
        set_booking_status(booking, Booking.Status.CANCELLED)
        self.assertEqual(self.slot(8).seats_remaining, 4)
        self.assertEqual(scheduling.first_fitting_slot(self.package, self.day, 4).start_time, clock(8, 0))

    def test_an_expired_hold_frees_its_start_time(self):
        booking = self.book(8, guests=4)
        Booking.objects.filter(pk=booking.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(expire_stale_holds(), 1)
        self.assertEqual(self.slot(8).seats_remaining, 4)

    def test_clients_pay_for_the_tour_length_not_a_length_they_choose(self):
        response = self.post_booking(8, guests=2, duration_hours=1)
        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get()
        self.assertEqual(booking.duration_hours, self.rule.tour_hours)

    def test_clients_must_book_at_least_the_smallest_group(self):
        response = self.post_booking(8, guests=1)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'needs at least 2 guests')
        self.assertFalse(Booking.objects.exists())

    def test_clients_cannot_book_more_than_the_largest_group(self):
        response = self.post_booking(8, guests=5)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Booking.objects.exists())

    def test_a_client_booking_takes_the_time_and_the_guests_are_recorded(self):
        self.client.force_login(self.user)
        self.post_booking(11, guests=3)
        booking = Booking.objects.get()
        self.assertEqual(booking.schedule.start_time, clock(11, 0))
        self.assertEqual(booking.guests, 3)
        self.assertEqual(self.slot(11).seats_booked, 3)

    def test_visiting_the_calendar_opens_more_times_when_the_window_runs_short(self):
        TourSchedule.objects.filter(package=self.package, group_only=True).delete()
        self.client.get(reverse('tour_calendar', args=[self.package.slug]))
        self.assertTrue(TourSchedule.objects.filter(package=self.package, group_only=True, date=self.day).exists())


# ---------- the calendar ----------

class CalendarSuggestionTests(SchedulingTestCase):
    def calendar(self, guests=2):
        return self.client.get(reverse('tour_calendar', args=[self.package.slug]),
                               {'date': self.day.isoformat(), 'guests': guests})

    def test_the_calendar_suggests_the_next_time_that_fits_the_group(self):
        self.book(8, guests=3, user=self.other)
        response = self.calendar(guests=2)
        self.assertEqual(response.context['suggested']['time'], '11:00')
        self.assertContains(response, 'Suggested for 2 guests')

    def test_times_that_overlap_a_tour_are_shown_but_cannot_be_booked(self):
        self.use_interval(2)
        self.book(8, guests=3, user=self.other)
        response = self.calendar(guests=2)
        by_time = {slot['time']: slot for slot in response.context['selected_day']['slots']}
        self.assertFalse(by_time['10:00']['bookable'])  # runs into the 08:00 tour
        self.assertFalse(by_time['08:00']['bookable'])  # taken by a group
        self.assertTrue(by_time['12:00']['bookable'])
        self.assertNotIn('book_url', by_time['10:00'])
        self.assertEqual(response.context['suggested']['time'], '12:00')

    def test_a_booking_link_carries_the_group_size(self):
        response = self.calendar(guests=2)
        by_time = {slot['time']: slot for slot in response.context['selected_day']['slots']}
        self.assertTrue(by_time['11:00']['book_url'].endswith('&guests=2'))

    def test_the_group_size_picker_offers_the_tours_limits(self):
        response = self.calendar(guests=3)
        self.assertEqual(list(response.context['guest_options']), [2, 3, 4])
        self.assertContains(response, 'name="guests"')
        self.assertContains(response, '<option value="3" selected>')


# ---------- administrators ----------

class TimetableAdminTests(SchedulingTestCase):
    def test_staff_can_open_the_timetables_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin_scheduling'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.package.title)

    def test_clients_cannot_open_the_timetables_page(self):
        self.client.force_login(self.user)
        self.assertNotEqual(self.client.get(reverse('admin_scheduling')).status_code, 200)

    def test_saving_a_timetable_opens_its_start_times(self):
        TourSchedule.objects.filter(package=self.package).delete()
        ScheduleRule.objects.filter(package=self.package).delete()
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_scheduling_edit', args=[self.package.pk]),
                                    self.timetable_data(interval_hours=2, min_guests=3, max_guests=4))
        self.assertRedirects(response, reverse('admin_scheduling'))
        self.assertEqual(ScheduleRule.objects.get(package=self.package).interval_hours, 2)
        self.package.refresh_from_db()
        self.assertEqual(self.package.min_guests, 3)
        self.assertTrue(TourSchedule.objects.filter(package=self.package, start_time=clock(10, 0),
                                                    group_only=True).exists())

    def test_the_last_start_cannot_be_before_the_first_on_the_form(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_scheduling_edit', args=[self.package.pk]),
                                    self.timetable_data(last_start='07:00'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cannot be earlier than the first start time')

    def test_the_smallest_group_cannot_be_larger_than_the_largest(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_scheduling_edit', args=[self.package.pk]),
                                    self.timetable_data(min_guests=4, max_guests=2))
        self.assertContains(response, 'smallest group cannot be larger')

    def test_the_daily_capacity_must_hold_a_largest_group(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_scheduling_edit', args=[self.package.pk]),
                                    self.timetable_data(daily_capacity=3))
        self.assertContains(response, 'must hold at least one largest group')

    def test_stopping_keeps_booked_times_and_removes_open_ones(self):
        booked = self.slot(8)
        self.book(8, guests=2)
        self.client.force_login(self.staff)
        response = self.client.post(reverse('admin_scheduling_off', args=[self.package.pk]))
        self.assertRedirects(response, reverse('admin_scheduling'))
        self.assertFalse(ScheduleRule.objects.filter(package=self.package).exists())
        self.assertTrue(TourSchedule.objects.filter(pk=booked.pk).exists())
        self.assertFalse(TourSchedule.objects.filter(package=self.package, start_time=clock(14, 0),
                                                     date=self.day).exists())
