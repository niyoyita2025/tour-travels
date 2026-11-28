"""
The travel assistant's answers.

Every fact in a reply is read from the website database when the question is asked: destinations, tours,
prices, durations, open dates and the booking policy. Where the website has no information, the assistant
says so and points to the contact page. It never guesses a price, a place or an availability.

Questions about other people's bookings are refused, and customer details are never read.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.urls import reverse

from Tour_travels.calendar_service import effective_remaining, upcoming_bookable_slots
from Tour_travels.models import MAX_BOOKING_HOURS, Destination, SiteSettings, TourPackage

from . import llm
from .models import AssistantConfig

MAX_QUESTION = 500
POLICY_URL = '/policies/booking-policy/'
SUGGESTIONS = [
    'What tours do you offer?',
    'Which destinations can I visit?',
    'How much does a tour cost?',
    'Which dates are open?',
    'How do I book?',
]
TOUR_WORD_STOP = {'tour', 'tours', 'kigali', 'experience', 'package', 'half', 'full', 'day', 'with', 'the'}


@dataclass
class Reply:
    text: str
    intent: str
    links: list = field(default_factory=list)  # (label, path on this website)
    suggestions: list = field(default_factory=list)


GREETING = re.compile(r'^\s*(hi|hello|hey|good (morning|afternoon|evening)|muraho|bonjour|jambo)\b', re.I)
CUSTOMER_WORDS = re.compile(r'\b(customers?|clients?|guests?|bookings?|reservations?)\b', re.I)
ASKING_FOR_DETAILS = re.compile(r'\b(names?|e-?mails?|phones?|numbers?|all|everyone|everybody|list|details|who)\b', re.I)
OWN_WORDS = re.compile(r'\b(my|mine)\b', re.I)

RULES = [
    ('availability', re.compile(
        r'\b(availab\w*|open dates?|free dates?|dates?|when|seats?|slots?|vacanc\w*|still (open|free))\b', re.I)),
    ('booking', re.compile(
        r'\b(book\w*|reserv\w*|how .* work|cancel\w*|refund\w*|pay\w*|payments?)\b', re.I)),
    ('price', re.compile(r'\b(price\w*|cost\w*|how much|fees?|usd|dollars?)\b', re.I)),
    ('duration', re.compile(r'\b(how long|duration|how many (days|hours)|length|lasts?)\b', re.I)),
    ('recommend', re.compile(
        r'\b(recommend\w*|suggest\w*|suitable|best|budget|cheap\w*|afford\w*|which (tour|one)|what (tour|should)|'
        r'for (a |my )?(family|kids|children|couple|friends|group)|interested in|prefer\w*|i like)\b', re.I)),
    ('destinations', re.compile(
        r'\b(destinations?|places?|visit\w*|sites?|where|memorial|markets?|neighbou?rhoods?|attractions?)\b', re.I)),
    ('tours', re.compile(r'\b(tours?|packages?|experiences?|offers?|what do you (offer|have))\b', re.I)),
    ('contact', re.compile(r'\b(contact|phone|call|whatsapp|e-?mail|reach|talk to|speak)\b', re.I)),
]


def _intent(text):
    if GREETING.search(text):
        return 'greeting'
    # Anything about other people's bookings is refused. A client asking about their own bookings is pointed
    # to their bookings page, after signing in.
    if CUSTOMER_WORDS.search(text) and ASKING_FOR_DETAILS.search(text):
        return 'own' if OWN_WORDS.search(text) else 'private'
    for name, pattern in RULES:
        if pattern.search(text):
            return name
    return None


def _active_tours():
    return list(TourPackage.objects.filter(is_active=True).select_related('category').order_by('price', 'title'))


def _named_tour(text, tours):
    """The tour the visitor named, if any. Matches on distinctive words from the tour's title."""
    lowered = text.lower()
    best, best_score = None, 0
    for tour in tours:
        words = [w for w in re.findall(r'[a-z0-9]+', tour.title.lower()) if len(w) > 3 and w not in TOUR_WORD_STOP]
        score = sum(1 for word in words if word in lowered)
        if score > best_score:
            best, best_score = tour, score
    return best


def _money(amount):
    return f'USD {amount:,.2f}'


def _greeting(text, config):
    return Reply(f'{config.greeting} Try asking about tours, prices, open dates or destinations.', 'greeting',
                 suggestions=SUGGESTIONS)


def _private(text, config):
    return Reply('I can only share public information about our tours, destinations, prices and open dates. '
                 'For anything about a booking, sign in to your account, or contact us.', 'private',
                 links=[('Contact us', reverse('contact'))], suggestions=SUGGESTIONS)


def _own(text, config):
    return Reply('To see your own bookings, sign in and open My bookings. I cannot show details of other people\'s '
                 'bookings.', 'own', links=[('My bookings', reverse('booking_history'))])


def _destinations(text, config):
    places = list(Destination.objects.filter(is_active=True).order_by('name')[:10])
    if not places:
        return Reply('We have no destinations published on the website at the moment. Please contact us for details.',
                     'destinations', links=[('Contact us', reverse('contact'))])
    lines = []
    for place in places:
        blurb = place.summary or place.description
        blurb = blurb if len(blurb) <= 140 else blurb[:137].rstrip() + '…'
        lines.append(f'- {place.name} ({place.location}): {blurb}')
    return Reply('Destinations you can visit:\n' + '\n'.join(lines), 'destinations',
                 links=[('All destinations', reverse('destinations'))], suggestions=SUGGESTIONS)


def _tours(text, config):
    tours = _active_tours()
    if not tours:
        return Reply('No tours are published on the website at the moment. Please contact us for details.', 'tours',
                     links=[('Contact us', reverse('contact'))])
    lines = [f'- {t.title}: {_money(t.price)} per guest, {t.duration_days} day(s), groups of '
             f'{t.min_guests} to {t.max_guests}.' for t in tours]
    return Reply('Our tours:\n' + '\n'.join(lines), 'tours',
                 links=[('Browse tours', reverse('packages'))], suggestions=SUGGESTIONS)


def _price(text, config):
    tours = _active_tours()
    named = _named_tour(text, tours)
    if named:
        return Reply(f'{named.title} costs {_money(named.price)} per guest. It lasts {named.duration_days} '
                     f'day(s). The price is for each guest; the total depends on the number of guests.',
                     'price', links=[(named.title, named.get_absolute_url())])
    if not tours:
        return Reply('No tour prices are published at the moment. Please contact us for details.', 'price')
    lines = [f'- {t.title}: {_money(t.price)} per guest' for t in tours]
    return Reply('Prices are per guest:\n' + '\n'.join(lines), 'price', links=[('Browse tours', reverse('packages'))])


def _duration(text, config):
    tours = _active_tours()
    named = _named_tour(text, tours)
    if named:
        return Reply(f'{named.title} lasts {named.duration_days} day(s). When you book, you choose how many hours '
                     f'you want on the tour, from 1 to {MAX_BOOKING_HOURS}.', 'duration',
                     links=[(named.title, named.get_absolute_url())])
    if not tours:
        return Reply('No tour durations are published at the moment. Please contact us for details.', 'duration')
    lines = [f'- {t.title}: {t.duration_days} day(s)' for t in tours]
    return Reply('Tour lengths:\n' + '\n'.join(lines) + f'\nWhen you book, you choose how many hours you want on '
                 f'the tour, from 1 to {MAX_BOOKING_HOURS}.', 'duration')


def _availability(text, config):
    tours = _active_tours()
    named = _named_tour(text, tours)
    chosen = [named] if named else tours[:6]
    if not chosen:
        return Reply('No tours are published, so there are no dates to show. Please contact us.', 'availability',
                     links=[('Contact us', reverse('contact'))])
    lines, links = [], []
    for tour in chosen:
        slots = upcoming_bookable_slots(tour, limit=3)
        links.append((f'Calendar: {tour.title}', reverse('tour_calendar', args=[tour.slug])))
        if slots:
            described = '; '.join(f'{s.date:%a %d %b} at {s.start_time:%H:%M} ({effective_remaining(s)} seats left)'
                                  for s in slots)
            lines.append(f'- {tour.title}: {described}')
        else:
            lines.append(f'- {tour.title}: no open dates in the next 120 days.')
    return Reply('Open dates, as the booking calendar shows them now (seats change as people book):\n'
                 + '\n'.join(lines), 'availability', links=links, suggestions=SUGGESTIONS)


def _booking(text, config):
    site = SiteSettings.load()
    steps = ('How to book:\n'
             '1. Open a tour and choose a date and start time on its calendar.\n'
             '2. Choose the number of guests and how many hours you want.\n'
             '3. Sign in or create an account. Your choice is kept while you do.\n'
             '4. Check your details and send the request.\n\n'
             f'Your request holds its seats for {settings.BOOKING_HOLD_HOURS} hours until we confirm it by email. '
             'Payment is arranged directly with the tour operator.')
    policy = (site.booking_policy_text or '').strip()
    if policy:
        steps += '\n\nBooking and cancellation policy: ' + policy
    return Reply(steps, 'booking', links=[('Booking and cancellation policy', POLICY_URL),
                                          ('Browse tours', reverse('packages'))])


def _recommend(text, config):
    tours = _active_tours()
    if not tours:
        return Reply('No tours are published at the moment, so there is nothing to recommend yet.', 'recommend',
                     links=[('Contact us', reverse('contact'))])
    lowered = text.lower()
    if re.search(r'budget|cheap|afford|low cost|lowest', lowered):
        ranked, reason = sorted(tours, key=lambda t: (t.price, t.title)), 'our lowest prices'
    elif re.search(r'short|quick|half|few hours|one day|1 day', lowered):
        ranked, reason = sorted(tours, key=lambda t: (t.duration_days, t.price)), 'our shortest tours'
    else:
        ranked, reason = sorted(tours, key=lambda t: (not t.is_featured, t.price)), 'our featured tours'
    picks = ranked[:3]
    lines = [f'- {t.title}: {_money(t.price)} per guest, {t.duration_days} day(s)' for t in picks]
    return Reply(f'Here are {reason}:\n' + '\n'.join(lines)
                 + '\nTell me your budget, how long you can spend, or what interests you, and I can narrow it down.',
                 'recommend', links=[(t.title, t.get_absolute_url()) for t in picks], suggestions=SUGGESTIONS)


def _contact(text, config):
    site = SiteSettings.load()
    parts = []
    if site.contact_email:
        parts.append(f'Email: {site.contact_email}')
    if site.contact_phone:
        parts.append(f'Phone: {site.contact_phone}')
    if site.whatsapp_number:
        parts.append(f'WhatsApp: +{site.whatsapp_number}')
    text_out = ('You can reach us at:\n' + '\n'.join(parts)) if parts else 'Please use our contact form.'
    return Reply(text_out, 'contact', links=[('Send us a message', reverse('contact'))])


def _fallback(text, config):
    return Reply('I can help with destinations, tours, prices, durations, open dates and booking. '
                 'Try one of these questions:', 'fallback', suggestions=SUGGESTIONS)


HANDLERS = {
    'greeting': _greeting, 'private': _private, 'own': _own, 'destinations': _destinations, 'tours': _tours,
    'price': _price, 'duration': _duration, 'availability': _availability, 'booking': _booking,
    'recommend': _recommend, 'contact': _contact,
}


def answer(question, config=None):
    """The rule-based answer. Every fact in it comes from the database at this moment."""
    config = config or AssistantConfig.load()
    text = (question or '').strip()[:MAX_QUESTION]
    if not text:
        return Reply('Please type a question about our tours, destinations or bookings.', 'empty',
                     suggestions=SUGGESTIONS)
    intent = _intent(text)
    handler = HANDLERS.get(intent, _fallback)
    return handler(text, config)


def respond(question):
    """The reply shown to a visitor. A language model may reword it, but only when an administrator has allowed it."""
    config = AssistantConfig.load()
    reply = answer(question, config)
    if config.use_llm and reply.intent not in ('empty', 'private') and llm.can_use():
        phrased = llm.phrase(question, reply.text, config.model_name)
        if phrased:
            reply.text = phrased
    return reply
