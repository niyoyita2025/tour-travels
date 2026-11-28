import secrets
from datetime import datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.validators import FileExtensionValidator, MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Avg, CheckConstraint, F, Q, Sum, UniqueConstraint
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .image_frames import fit_to_frame, fitted_upload, frame_for

PROFILE_PHOTO_FRAME = (512, 512)  # profile photos are squares, so they never stretch in a circle
BIO_MAX_LENGTH = 500

MAX_IMAGE_BYTES = 5 * 1024 * 1024


def validate_image_size(file):
    if file.size > MAX_IMAGE_BYTES:
        raise ValidationError('Image must be 5 MB or smaller.')


IMAGE_VALIDATORS = [FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp']), validate_image_size]

MAX_FAVICON_BYTES = 512 * 1024


def validate_favicon_size(file):
    if file.size > MAX_FAVICON_BYTES:
        raise ValidationError('The favicon must be 512 KB or smaller.')


FAVICON_VALIDATORS = [FileExtensionValidator(['png', 'ico', 'webp', 'jpg', 'jpeg']), validate_favicon_size]


def unique_slug(instance, value):
    """Return a slug for `value` that is unique for the instance's model."""
    base = slugify(value)[:180] or 'item'
    slug, n = base, 2
    model = instance.__class__
    while model.objects.filter(slug=slug).exclude(pk=instance.pk).exists():
        slug = f'{base}-{n}'
        n += 1
    return slug


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'categories'

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Destination(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    location = models.CharField(max_length=120, default='Kigali, Rwanda')
    country = models.CharField(max_length=80, default='Rwanda')
    summary = models.CharField('short description', max_length=220, blank=True,
                               help_text='One or two sentences for cards. Leave blank to use the start of the full description.')
    description = models.TextField('full description')
    attractions = models.TextField(blank=True, help_text='One attraction per line.')
    activities = models.TextField(blank=True, help_text='One activity per line.')
    travel_info = models.TextField('getting there', blank=True, help_text='Transport, distance and access.')
    accommodation_info = models.TextField('accommodation', blank=True, help_text='Where to stay nearby, if relevant.')
    best_time = models.CharField('best time to visit', max_length=160, blank=True)
    recommended_duration = models.CharField('recommended visit length', max_length=80, blank=True,
                                            help_text='For example "2 to 3 hours" or "Half a day".')
    visitor_info = models.TextField('important visitor information', blank=True,
                                    help_text='Opening hours, dress code, fees, photography rules and similar.')
    map_link = models.URLField(
        'map link (optional)', blank=True,
        help_text='A Google Maps link for this place. Leave blank to use an automatic map search.')
    image = models.ImageField(upload_to='destinations/', blank=True, validators=IMAGE_VALIDATORS)
    image_url = models.URLField(blank=True, help_text='Optional web address of a photo, used when no image is uploaded.')
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('destination_detail', args=[self.slug])

    @property
    def photo_url(self):
        return self.image.url if self.image else self.image_url

    @property
    def map_url(self):
        if self.map_link:
            return self.map_link
        return 'https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': f'{self.name} {self.location}'})

    @property
    def map_embed_url(self):
        """An embeddable Google Maps view of this place, for the destination page."""
        return 'https://maps.google.com/maps?' + urlencode({
            'q': f'{self.name} {self.location}', 't': '', 'z': 13, 'ie': 'UTF8', 'output': 'embed'})

    @property
    def short_text(self):
        return self.summary or self.description

    @property
    def attraction_list(self):
        return _lines(self.attractions)

    @property
    def activity_list(self):
        return _lines(self.activities)


def _lines(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


class DestinationPhoto(models.Model):
    """Extra photographs for a destination's gallery. The main photo stays on the destination itself."""
    destination = models.ForeignKey(Destination, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='destinations/gallery/', validators=IMAGE_VALIDATORS)
    caption = models.CharField(max_length=200, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField('shown', default=True)

    class Meta:
        ordering = ['destination__name', 'order', 'id']
        verbose_name = 'destination photo'

    def __str__(self):
        return f'{self.destination}: {self.caption or "photo"}'


class TourPackage(models.Model):
    title = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170, unique=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='packages')
    destinations = models.ManyToManyField(
        Destination, blank=True, related_name='packages',
        help_text='Every place this tour visits. The tour is offered on each of these destination pages.')
    auto_for = models.OneToOneField(
        Destination, on_delete=models.SET_NULL, null=True, blank=True, related_name='auto_tour',
        help_text='The destination this bookable tour was created for automatically. Empty for tours made by hand.')
    summary = models.CharField(max_length=200)
    description = models.TextField()
    highlights = models.TextField(blank=True, help_text='One highlight per line.')
    itinerary = models.TextField(blank=True, help_text='One step per line, for example "09:00 Meet at the hotel".')
    start_location = models.CharField('starting point', max_length=160, blank=True)
    end_location = models.CharField('ending point', max_length=160, blank=True)
    included = models.TextField("what's included", blank=True, help_text='One item per line.')
    excluded = models.TextField('not included', blank=True, help_text='One item per line.')
    cancellation_policy = models.TextField(blank=True, help_text='Leave blank to use the general booking policy.')
    travel_instructions = models.TextField('important travel instructions', blank=True,
                                           help_text='What to bring, what to wear, meeting details.')
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0'))],
                                help_text='Price per guest, in USD.')
    duration_days = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    min_guests = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text='Smallest group a client can book (for example 2 or 3). Bookings below this are refused.')
    max_guests = models.PositiveSmallIntegerField(default=10, validators=[MinValueValidator(1)])
    image = models.ImageField(upload_to='packages/', blank=True, validators=IMAGE_VALIDATORS)
    image_url = models.URLField(blank=True, help_text='Optional web address of a photo, used when no image is uploaded.')
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['price', 'title']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, self.title)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('package_detail', args=[self.slug])

    @property
    def photo_url(self):
        return self.image.url if self.image else self.image_url

    @property
    def highlight_list(self):
        return _lines(self.highlights)

    @property
    def itinerary_list(self):
        return _lines(self.itinerary)

    @property
    def included_list(self):
        return _lines(self.included)

    @property
    def excluded_list(self):
        return _lines(self.excluded)

    @property
    def average_rating(self):
        value = self.reviews.filter(is_published=True).aggregate(avg=Avg('rating'))['avg']
        return round(value, 1) if value else None

    @property
    def review_count(self):
        return self.reviews.filter(is_published=True).count()


class TourPhoto(models.Model):
    """Extra photographs for a tour's gallery. The main photo stays on the tour itself."""
    package = models.ForeignKey(TourPackage, on_delete=models.CASCADE, related_name='photos', verbose_name='tour')
    image = models.ImageField(upload_to='packages/gallery/', validators=IMAGE_VALIDATORS)
    caption = models.CharField(max_length=200, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField('shown', default=True)

    class Meta:
        ordering = ['package__title', 'order', 'id']
        verbose_name = 'tour photo'

    def __str__(self):
        return f'{self.package}: {self.caption or "photo"}'


def generate_reference():
    return 'ER' + secrets.token_hex(3).upper()


# Booking statuses that hold seats on a tour slot. Cancelled, expired and completed
# bookings release their seats.
HOLDING_STATUSES = ('pending', 'confirmed')
MAX_BOOKING_HOURS = 12


class TourSchedule(models.Model):
    """
    One bookable departure: a tour on a date at a start time, with a maximum
    number of clients. seats_booked is kept in step with bookings by
    bookings.py, and a database constraint stops it ever exceeding capacity.
    """

    package = models.ForeignKey(TourPackage, on_delete=models.PROTECT, related_name='schedules')
    date = models.DateField()
    start_time = models.TimeField(default=time(9, 0))
    capacity = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1)],
        help_text='Maximum number of clients (guests) for this date and time.')
    seats_booked = models.PositiveSmallIntegerField(
        default=0, editable=False,
        help_text='Seats held by pending and confirmed bookings. Maintained automatically.')
    is_open = models.BooleanField(default=True, help_text='Untick to close this slot to new bookings.')
    group_only = models.BooleanField(
        default=False, editable=False,
        help_text='Set for start times made by a tour timetable: one group per start time.')
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'start_time']
        constraints = [
            UniqueConstraint(fields=['package', 'date', 'start_time'], name='unique_tour_slot'),
            CheckConstraint(condition=Q(seats_booked__lte=F('capacity')), name='seats_never_exceed_capacity'),
        ]

    def __str__(self):
        return f'{self.package.title} · {self.date:%d %b %Y} {self.start_time:%H:%M}'

    @property
    def seats_remaining(self):
        if self.group_only and self.seats_booked:
            return 0  # one group per start time: the time is taken once a group has booked it
        return max(self.capacity - self.seats_booked, 0)

    @property
    def start_datetime(self):
        return timezone.make_aware(datetime.combine(self.date, self.start_time), timezone.get_current_timezone())

    @property
    def has_started(self):
        return self.start_datetime <= timezone.now()

    @property
    def is_bookable(self):
        return self.is_open and not self.has_started and self.seats_remaining > 0


class ScheduleRule(models.Model):
    """
    An automatic timetable for one tour. Start times run from default_start every interval_hours
    up to last_start, on the operating days. Each start time takes one group, a group is on tour for
    tour_hours (later starts that would overlap it are not offered), and one date holds at most
    daily_capacity guests. The group sizes are set on the tour itself (TourPackage).
    Tours without a rule keep their hand-made dates.
    """

    package = models.OneToOneField(TourPackage, on_delete=models.CASCADE, related_name='schedule_rule')
    default_start = models.TimeField(default=time(8, 0), help_text='The first start time of the day.')
    last_start = models.TimeField(default=time(17, 0), help_text='The latest start time of the day.')
    interval_hours = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(MAX_BOOKING_HOURS)],
        help_text='Hours from one start time to the next.')
    tour_hours = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(MAX_BOOKING_HOURS)],
        help_text='How long one group is on tour. Every booking uses this length.')
    operating_days = models.CharField(
        max_length=20, default='0,1,2,3,4,5,6', help_text='Days the tour runs. Monday is 0, Sunday is 6.')
    daily_capacity = models.PositiveSmallIntegerField(
        default=24, validators=[MinValueValidator(1)],
        help_text='Most guests on one date, across all of its start times.')
    days_ahead = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(7), MaxValueValidator(365)],
        help_text='How many days ahead dates are opened.')

    def __str__(self):
        return f'Timetable for {self.package.title}' if self.package_id else 'Tour timetable'

    def clean(self):
        super().clean()
        if self.default_start and self.last_start and self.last_start < self.default_start:
            raise ValidationError({'last_start': 'The last start time cannot be earlier than the first start time.'})

    @property
    def weekdays(self):
        return {int(day) for day in self.operating_days.split(',') if day.strip().isdigit()}

    def start_times(self):
        """Every start time of the day, in order."""
        minute = self.default_start.hour * 60 + self.default_start.minute
        last = self.last_start.hour * 60 + self.last_start.minute
        times = []
        while minute <= last:
            times.append(time(minute // 60, minute % 60))
            minute += self.interval_hours * 60
        return times


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending confirmation'
        CONFIRMED = 'confirmed', 'Confirmed'
        CANCELLED = 'cancelled', 'Cancelled'
        COMPLETED = 'completed', 'Completed'
        EXPIRED = 'expired', 'Expired (not confirmed in time)'

    class PaymentStatus(models.TextChoices):
        UNPAID = 'unpaid', 'Unpaid'
        PARTIAL = 'partial', 'Partially paid'
        PAID = 'paid', 'Paid'
        VERIFIED = 'verified', 'Payment verified'

    reference = models.CharField(max_length=12, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookings')
    package = models.ForeignKey(TourPackage, on_delete=models.PROTECT, related_name='bookings')
    destination = models.ForeignKey(
        Destination, on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings',
        help_text='The destination the client chose to visit on this tour.')
    schedule = models.ForeignKey(
        TourSchedule, on_delete=models.PROTECT, null=True, blank=True, related_name='bookings',
        help_text='Tour date and time slot. Empty only for bookings made before the booking calendar existed.')
    travel_date = models.DateField()
    guests = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    duration_hours = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(MAX_BOOKING_HOURS)],
        help_text='How many hours the client wants to spend on the tour, starting at the chosen time.')
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    country = models.CharField(max_length=80)
    special_requests = models.TextField(blank=True)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'), editable=False,
                                      help_text='Price snapshot taken when the booking was made.')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID,
        help_text='Set by staff after payment is agreed and received outside the website.')
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='A pending request not confirmed by this time is released so the seats can be sold again.')
    reminder_sent_at = models.DateTimeField(null=True, blank=True, editable=False,
                                            help_text='When the reminder email for this booking was sent.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # A client can hold only one active booking for the same slot.
            UniqueConstraint(fields=['user', 'schedule'], condition=Q(status__in=HOLDING_STATUSES),
                             name='one_active_booking_per_slot'),
        ]

    def __str__(self):
        return f'{self.reference} – {self.package.title}'

    def clean(self):
        super().clean()
        if self.package_id and self.guests and self.guests < self.package.min_guests:
            raise ValidationError(f'This tour needs at least {self.package.min_guests} guests.')
        if self.package_id and self.guests and self.guests > self.package.max_guests:
            raise ValidationError(f'This tour allows at most {self.package.max_guests} guests.')
        if self._state.adding and self.travel_date and self.travel_date < timezone.localdate():
            raise ValidationError({'travel_date': 'Choose a date in the future.'})

    def save(self, *args, **kwargs):
        if not self.reference:
            reference = generate_reference()
            while Booking.objects.filter(reference=reference).exists():
                reference = generate_reference()
            self.reference = reference
        if self._state.adding:
            self.total_price = self.package.price * self.guests
        super().save(*args, **kwargs)

    @property
    def amount_paid(self):
        """Total of the payments recorded against this booking (see Payment)."""
        total = self.payments.aggregate(s=Sum('amount'))['s']
        return total or Decimal('0')

    @property
    def balance_due(self):
        return max(self.total_price - self.amount_paid, Decimal('0'))

    @property
    def payment_state(self):
        """Kept for templates. The status itself is set by staff in payment_status."""
        return self.payment_status

    @property
    def is_upcoming(self):
        return self.travel_date >= timezone.localdate()

    @property
    def window_minutes(self):
        """(start, end) of the booked time, in minutes after midnight. None without a time slot."""
        if not self.schedule_id:
            return None
        start = self.schedule.start_time.hour * 60 + self.schedule.start_time.minute
        return start, start + self.duration_hours * 60

    @property
    def holds_seats(self):
        return self.schedule_id is not None and self.status in HOLDING_STATUSES

    @property
    def start_datetime(self):
        if self.schedule_id:
            return self.schedule.start_datetime
        return timezone.make_aware(datetime.combine(self.travel_date, time.min), timezone.get_current_timezone())

    @property
    def cancellation_deadline(self):
        return self.start_datetime - timedelta(hours=settings.CANCELLATION_NOTICE_HOURS)

    @property
    def can_cancel(self):
        """Cancellation policy: allowed while pending or confirmed, until the notice period before the tour."""
        return self.status in (self.Status.PENDING, self.Status.CONFIRMED) and timezone.now() < self.cancellation_deadline

    def get_absolute_url(self):
        return reverse('booking_detail', args=[self.reference])


class Payment(models.Model):
    """
    A payment recorded by staff against a booking (a ledger entry).

    Clients pay directly with the tour operator, through mobile money or another
    agreed method outside the website. No payment is taken online, and no card
    data is stored. Staff record what was received and set the booking's
    payment_status. A future payment gateway would add its own rows here
    without changing the booking flow.
    """

    class Method(models.TextChoices):
        BANK_TRANSFER = 'bank_transfer', 'Bank transfer'
        MOBILE_MONEY = 'mobile_money', 'Mobile money'
        CASH = 'cash', 'Cash'
        OTHER = 'other', 'Other agreed method'

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.MOBILE_MONEY)
    transaction_reference = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='recorded_payments')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.booking.reference}: USD {self.amount} ({self.get_method_display()})'


class Review(models.Model):
    package = models.ForeignKey(TourPackage, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(max_length=1000)
    is_published = models.BooleanField(default=True, help_text='Untick to hide this review from the website.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['package', 'user'], name='one_review_per_user_per_package'),
        ]

    def __str__(self):
        return f'{self.rating}★ {self.package.title} by {self.user}'


class Inquiry(models.Model):
    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    package = models.ForeignKey(TourPackage, on_delete=models.SET_NULL, null=True, blank=True, related_name='inquiries')
    message = models.TextField(max_length=2000)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'inquiries'

    def __str__(self):
        return f'Inquiry from {self.name} ({self.created_at:%Y-%m-%d})'


class FAQ(models.Model):
    question = models.CharField(max_length=255)
    answer = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'FAQ'
        verbose_name_plural = 'FAQs'

    def __str__(self):
        return self.question


class SiteSettings(models.Model):
    """
    Everything on the public site that staff edit as text, images or contact details.
    There is exactly one row (pk=1); use SiteSettings.load() to read it.
    """
    site_name = models.CharField('site name', max_length=100, default='Explore Rwanda with a Local Guide')
    # Branding: shown in the header, dashboards, browser tab and emails.
    tagline = models.CharField(max_length=80, default='with a local guide', blank=True,
                               help_text='Short line under the name in the header.')
    site_description = models.TextField(
        'website description', max_length=300, blank=True,
        default='Discover Kigali and experience Rwanda with a friendly local guide. Explore culture, history, '
                'markets and beautiful city views.',
        help_text='Used by search engines and link previews.')
    logo = models.ImageField(upload_to='site/', blank=True, validators=IMAGE_VALIDATORS,
                             help_text='JPG, PNG or WebP, up to 5 MB. Leave blank to show the letter mark.')
    logo_dark = models.ImageField('logo for dark backgrounds', upload_to='site/', blank=True,
                                  validators=IMAGE_VALIDATORS,
                                  help_text='Optional. Used in dark mode, the dashboards and the footer.')
    favicon = models.ImageField('browser icon (favicon)', upload_to='site/', blank=True,
                                validators=FAVICON_VALIDATORS,
                                help_text='Square PNG or ICO, ideally 64 x 64 pixels, up to 512 KB.')
    mission = models.TextField('our mission', blank=True)
    vision = models.TextField('our vision', blank=True)
    # Policies: each has its own public page, linked from the footer.
    terms_text = models.TextField('terms and conditions', blank=True)
    privacy_text = models.TextField('privacy policy', blank=True)
    booking_policy_text = models.TextField(
        'booking and cancellation policy', blank=True,
        default='Your booking is a request until we confirm it. Pending requests hold your seats for a short time. '
                'You can cancel a booking from your account up to the notice period shown on your booking. '
                'Payment is arranged directly with us.')
    # Email notification settings
    email_staff_on_booking = models.BooleanField(
        'email staff about new bookings, cancellations and messages', default=True)
    send_booking_reminders = models.BooleanField('send booking reminders', default=True)
    reminder_hours_before = models.PositiveSmallIntegerField(
        'send reminders this many hours before', default=24,
        validators=[MinValueValidator(1), MaxValueValidator(168)])
    footer_tagline = models.TextField(
        default="Local guided experiences that help international visitors discover Kigali's history, culture and everyday life.")

    hero_eyebrow = models.CharField(max_length=120, default='Discover the heart of Rwanda')
    hero_title = models.CharField(max_length=160, default='See Rwanda through local eyes.')
    hero_text = models.TextField(default="Go beyond the usual visit. Discover Kigali's history, colourful neighbourhoods, local markets and welcoming culture with a friendly local guide.")
    intro_kicker = models.CharField(max_length=120, default='Your local connection')
    intro_title = models.CharField(max_length=200, default='More than a tour. A connection with Rwanda.')
    intro_text = models.TextField(default="Rwanda is a country of remarkable history, vibrant communities and beautiful hills. We help visitors discover the stories behind the places and experience everyday life in Kigali.")
    culture_intro = models.TextField('culture section intro', default='Every visit is an opportunity to learn something about Rwanda\'s people, history and traditions.')
    history_note = models.TextField('history note', default="Your guide can help connect individual places to Rwanda's pre-colonial kingdom, the colonial period, independence in 1962, the 1994 Genocide against the Tutsi, and Rwanda today. Memorial visits are handled with care and respect.")

    about_title = models.CharField(max_length=200, default='About Explore Rwanda')
    about_text = models.TextField(default="Rwanda is a country of remarkable history, vibrant communities and beautiful hills. We help visitors discover the stories behind the places and experience everyday life in Kigali.\n\nMemorial visits are handled with care and respect. Your guide can help connect each place to Rwanda's history and to Rwanda today.")

    contact_intro = models.TextField(default='Tell us about your trip: the places you want to see, your dates and the size of your group.')
    booking_notice = models.TextField(default='Your booking is a request, not a confirmed reservation. We confirm availability, price and arrangements with you.')
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    contact_address = models.CharField(max_length=200, blank=True, help_text='Office address shown in the footer.')
    whatsapp_number = models.CharField('WhatsApp number', max_length=20, blank=True,
                                       help_text='Digits only in international format, e.g. 2507XXXXXXXX. Leave blank to hide the WhatsApp link.')

    final_cta_title = models.CharField(max_length=200, default='Come curious. Leave with a deeper connection.')
    final_cta_text = models.TextField(default='Discover the places, people and stories that make Kigali special. We look forward to welcoming you.')

    payment_phone = models.CharField(
        'payment phone number', max_length=40, blank=True,
        help_text='The number clients call or send mobile money to. Shown on booking and payment pages.')
    payment_instructions = models.TextField(
        default='Payment is arranged directly with the tour operator. Please call or message the number '
                'shown here to agree how and when to pay. Your payment status appears on your booking.',
        help_text='Shown to clients on their booking and payment pages.')
    staff_notification_email = models.EmailField(
        blank=True, help_text='Receives new bookings, payment updates and enquiries.')

    # Hosting credit (footer: "Hosted by")
    hosted_by_name = models.CharField('hosted by: name', max_length=120, blank=True,
                                      help_text='Leave blank to hide the "Hosted by" block.')
    hosted_by_logo = models.ImageField('hosted by: logo or profile image', upload_to='site/', blank=True,
                                       validators=IMAGE_VALIDATORS)
    hosted_by_url = models.URLField('hosted by: website or contact link', blank=True)

    class Meta:
        verbose_name = 'site settings'
        verbose_name_plural = 'site settings'

    def __str__(self):
        return 'Site settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # the single settings row cannot be deleted

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def logo_url(self):
        return self.logo.url if self.logo else ''

    @property
    def logo_dark_url(self):
        return self.logo_dark.url if self.logo_dark else self.logo_url

    @property
    def favicon_url(self):
        return self.favicon.url if self.favicon else ''

    @property
    def brand_letter(self):
        return (self.site_name.strip()[:1] or 'R').upper()


class Testimonial(models.Model):
    """A short quote from a past guest, shown on the home page. Separate from tour reviews."""
    name = models.CharField(max_length=120)
    origin = models.CharField('from', max_length=120, blank=True, help_text='For example "Toronto, Canada".')
    quote = models.TextField(max_length=600)
    rating = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(5)])
    photo = models.ImageField(upload_to='testimonials/', blank=True, validators=IMAGE_VALIDATORS)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField('published', default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.name}: {self.quote[:40]}'


class EmailLog(models.Model):
    """One row per email the website tried to send. Never stores codes, passwords or message bodies."""

    class Status(models.TextChoices):
        SENT = 'sent', 'Accepted by Gmail'
        FAILED = 'failed', 'Failed'

    kind = models.CharField(max_length=40, db_index=True)
    recipient = models.EmailField()
    subject = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.kind} to {self.recipient}: {self.status}'


class CultureItem(models.Model):
    icon = models.CharField(max_length=10, default='🌿', help_text='An emoji shown on the card.')
    title = models.CharField(max_length=80)
    text = models.TextField(max_length=300)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.title


class CoHost(models.Model):
    """A partner or co-host shown in the footer with their logo and name."""

    name = models.CharField(max_length=120)
    logo = models.ImageField(upload_to='cohosts/', blank=True, validators=IMAGE_VALIDATORS,
                             help_text='Square logo or profile image, JPG, PNG or WebP, up to 5 MB.')
    description = models.CharField(max_length=300, blank=True)
    website_url = models.URLField('website or contact link', blank=True)
    is_active = models.BooleanField(default=True, help_text='Untick to hide this co-host from the footer.')
    order = models.PositiveSmallIntegerField(default=0, help_text='Lower numbers appear first.')

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name

    @property
    def logo_url(self):
        return self.logo.url if self.logo else ''


class BackgroundImage(models.Model):
    """
    A background photo for one part of the website. Each section has at most one image;
    the administrator can replace or remove it at any time from the Control Panel.
    """

    class Section(models.TextChoices):
        HERO = 'hero', 'Home: top banner'
        INTRO = 'intro', 'Home: "Your local connection" photo'
        DESTINATIONS = 'destinations', 'Home: destinations band'
        EXPERIENCES = 'experiences', 'Home: tours band'
        CULTURE = 'culture', 'Home: culture section'
        BOOKING = 'booking', 'Home: enquiry section'
        FINAL_CTA = 'final_cta', 'Home: closing banner'
        TOURS_PAGE = 'tours_page', 'Tours page header'
        DESTINATIONS_PAGE = 'destinations_page', 'Destinations page header'
        ABOUT = 'about', 'About page header'
        CONTACT = 'contact', 'Contact page header'
        FAQ = 'faq', 'FAQ page header'
        DETAILS = 'details', 'Details page header'

    section = models.CharField(max_length=30, choices=Section.choices, unique=True)
    image = models.ImageField(upload_to='backgrounds/', blank=True, validators=IMAGE_VALIDATORS)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['section']

    def __str__(self):
        return self.get_section_display()

    @property
    def frame(self):
        """(width, height) in pixels that this section's photo is cropped to."""
        return frame_for(self.section)

    def save(self, *args, **kwargs):
        # A new upload is cropped and resized to this section's frame before it is stored, so the
        # administrator never has to crop by hand. Admin and Control Panel uploads both pass through here.
        if self.image and not self.image._committed:
            self.image = fitted_upload(self.image.file, self.image.name, self.section)
        super().save(*args, **kwargs)

    @property
    def url(self):
        return self.image.url if self.image else ''


class DetailsPage(models.Model):
    """The public Details page: a single managed page (pk=1). Its photos, books, links and sections are separate rows."""

    title = models.CharField(max_length=120, default='Details')
    intro = models.TextField(default='Discover more about Rwanda, its history and the places we visit.')
    body = models.TextField(blank=True, help_text='Paragraphs are separated by a blank line.')
    is_published = models.BooleanField(default=True, help_text='Untick to hide the Details page from visitors.')

    class Meta:
        verbose_name = 'details page'
        verbose_name_plural = 'details page'

    def __str__(self):
        return 'Details page'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class DetailPhoto(models.Model):
    image = models.ImageField(upload_to='details/', validators=IMAGE_VALIDATORS)
    caption = models.CharField(max_length=200, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.caption or f'Photo {self.pk}'


class DetailBook(models.Model):
    title = models.CharField(max_length=160)
    author = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    link = models.URLField('where to find it', blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.title


class DetailLink(models.Model):
    title = models.CharField(max_length=160)
    url = models.URLField('web address')
    description = models.CharField(max_length=300, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.title


class DetailSection(models.Model):
    """Any other information for the Details page: a heading and its text."""

    heading = models.CharField(max_length=160)
    body = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.heading


class AboutSection(models.Model):
    """A block of content on the About page, with an optional photo."""

    heading = models.CharField(max_length=160)
    body = models.TextField()
    image = models.ImageField(upload_to='about/', blank=True, validators=IMAGE_VALIDATORS)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.heading

    @property
    def image_url(self):
        return self.image.url if self.image else ''


class ContactDetail(models.Model):
    """Any extra contact information shown on the Contact page (phone lines, emails, hours, and so on)."""

    class Kind(models.TextChoices):
        PHONE = 'phone', 'Phone'
        EMAIL = 'email', 'Email'
        ADDRESS = 'address', 'Address'
        WHATSAPP = 'whatsapp', 'WhatsApp'
        HOURS = 'hours', 'Opening hours'
        OTHER = 'other', 'Other'

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PHONE)
    label = models.CharField(max_length=80, blank=True, help_text='For example "Office" or "Tour desk".')
    value = models.CharField(max_length=200)
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.value}'


class SocialLink(models.Model):
    """A social media account. Its icon appears in the footer, opening the account in a new tab."""

    class Platform(models.TextChoices):
        FACEBOOK = 'facebook', 'Facebook'
        INSTAGRAM = 'instagram', 'Instagram'
        X = 'x', 'X (formerly Twitter)'
        TIKTOK = 'tiktok', 'TikTok'
        YOUTUBE = 'youtube', 'YouTube'
        LINKEDIN = 'linkedin', 'LinkedIn'
        WHATSAPP = 'whatsapp', 'WhatsApp'
        OTHER = 'other', 'Other'

    platform = models.CharField(max_length=20, choices=Platform.choices, default=Platform.FACEBOOK)
    label = models.CharField(max_length=60, blank=True, help_text='Optional. Defaults to the platform name.')
    url = models.URLField('account address (full link)')
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.display_label

    @property
    def display_label(self):
        return self.label or self.get_platform_display()


class Notification(models.Model):
    """In-app message for one user (customer or administrator)."""

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=160)
    message = models.TextField(blank=True)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} → {self.recipient}'


class ActivityLog(models.Model):
    """A record of what happened on the website and who did it."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='activity')
    action = models.CharField(max_length=60, db_index=True, help_text='e.g. booking.created')
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'activity log'

    def __str__(self):
        return f'{self.action}: {self.description}'


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=30, blank=True)
    country = models.CharField(max_length=80, blank=True)
    # New self-service sign-ups start unverified and inactive until their emailed code is entered.
    # Accounts that predate this field, and accounts created by staff, count as verified.
    email_verified = models.BooleanField(default=True)
    photo = models.ImageField('profile photo', upload_to='profiles/', blank=True, validators=IMAGE_VALIDATORS,
                              help_text='JPG, PNG or WebP, up to 5 MB. It is cropped to a square.')
    bio = models.TextField('about you', blank=True, max_length=BIO_MAX_LENGTH,
                           validators=[MaxLengthValidator(BIO_MAX_LENGTH)])

    def __str__(self):
        return f'Profile of {self.user}'

    def save(self, *args, **kwargs):
        # A new photo is cropped to a square and re-encoded as JPEG before it is stored. That keeps it
        # undistorted and strips any embedded metadata.
        if self.photo and not self.photo._committed:
            self.photo = SimpleUploadedFile(f'{secrets.token_hex(16)}.jpg',
                                            fit_to_frame(self.photo.file, PROFILE_PHOTO_FRAME),
                                            content_type='image/jpeg')
        super().save(*args, **kwargs)

    @property
    def photo_url(self):
        return self.photo.url if self.photo else ''


class EmailOTP(models.Model):
    """A six-digit code emailed to an account holder. Only a keyed hash of the code is stored."""

    class Purpose(models.TextChoices):
        SIGNUP = 'signup', 'Email verification'
        RESET = 'reset', 'Password reset'
        EMAIL_CHANGE = 'email_change', 'Email change'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_otps')
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    # For an email change, the new address the code was sent to. The code confirms only that address.
    target_email = models.EmailField(blank=True, default='')
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'purpose', 'used_at'])]

    def __str__(self):
        return f'{self.get_purpose_display()} code for user {self.user_id}'


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
