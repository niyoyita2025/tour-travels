"""
Separate Destinations and Tours navigation and pages, the dark/light mode, Google Translate,
and category chips on the Tours page.
"""

from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .models import Category, TourPackage
from .tests import DataMixin


class NavigationTests(DataMixin, TestCase):
    def test_destinations_and_tours_are_separate_links_in_the_menu(self):
        html = self.client.get(reverse('home')).content.decode()
        self.assertIn('class="nav-destinations"', html)
        self.assertIn('class="nav-tours"', html)
        self.assertIn(f'href="{reverse("destinations")}"', html)
        self.assertIn(f'href="{reverse("packages")}"', html)
        self.assertNotIn('Tours &amp; places', html)

    def test_destinations_link_opens_the_destinations_page_and_tours_opens_tours(self):
        destinations = self.client.get(reverse('destinations')).content.decode()
        tours = self.client.get(reverse('packages')).content.decode()
        self.assertIn('class="page-destinations"', destinations)
        self.assertIn('class="page-tours"', tours)


class ToursPageTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.slot(capacity=10, days=7)

    def test_tours_page_is_booking_first_with_its_own_hero(self):
        response = self.client.get(reverse('packages'))
        self.assertContains(response, 'tour-hero')
        self.assertContains(response, 'Book Now')
        self.assertNotContains(response, 'Places to visit')


class CategoryChipTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.slot(capacity=10, days=7)
        self.other_category = Category.objects.create(name='City Walks')
        self.other_tour = TourPackage.objects.create(
            title='Kigali City Walk', summary='City', description='d', price=20, category=self.other_category)

    def test_every_category_with_tours_is_a_chip_with_its_count(self):
        response = self.client.get(reverse('packages'))
        self.assertContains(response, 'class="cat-chip')
        self.assertContains(response, self.category.name)
        self.assertContains(response, self.other_category.name)
        self.assertContains(response, f'?category={self.other_category.pk}')

    def test_clicking_a_chip_shows_only_that_category(self):
        response = self.client.get(reverse('packages'), {'category': self.other_category.pk})
        titles = [t.title for t in response.context['page'].object_list]
        self.assertEqual(titles, ['Kigali City Walk'])
        self.assertEqual(response.context['active_category'], self.other_category)
        self.assertContains(response, 'cat-chip is-active')

    def test_empty_categories_are_not_shown_as_chips(self):
        Category.objects.create(name='Empty Category')
        response = self.client.get(reverse('packages'))
        names = [c.name for c in response.context['category_chips']]
        self.assertNotIn('Empty Category', names)
        self.assertIn(self.category.name, names)

    def test_a_new_category_with_a_tour_appears_as_a_chip(self):
        new = Category.objects.create(name='Adventure Trips')
        TourPackage.objects.create(title='Mountain Hike', summary='Hike', description='d', price=40, category=new)
        self.assertContains(self.client.get(reverse('packages')), 'Adventure Trips')


class ThemeAndTranslateTests(DataMixin, TestCase):
    def test_every_page_has_the_dark_light_toggle_and_the_theme_script(self):
        html = self.client.get(reverse('home')).content.decode()
        self.assertIn('id="themeToggle"', html)
        self.assertIn("localStorage.getItem('er-theme')", html)
        self.assertIn('prefers-color-scheme: dark', html)
        self.assertIn('css/theme.css', html)
        self.assertIn('data-theme="light"', html)

    def test_dark_theme_stylesheet_is_available(self):
        path = finders.find('css/theme.css')
        self.assertIsNotNone(path)
        with open(path, encoding='utf-8') as css:
            self.assertIn('html[data-theme="dark"]', css.read())

    def test_every_page_offers_google_translate_with_the_supported_languages(self):
        for name in ['home', 'packages', 'destinations', 'contact']:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertIn('id="google_translate_element"', html)
                self.assertIn('translate.google.com/translate_a/element.js', html)
                self.assertIn("includedLanguages: 'en,rw,fr,sw'", html)

    def test_translate_banner_is_hidden_so_the_layout_does_not_shift(self):
        with open(finders.find('css/theme.css'), encoding='utf-8') as css:
            self.assertIn('goog-te-banner-frame', css.read())

    def test_theme_and_translate_are_also_on_the_dashboards(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('id="themeToggle"', html)
        self.assertIn('google_translate_element', html)
        self.assertIn('has-dashboard', html)
