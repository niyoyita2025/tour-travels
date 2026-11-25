"""
Load the Explore Rwanda landing-page content into the database.

Safe to run more than once: records are matched by slug and updated in place.
Prices and FAQ answers are sample values; edit them in the admin before launch.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from Tour_travels.destination_tours import ensure_destination_tour
from Tour_travels.models import (
    FAQ, AboutSection, Category, CultureItem, Destination, DetailsPage, SiteSettings, TourPackage,
)

TOUR_PLACES = {
    'kigali-highlights': ['imbuga-city-walk', 'kimironko-market'],
    'kigali-full-day-discovery': ['nyamirambo', 'kigali-genocide-memorial', 'mu-marangi-biryogo'],
    'custom-rwanda-experience': ['fazenda-sengha', 'kigali-genocide-memorial'],
}

ABOUT_SECTIONS = [
    ('Choose a tour', 'Browse our tours or tell us your interests.'),
    ('Pick a date and time', 'Open the booking calendar, choose a date, a starting time and how many hours you want.'),
    ('Confirm and pay', 'We confirm availability. Payment is arranged directly with us, and your status appears on your booking.'),
    ('Enjoy your visit', 'Meet your guide and explore Kigali at your own pace.'),
]

CULTURE = [
    ('🗣️', 'Language', 'Kinyarwanda is the national language. Try “Muraho” for hello and “Murakoze” for thank you.'),
    ('🥘', 'Traditional food', 'Learn about foods such as isombe, ibitoke, beans and sweet potatoes.'),
    ('🥁', 'Dance & music', 'Discover Intore, a well-known Rwandan dance tradition with coordinated movement and music.'),
    ('🤝', 'Community', 'Explore values of hospitality, respect, community responsibility and national unity.'),
]

U = 'https://images.unsplash.com/'

DESTINATIONS = [
    ('kigali-genocide-memorial', 'Kigali Genocide Memorial', 'Gisozi, Kigali',
     'A place of remembrance and education honouring victims of the 1994 Genocide against the Tutsi.',
     U + 'photo-1590736969955-71cc94901144?auto=format&fit=crop&w=700&q=80', True),
    ('belgian-peacekeepers-memorial', 'Belgian Peacekeepers Memorial', 'Camp Kigali, Kigali',
     'Learn about the ten Belgian UN peacekeepers killed at Camp Kigali on 7 April 1994.',
     U + 'photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=700&q=80', False),
    ('imbuga-city-walk', 'Imbuga City Walk', 'City centre, Kigali',
     'Enjoy a pedestrian-friendly public space for walking, relaxing and seeing the modern city.',
     U + 'photo-1519501025264-65ba15a82390?auto=format&fit=crop&w=700&q=80', True),
    ('nyamirambo', 'Nyamirambo', 'Nyamirambo, Kigali',
     'Explore a lively neighbourhood known for local shops, food, fashion and community life.',
     U + 'photo-1516026672322-bc52d61a55d5?auto=format&fit=crop&w=700&q=80', True),
    ('mu-marangi-biryogo', 'Mu Marangi – Biryogo', 'Biryogo, Kigali',
     'Experience colourful pedestrian streets, local food businesses and a lively atmosphere.',
     U + 'photo-1517248135467-4c7edcad34c4?auto=format&fit=crop&w=700&q=80', False),
    ('kimironko-market', 'Kimironko Market', 'Kimironko, Kigali',
     'Discover fresh produce, colourful fabrics, clothing and the energy of local trading.',
     U + 'photo-1488459716781-31db52582fe9?auto=format&fit=crop&w=700&q=80', False),
    ('niyo-arts-center', 'Niyo Arts Center', 'Kigali',
     'Discover contemporary African art, creative expression and community initiatives.',
     U + 'photo-1579783902614-a3fb3927b6a5?auto=format&fit=crop&w=700&q=80', False),
    ('fazenda-sengha', 'Fazenda Sengha', 'Mount Kigali, Kigali',
     'Enjoy outdoor recreation, horse riding and views around Mount Kigali.',
     U + 'photo-1473448912268-2022ce9509d8?auto=format&fit=crop&w=700&q=80', False),
]

CATEGORIES = [
    ('City Tours', 'Walking and driving tours of Kigali’s landmarks and neighbourhoods.'),
    ('Culture & History', 'Memorials, stories and traditions that explain Rwanda’s past and present.'),
    ('Custom Journeys', 'Personalised itineraries built around your interests.'),
]

PACKAGES = [
    {
        'slug': 'kigali-highlights',
        'title': 'Kigali Highlights',
        'category': 'City Tours',
        'summary': 'A relaxed introduction to selected city landmarks and stories.',
        'description': 'A relaxed introduction to selected Kigali landmarks and the stories behind them. '
                       'Choose the attractions that interest you, with a flexible pace and meeting point.',
        'highlights': 'Choose a few Kigali attractions\nLocal history and city context\nFlexible pace and meeting point',
        'price': Decimal('35.00'), 'duration_days': 1, 'max_guests': 8, 'featured': False,
    },
    {
        'slug': 'kigali-full-day-discovery',
        'title': 'Kigali Full-Day Discovery',
        'category': 'Culture & History',
        'summary': 'Spend more time exploring the city’s history, culture and everyday life.',
        'description': 'A full day exploring Kigali’s history, culture and everyday life, including '
                       'markets and neighbourhoods, with plenty of time for questions.',
        'highlights': 'Visit several selected attractions\nMarkets and neighbourhoods\nTime for questions and local insights',
        'price': Decimal('60.00'), 'duration_days': 1, 'max_guests': 8, 'featured': True,
    },
    {
        'slug': 'custom-rwanda-experience',
        'title': 'Custom Rwanda Experience',
        'category': 'Custom Journeys',
        'summary': 'Tell us what interests you and we will plan a personalised itinerary.',
        'description': 'Tell us your interests and we can discuss a personalised itinerary, including '
                       'destinations, timing and trips beyond Kigali.',
        'highlights': 'Choose your interests\nDiscuss destinations and timing\nAsk about trips beyond Kigali',
        'price': Decimal('120.00'), 'duration_days': 3, 'max_guests': 10, 'featured': False,
    },
]

FAQS = [
    ('How do I book a tour?',
     'Choose a tour, pick a date and number of guests, and submit the booking form. You will receive an '
     'email with your booking reference. We confirm availability and arrangements before your visit.'),
    ('How do I pay?',
     'Pay by bank transfer, mobile money or cash, then record the payment on your booking page with the '
     'transaction reference. Our team confirms each payment and emails you.'),
    ('Can I cancel my booking?',
     'Yes. You can cancel a pending or confirmed booking from your booking page before the travel date. '
     'Please contact us for any changes close to your travel date.'),
    ('Do I need to speak Kinyarwanda?',
     'No. Our guides speak English and French. Learning a few Kinyarwanda greetings, such as “Muraho” '
     '(hello) and “Murakoze” (thank you), is always appreciated.'),
    ('Are memorial visits appropriate for children?',
     'Memorial sites deal with difficult history. Please tell us about your group when booking so your '
     'guide can help you plan a respectful visit.'),
]


class Command(BaseCommand):
    help = 'Load the sample destinations, tours, categories and FAQs from the landing page.'

    def handle(self, *args, **options):
        categories = {}
        for name, description in CATEGORIES:
            cat, _ = Category.objects.update_or_create(name=name, defaults={'description': description})
            categories[name] = cat

        for slug, name, location, description, photo, featured in DESTINATIONS:
            destination, _ = Destination.objects.update_or_create(
                slug=slug,
                defaults={'name': name, 'location': location, 'description': description,
                          'image_url': photo, 'is_featured': featured, 'is_active': True},
            )
            ensure_destination_tour(destination)  # every destination has its bookable tour, with open dates

        for item in PACKAGES:
            TourPackage.objects.update_or_create(
                slug=item['slug'],
                defaults={
                    'title': item['title'],
                    'category': categories[item['category']],
                    'summary': item['summary'],
                    'description': item['description'],
                    'highlights': item['highlights'],
                    'price': item['price'],
                    'duration_days': item['duration_days'],
                    'max_guests': item['max_guests'],
                    'is_featured': item['featured'],
                    'is_active': True,
                },
            )

        for order, (question, answer) in enumerate(FAQS, start=1):
            FAQ.objects.update_or_create(question=question, defaults={'answer': answer, 'order': order})

        for order, (icon, title, text) in enumerate(CULTURE, start=1):
            CultureItem.objects.update_or_create(
                title=title, defaults={'icon': icon, 'text': text, 'order': order, 'is_active': True})

        # Link each demo tour to the places it visits. Only adds links, so edits made in the Control Panel stay.
        for slug, place_slugs in TOUR_PLACES.items():
            tour = TourPackage.objects.filter(slug=slug).first()
            if tour is None:
                continue
            tour.destinations.add(*Destination.objects.filter(slug__in=place_slugs))

        for order, (heading, body) in enumerate(ABOUT_SECTIONS, start=1):
            AboutSection.objects.update_or_create(
                heading=heading, defaults={'body': body, 'order': order, 'is_active': True})

        SiteSettings.load()  # creates the settings row with the landing-page text as defaults
        DetailsPage.load()   # creates the Details page with its default introduction

        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(DESTINATIONS)} destinations, {len(PACKAGES)} tours, '
            f'{len(CATEGORIES)} categories and {len(FAQS)} FAQs.'))
