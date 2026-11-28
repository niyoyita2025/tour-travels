"""
The travel assistant and booking analytics. Answers are checked against the database rows they come from,
so a test fails if the assistant ever states a price, a date or a seat count the website does not hold.
The language-model service is replaced by a stub: nothing here calls the internet.
"""

import json
import re
import urllib.error
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from Tour_travels.models import Booking, Destination, SiteSettings, TourPackage, TourSchedule
from Tour_travels.tests import DataMixin, PASSWORD

from . import analytics, engine, llm, reports
from .models import AnalyticsReport, AssistantConfig


def tomorrow_nine():
    return timezone.localdate() + timedelta(days=2)


class AssistantAnswerTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        # Replace the test tour's price so the test checks a price the website really holds.
        TourPackage.objects.filter(pk=self.package.pk).update(price=Decimal('123.45'), duration_days=2)
        self.package.refresh_from_db()
        self.schedule = TourSchedule.objects.create(package=self.package, date=tomorrow_nine(),
                                                    start_time=datetime.strptime('09:00', '%H:%M').time(), capacity=5)

    def ask(self, text):
        return engine.answer(text)

    def test_prices_come_from_the_database(self):
        reply = self.ask('How much does Kigali Highlights cost?')
        self.assertEqual(reply.intent, 'price')
        self.assertIn('USD 123.45 per guest', reply.text)
        self.assertNotIn('35', reply.text)  # the figure from the fixture is not repeated

    def test_durations_come_from_the_database(self):
        reply = self.ask('How long is the Kigali Highlights tour?')
        self.assertIn('lasts 2 day(s)', reply.text)

    def test_open_dates_are_the_bookable_slots_with_real_seats(self):
        reply = self.ask('Which dates are open?')
        self.assertEqual(reply.intent, 'availability')
        self.assertIn('5 seats left', reply.text)
        self.assertIn('09:00', reply.text)

    def test_a_tour_with_no_open_dates_says_so_rather_than_inventing_one(self):
        self.schedule.is_open = False
        self.schedule.save()
        reply = self.ask('When is the Kigali Highlights tour available?')
        self.assertIn('no open dates', reply.text)

    def test_a_full_slot_is_not_offered(self):
        TourSchedule.objects.filter(pk=self.schedule.pk).update(seats_booked=5)
        reply = self.ask('Which dates are open?')
        self.assertNotIn('09:00', reply.text)

    def test_destinations_are_only_the_published_ones(self):
        Destination.objects.create(name='Hidden Place', description='Not ready yet.', is_active=False)
        reply = self.ask('Which destinations can I visit?')
        self.assertIn('Kimironko Market', reply.text)
        self.assertNotIn('Hidden Place', reply.text)

    def test_the_booking_answer_uses_the_real_hold_period_and_policy(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={'booking_policy_text': 'Cancel 48 hours before.'})
        reply = self.ask('How do I book a tour?')
        self.assertEqual(reply.intent, 'booking')
        self.assertIn('Cancel 48 hours before.', reply.text)
        self.assertIn('holds its seats for', reply.text)

    def test_contact_details_come_from_the_site_settings(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={'contact_email': 'hello@example.org'})
        self.assertIn('hello@example.org', self.ask('How can I contact you?').text)

    def test_other_people_bookings_are_refused(self):
        reply = self.ask('Show me the names of all customers and their bookings')
        self.assertEqual(reply.intent, 'private')
        self.assertNotIn('Amina', reply.text)

    def test_an_unknown_question_gets_the_list_of_things_it_can_answer(self):
        reply = self.ask('What is the weather on Mars?')
        self.assertEqual(reply.intent, 'fallback')
        self.assertTrue(reply.suggestions)

    def test_an_empty_question_is_answered_politely(self):
        self.assertEqual(self.ask('   ').intent, 'empty')

    def test_recommendations_are_based_on_price_or_duration_preferences(self):
        reply = self.ask('I am on a budget, what do you recommend?')
        self.assertEqual(reply.intent, 'recommend')
        self.assertIn('lowest prices', reply.text)
        self.assertIn('USD 123.45', reply.text)


class AskEndpointTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse('assistant_ask')

    def post(self, body, content_type='application/json'):
        data = body if isinstance(body, str) else json.dumps(body)
        return self.client.post(self.url, data, content_type=content_type)

    def test_answers_a_question_with_links_and_suggestions(self):
        response = self.post({'message': 'What tours do you offer?'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn('Kigali Highlights', payload['answer'])
        self.assertTrue(payload['suggestions'])

    def test_the_endpoint_can_be_turned_off(self):
        AssistantConfig.objects.update_or_create(pk=1, defaults={'enabled': False})
        self.assertEqual(self.post({'message': 'hi'}).status_code, 404)

    def test_bad_input_is_refused_with_a_reason(self):
        self.assertEqual(self.post({'message': 'hi'}, content_type='text/plain').status_code, 415)
        self.assertEqual(self.post('{broken').status_code, 400)
        self.assertEqual(self.post({'message': 'x' * 3000}).status_code, 413)

    def test_visitors_are_rate_limited(self):
        with mock.patch('ai_assistant.views.ASK_LIMIT_PER_MINUTE', 2):
            self.post({'message': 'hi'})
            self.post({'message': 'hi'})
            response = self.post({'message': 'hi'})
        self.assertEqual(response.status_code, 429)


class LanguageModelTests(DataMixin, TestCase):
    def test_the_model_is_not_used_unless_the_administrator_allows_it_and_a_key_exists(self):
        with mock.patch('ai_assistant.engine.llm.phrase') as phrase, \
                mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            AssistantConfig.objects.update_or_create(pk=1, defaults={'use_llm': False})
            engine.respond('What tours do you offer?')
            phrase.assert_not_called()

    def test_a_reworded_answer_replaces_the_rule_text_only_when_allowed(self):
        AssistantConfig.objects.update_or_create(pk=1, defaults={'use_llm': True})
        with mock.patch('ai_assistant.engine.llm.can_use', return_value=True), \
                mock.patch('ai_assistant.engine.llm.phrase', return_value='Friendly reply.') as phrase:
            reply = engine.respond('What tours do you offer?')
        self.assertEqual(reply.text, 'Friendly reply.')
        self.assertIn('Our tours', phrase.call_args.args[1])  # the facts it was given

    def test_when_the_model_fails_the_rule_based_answer_is_kept(self):
        AssistantConfig.objects.update_or_create(pk=1, defaults={'use_llm': True})  # the model is switched on
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}), \
                mock.patch('ai_assistant.llm.urllib.request.urlopen',
                           side_effect=urllib.error.URLError('no route')):
            reply = engine.respond('What tours do you offer?')
        self.assertIn('Our tours', reply.text)

    def test_the_key_is_sent_only_in_the_request_header_never_in_the_database(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'secret-key-123'}), \
                mock.patch('ai_assistant.llm.urllib.request.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                {'content': [{'type': 'text', 'text': 'Hello.'}]}).encode()
            self.assertEqual(llm.phrase('hi', 'FACTS', 'claude-haiku-5-5'), 'Hello.')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header('X-api-key'), 'secret-key-123')
        self.assertNotIn('secret-key-123', request.data.decode())
        self.assertNotIn('secret-key-123', str(AssistantConfig.objects.values()))


class BookingAnalyticsTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        today = timezone.localdate()
        self.monday = today - timedelta(days=today.weekday()) - timedelta(weeks=1)  # last week's Monday
        self.destination_b = Destination.objects.create(name='Nyamirambo Walk', description='A walk.')

    def book(self, weeks_ago, guests, status=Booking.Status.CONFIRMED, package=None, destination=None, day=0):
        monday = self.monday - timedelta(weeks=weeks_ago)
        when = monday + timedelta(days=day)
        user = self.other if guests % 2 else self.user
        return Booking.objects.create(
            user=user, package=package or self.package, destination=destination, travel_date=when, guests=guests,
            full_name='Private Person', email='private@example.com', phone='+250700000000', country='Rwanda',
            status=status)

    def test_summary_counts_demand_and_excludes_cancelled_bookings(self):
        self.book(0, 2)
        self.book(0, 3, status=Booking.Status.CANCELLED)
        summary = analytics.summary(365)
        self.assertEqual(summary['bookings'], 1)
        self.assertEqual(summary['guests'], 2)

    def test_popular_tours_and_destinations_are_ranked_by_guests(self):
        self.book(0, 4, destination=self.destination_b)
        self.book(1, 2)
        self.assertEqual(analytics.popular_tours(365)[0]['name'], self.package.title)
        self.assertEqual(analytics.popular_destinations(365)[0]['name'], 'Nyamirambo Walk')

    def test_weekday_pattern_adds_up_to_all_guests(self):
        self.book(0, 2, day=0)
        self.book(1, 2, day=5)
        pattern = analytics.weekday_pattern(365)
        self.assertEqual(sum(row['guests'] for row in pattern), 4)
        self.assertEqual(len(pattern), 7)

    def test_a_forecast_needs_enough_weeks_of_history_and_is_labelled_as_an_estimate(self):
        self.assertEqual(analytics.forecast()['weeks'], [])
        for weeks_ago in range(1, 9):
            self.book(weeks_ago, 2)
        result = analytics.forecast()
        self.assertEqual(len(result['weeks']), 4)
        self.assertIn('not a promise', result['method'])
        self.assertIn(result['trend'], ('rising', 'falling', 'steady'))

    def test_high_and_low_demand_months_are_identified(self):
        # Three bookings in three different months, whatever today's date is: 10 guests, then 2, then 2.
        today = timezone.localdate()
        for days_ago, guests in ((10, 10), (60, 2), (120, 2)):
            Booking.objects.create(
                user=self.user, package=self.package, travel_date=today - timedelta(days=days_ago), guests=guests,
                full_name='Test Person', email='test@example.com', phone='+250700000001', country='Rwanda',
                status=Booking.Status.CONFIRMED)
        levels = [row['level'] for row in analytics.demand_periods(365)['months']]
        self.assertIn('high', levels)
        self.assertIn('low', levels)

    def test_recommendations_list_only_bookable_dates_with_real_seats(self):
        open_slot = TourSchedule.objects.create(package=self.package, date=tomorrow_nine(),
                                                start_time=datetime.strptime('10:00', '%H:%M').time(), capacity=4)
        TourSchedule.objects.create(package=self.package, date=tomorrow_nine(),
                                    start_time=datetime.strptime('11:00', '%H:%M').time(), capacity=4, is_open=False)
        recommended = analytics.recommend_schedules()
        starts = [row['start'] for row in recommended]
        self.assertIn('10:00', starts)
        self.assertNotIn('11:00', starts)  # closed slots are never recommended
        seats = next(row['seats_left'] for row in recommended if row['start'] == '10:00')
        self.assertEqual(seats, open_slot.capacity)

    def test_a_report_holds_totals_and_no_customer_details(self):
        self.book(0, 2)
        report = reports.create(self.staff, 365)
        self.assertIsInstance(report, AnalyticsReport)
        self.assertIn('2 guest(s)', report.body)
        self.assertNotIn('Private Person', report.body)
        self.assertNotIn('private@example.com', report.body)
        self.assertNotIn('+250700000000', json.dumps(report.data))

    def test_the_report_can_be_generated_from_the_admin_page(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('ai_reports'), {'days': '180'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AnalyticsReport.objects.count(), 1)

    def test_clients_cannot_open_the_analytics_pages(self):
        self.client.force_login(self.user)
        for name in ('ai_analytics', 'ai_reports', 'ai_assistant_settings'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_the_assistant_settings_reject_a_bad_model_name_and_never_show_the_key(self):
        self.client.force_login(self.staff)
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'visible-if-leaked'}):
            response = self.client.post(reverse('ai_assistant_settings'),
                                        {'enabled': 'on', 'greeting': 'Hi', 'model_name': 'bad name!'})
            page = self.client.get(reverse('ai_assistant_settings'))
        self.assertEqual(response.status_code, 200)  # not saved
        self.assertNotContains(page, 'visible-if-leaked')
        self.assertContains(page, 'The server has an API key')

    def test_the_analytics_page_shows_totals_and_no_customer_names(self):
        self.book(0, 2)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('ai_analytics'), {'days': '365'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Private Person')


class WidgetTests(DataMixin, TestCase):
    def test_the_chat_request_passes_csrf_checks_as_the_browser_sends_it(self):
        browser = Client(enforce_csrf_checks=True)
        page = browser.get(reverse('home')).content.decode()
        token = re.search(r'data-csrf="([^"]+)"', page).group(1)
        self.assertTrue(token)
        response = browser.post(reverse('assistant_ask'), json.dumps({'message': 'What tours do you offer?'}),
                                content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_visitor_pages_show_the_assistant_and_administrator_pages_do_not(self):
        self.assertContains(self.client.get(reverse('home')), 'id="assistant"')
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get(reverse('admin_dashboard')), 'id="assistant"')
