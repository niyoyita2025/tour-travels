import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import RegexValidator
from django.utils import timezone
from PIL import Image

from .calendar_service import effective_remaining, schedules_in_range
from .models import (
    FAQ, Booking, Category, CultureItem, Destination, Inquiry, Payment, Review, SiteSettings,
    ScheduleRule, TourPackage, TourSchedule, UserProfile, CoHost,
    AboutSection, BackgroundImage, ContactDetail, DetailBook, DetailLink, DetailPhoto, DetailSection,
    DetailsPage, SocialLink, DestinationPhoto, TourPhoto, Testimonial,
)
from .models import BIO_MAX_LENGTH, MAX_BOOKING_HOURS
from .models import MAX_IMAGE_BYTES as MAX_LOGO_BYTES

User = get_user_model()


class StyledFormMixin:
    """Gives every input the shared `input` CSS class, except checkboxes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                continue
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault('rows', 4)
            css = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{css} input'.strip()


class LoginForm(StyledFormMixin, AuthenticationForm):
    """Sign in with the email address or the username."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Email or username'
        self.fields['username'].widget.attrs.update({'autocomplete': 'username', 'autofocus': True})


class RegisterForm(StyledFormMixin, forms.Form):
    """Sign-up: full name, email address, and a password typed twice.

    The account is created inactive. It is activated only after the six-digit code emailed to the
    address is entered (see accounts_views.verify_email).
    """

    full_name = forms.CharField(label='Full name', max_length=150,
                                widget=forms.TextInput(attrs={'autocomplete': 'name'}))
    email = forms.EmailField(label='Email address', max_length=254,
                             widget=forms.EmailInput(attrs={'autocomplete': 'email'}))
    password1 = forms.CharField(label='Password', strip=False,
                                widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
                                help_text='At least 8 characters. Not too common, and not entirely numeric.')
    password2 = forms.CharField(label='Confirm password', strip=False,
                                widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    # Set when the email belongs to a sign-up that was never verified. That sign-up is resumed
    # instead of creating a second account.
    pending_user = None

    def clean_full_name(self):
        name = ' '.join(self.cleaned_data['full_name'].split())
        if len(name.split(' ')) < 2:
            raise ValidationError('Please enter your first and last name.')
        return name

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        existing = User.objects.filter(email__iexact=email).order_by('pk').first()
        if existing is not None:
            unverified = (not existing.is_active
                          and UserProfile.objects.filter(user=existing, email_verified=False).exists())
            if not unverified:
                raise ValidationError(
                    'An account with this email already exists. Log in instead, or use Forgot password.')
            self.pending_user = existing
        return email

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get('password1')
        password2 = cleaned.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'The two passwords do not match.')
        if password1:
            probe = User(email=cleaned.get('email', ''), first_name=(cleaned.get('full_name') or '').split(' ')[0])
            try:
                validate_password(password1, user=probe)
            except ValidationError as error:
                self.add_error('password1', error)
        return cleaned


class ForgotPasswordForm(StyledFormMixin, forms.Form):
    email = forms.EmailField(label='Email address', max_length=254,
                             widget=forms.EmailInput(attrs={'autocomplete': 'email'}))

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class NewPasswordForm(StyledFormMixin, SetPasswordForm):
    """Choose a new password after the reset code has been accepted."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].help_text = 'At least 8 characters. Not too common, and not entirely numeric.'


PHONE_VALIDATOR = RegexValidator(r'^\+?[0-9][0-9 ()\-]{6,28}$',
                                 'Enter a valid phone number, for example +250 788 123 456.')
PHOTO_FORMATS = {'JPEG', 'PNG', 'WEBP'}


class ProfileDetailsForm(StyledFormMixin, forms.ModelForm):
    """A client's or administrator's own profile. The email address is changed separately, with a code."""

    full_name = forms.CharField(label='Full name', max_length=150,
                                widget=forms.TextInput(attrs={'autocomplete': 'name'}))
    photo = forms.ImageField(label='Profile photo', required=False,
                             widget=forms.FileInput(attrs={'accept': 'image/jpeg,image/png,image/webp'}))
    remove_photo = forms.BooleanField(label='Remove my photo', required=False)
    phone = forms.CharField(label='Phone number', max_length=30, required=False, validators=[PHONE_VALIDATOR],
                            widget=forms.TextInput(attrs={'autocomplete': 'tel', 'inputmode': 'tel'}))
    country = forms.CharField(label='Country', max_length=80, required=False,
                              widget=forms.TextInput(attrs={'autocomplete': 'country-name'}))
    bio = forms.CharField(label='About you', max_length=BIO_MAX_LENGTH, required=False,
                          widget=forms.Textarea(attrs={'rows': 4, 'data-counter': BIO_MAX_LENGTH,
                                                       'placeholder': 'A few lines about you and the trips you enjoy.'}))

    class Meta:
        model = UserProfile
        fields = ('phone', 'country', 'bio', 'photo')

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.old_photo = self.instance.photo.name if self.instance.pk and self.instance.photo else ''
        self.initial.setdefault('full_name', user.get_full_name())

    def clean_full_name(self):
        return ' '.join(self.cleaned_data['full_name'].split())

    def clean_photo(self):
        photo = self.cleaned_data.get('photo')
        if not isinstance(photo, UploadedFile):
            return photo  # no new upload: keep the current photo, or none
        if photo.size > MAX_LOGO_BYTES:
            raise ValidationError('Choose a photo smaller than 5 MB.')
        photo.seek(0)
        try:
            with Image.open(photo) as image:
                image_format = image.format
        except Exception:  # noqa: BLE001 - anything Pillow cannot read is not a photo
            raise ValidationError('That file is not a photo. Use a JPG, PNG or WebP image.') from None
        finally:
            photo.seek(0)
        if image_format not in PHOTO_FORMATS:
            raise ValidationError('Use a JPG, PNG or WebP image.')
        return photo

    def clean(self):
        cleaned = super().clean()
        # Django returns the stored photo when nothing new is uploaded, so check for a real upload.
        new_upload = isinstance(cleaned.get('photo'), UploadedFile)
        if cleaned.get('remove_photo') and not new_upload:
            cleaned['photo'] = ''  # an empty value clears the stored photo
        return cleaned

    def save(self, commit=True):
        profile = super().save(commit=commit)
        parts = self.cleaned_data['full_name'].split(' ', 1)
        self.user.first_name = parts[0][:150]
        self.user.last_name = (parts[1] if len(parts) > 1 else '')[:150]
        self.user.save(update_fields=['first_name', 'last_name'])
        if self.old_photo and profile.photo.name != self.old_photo:
            profile.photo.storage.delete(self.old_photo)  # replaced or removed: do not keep the old file
        return profile


class EmailChangeForm(StyledFormMixin, forms.Form):
    """Asks for a new email address. The change happens only after a code sent to that address is entered."""

    new_email = forms.EmailField(label='New email address', max_length=254,
                                 widget=forms.EmailInput(attrs={'autocomplete': 'email'}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_new_email(self):
        email = self.cleaned_data['new_email'].strip().lower()
        if email == (self.user.email or '').lower():
            raise ValidationError('That is already your email address.')
        if User.objects.filter(email__iexact=email).exclude(pk=self.user.pk).exists():
            raise ValidationError('Another account already uses this email address.')
        return email


class ProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('phone', 'country')


class BookingForm(StyledFormMixin, forms.ModelForm):
    """
    Booking details for one chosen tour slot. The date and start time come from the slot;
    the client chooses how many hours they want to spend.
    """

    duration_hours = forms.TypedChoiceField(
        choices=[(h, f"{h} hour{'s' if h != 1 else ''}") for h in range(1, MAX_BOOKING_HOURS + 1)],
        coerce=int, initial=2, label='How many hours do you want to spend?')
    destination = forms.ModelChoiceField(
        queryset=Destination.objects.none(), required=False, label='Destination you want to visit')

    field_order = ['destination', 'guests', 'duration_hours']

    class Meta:
        model = Booking
        fields = ('guests', 'duration_hours', 'full_name', 'email', 'phone', 'country', 'special_requests')
        widgets = {'special_requests': forms.Textarea(attrs={'rows': 4})}

    def __init__(self, *args, schedule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.schedule = schedule
        # The client picks one of the places this tour visits. A tour with no places lists all active places.
        package = self.instance.package if self.instance.package_id else (schedule.package if schedule else None)
        places = package.destinations.filter(is_active=True).order_by('name') if package else None
        if places is not None and places.exists():
            self.fields['destination'].queryset = places
            self.fields['destination'].required = True
            self.fields['destination'].empty_label = 'Choose the place you will visit'
            self.fields['destination'].help_text = 'One of the places this tour visits.'
        else:
            self.fields['destination'].queryset = Destination.objects.filter(is_active=True).order_by('name')
            self.fields['destination'].empty_label = 'Not sure yet'
        self.package = package
        self.rule = ScheduleRule.objects.filter(package=package).first() if package else None
        self.fields['guests'].widget.attrs['min'] = package.min_guests if package else 1
        if self.rule is not None:
            # A tour with a timetable runs for its set length, so the client does not choose it.
            self.initial['duration_hours'] = self.rule.tour_hours
            self.fields['duration_hours'].disabled = True
            self.fields['duration_hours'].label = 'Tour length'
            self.fields['duration_hours'].help_text = 'Every group is on tour for this many hours.'
        if schedule is not None:
            self.fields['guests'].widget.attrs['max'] = max(min(schedule.seats_remaining,
                                                                 schedule.package.max_guests), 1)

    def _seats_now(self):
        """Guests this start time can still take, including what its date has left under the daily capacity."""
        schedule = self.schedule
        for slot in schedules_in_range(schedule.date, schedule.date, schedule.package):
            if slot.pk == schedule.pk:
                return effective_remaining(slot)
        return schedule.seats_remaining

    def clean_guests(self):
        guests = self.cleaned_data['guests']
        if self.schedule is None:
            return guests
        if guests < self.schedule.package.min_guests:
            raise forms.ValidationError(f'This tour needs at least {self.schedule.package.min_guests} guests.')
        seats = self._seats_now()
        if guests > seats:
            if seats == 0 and self.schedule.group_only:
                raise forms.ValidationError(
                    'This start time has already been booked by another group. Please choose another time.')
            if seats == 0:
                raise forms.ValidationError('This date is now fully booked. Please choose another date or time.')
            raise forms.ValidationError(f'Only {seats} seat(s) are left on this date and time.')
        return guests


class PaymentForm(StyledFormMixin, forms.ModelForm):
    """Staff record an amount received for a booking. Clients do not pay through the website."""

    class Meta:
        model = Payment
        fields = ('amount', 'method', 'transaction_reference', 'note')
        widgets = {'note': forms.TextInput(attrs={'placeholder': 'Optional message for the team'})}

    def __init__(self, *args, booking=None, **kwargs):
        self.booking = booking
        super().__init__(*args, **kwargs)
        self.fields['transaction_reference'].label = 'Transaction / receipt reference'
        self.fields['transaction_reference'].required = False

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if self.booking and amount > self.booking.balance_due:
            raise forms.ValidationError(
                f'The amount cannot be more than the balance due ({self.booking.balance_due}).')
        return amount


class ReviewForm(StyledFormMixin, forms.ModelForm):
    rating = forms.TypedChoiceField(
        choices=[(i, f'{i} – ' + ['Poor', 'Fair', 'Good', 'Very good', 'Excellent'][i - 1]) for i in range(5, 0, -1)],
        coerce=int,
    )

    class Meta:
        model = Review
        fields = ('rating', 'comment')
        widgets = {'comment': forms.Textarea(attrs={'rows': 4})}


class InquiryForm(StyledFormMixin, forms.ModelForm):
    package = forms.ModelChoiceField(
        queryset=TourPackage.objects.filter(is_active=True), required=False, empty_label='Not sure yet',
        label='Tour you are interested in')

    class Meta:
        model = Inquiry
        fields = ('name', 'email', 'phone', 'package', 'message')
        widgets = {'message': forms.Textarea(attrs={'rows': 5})}


class TourSearchForm(StyledFormMixin, forms.Form):
    SORT_CHOICES = [
        ('price', 'Price: low to high'),
        ('-price', 'Price: high to low'),
        ('-created_at', 'Newest'),
        ('duration_days', 'Shortest duration'),
        ('title', 'Name A–Z'),
    ]

    q = forms.CharField(required=False, label='Search', max_length=100,
                        widget=forms.TextInput(attrs={'placeholder': 'Search tours or places'}))
    category = forms.ModelChoiceField(queryset=Category.objects.all(), required=False, empty_label='All categories')
    min_price = forms.DecimalField(required=False, min_value=0, decimal_places=2, max_digits=10, label='Min price (USD)')
    max_price = forms.DecimalField(required=False, min_value=0, decimal_places=2, max_digits=10, label='Max price (USD)')
    sort = forms.ChoiceField(choices=SORT_CHOICES, required=False, initial='price')

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get('min_price'), cleaned.get('max_price')
        if lo is not None and hi is not None and lo > hi:
            raise forms.ValidationError('Minimum price cannot be higher than maximum price.')
        return cleaned


class BookingStatusForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Booking
        fields = ('status',)


# ---------- staff content management: every publishable field ----------

class DestinationForm(StyledFormMixin, forms.ModelForm):
    country = forms.CharField(max_length=80, required=False, initial='Rwanda',
                              help_text='Leave blank for Rwanda.')

    def clean_country(self):
        return (self.cleaned_data.get('country') or '').strip() or 'Rwanda'

    class Meta:
        model = Destination
        fields = ['name', 'slug', 'location', 'country', 'summary', 'description', 'attractions', 'activities',
                  'best_time', 'recommended_duration', 'travel_info', 'accommodation_info', 'visitor_info',
                  'map_link', 'image', 'image_url', 'is_featured', 'is_active']
        labels = {'is_active': 'Published (shown on the website)'}
        widgets = {name: forms.Textarea(attrs={'rows': 4}) for name in
                   ('attractions', 'activities', 'travel_info', 'accommodation_info', 'visitor_info')}


class DestinationPhotoForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DestinationPhoto
        fields = ['destination', 'image', 'caption', 'order', 'is_active']


class TourPhotoForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = TourPhoto
        fields = ['package', 'image', 'caption', 'order', 'is_active']


class TestimonialForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Testimonial
        fields = ['name', 'origin', 'quote', 'rating', 'photo', 'order', 'is_active']


class BrandingForm(StyledFormMixin, forms.ModelForm):
    """Website name, logo, favicon, tagline and description. Shown everywhere through the shared context."""

    class Meta:
        model = SiteSettings
        fields = ['site_name', 'tagline', 'site_description', 'logo', 'logo_dark', 'favicon']


class PoliciesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['terms_text', 'privacy_text', 'booking_policy_text']
        help_texts = {name: 'Paragraphs are separated by a blank line. Leave blank to hide the page.'
                      for name in ('terms_text', 'privacy_text', 'booking_policy_text')}
        widgets = {name: forms.Textarea(attrs={'rows': 12})
                   for name in ('terms_text', 'privacy_text', 'booking_policy_text')}


class NotificationSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['staff_notification_email', 'email_staff_on_booking', 'send_booking_reminders',
                  'reminder_hours_before']


class TourPackageForm(StyledFormMixin, forms.ModelForm):
    # Optional on the form: a blank value keeps the default of one guest, so existing tours are unaffected.
    min_guests = forms.IntegerField(min_value=1, required=False, initial=1, label='Smallest group (guests)',
                                    help_text='Clients must book at least this many guests.')

    class Meta:
        model = TourPackage
        fields = ['title', 'slug', 'category', 'destinations', 'summary', 'description', 'highlights',
                  'itinerary', 'start_location', 'end_location', 'included', 'excluded',
                  'price', 'duration_days', 'min_guests', 'max_guests', 'cancellation_policy',
                  'travel_instructions', 'image', 'image_url', 'is_featured', 'is_active']
        labels = {'is_active': 'Published (shown on the website)'}
        widgets = {name: forms.Textarea(attrs={'rows': 5}) for name in
                   ('highlights', 'itinerary', 'included', 'excluded', 'cancellation_policy', 'travel_instructions')}

    def clean_min_guests(self):
        return self.cleaned_data.get('min_guests') or 1

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get('min_guests'), cleaned.get('max_guests')
        if low and high and low > high:
            self.add_error('min_guests', 'The smallest group cannot be larger than the largest group.')
        return cleaned


class CategoryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'slug', 'description']


class FAQForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = FAQ
        fields = ['question', 'answer', 'order', 'is_active']


class CultureItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CultureItem
        fields = ['icon', 'title', 'text', 'order', 'is_active']


class ReviewAdminForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Review
        fields = ['rating', 'comment', 'is_published']


class SiteSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        # Home page and site text. Contact details, About text and social links have their own forms.
        # The website name is on the Branding form; the staff email is on the Email settings form.
        fields = [
            'footer_tagline',
            'hero_eyebrow', 'hero_title', 'hero_text',
            'intro_kicker', 'intro_title', 'intro_text',
            'culture_intro', 'history_note',
            'booking_notice', 'final_cta_title', 'final_cta_text',
            'payment_instructions',
            'hosted_by_name', 'hosted_by_logo', 'hosted_by_url',
        ]


# ---------- account management (administrators) ----------

class AdminAccountForm(StyledFormMixin, UserCreationForm):
    """Creates an administrator account. Only reachable from the admin dashboard."""

    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email


class AdminCustomerForm(StyledFormMixin, forms.ModelForm):
    """Edits a customer's name, email and whether the account is active."""

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'is_active')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email


class TourScheduleForm(StyledFormMixin, forms.ModelForm):
    """One bookable date and time for a tour. Administrators set capacity and open/closed status."""

    class Meta:
        model = TourSchedule
        fields = ['package', 'date', 'start_time', 'capacity', 'is_open', 'notes']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
        }
        labels = {'capacity': 'Maximum clients (seats)'}

    def clean_capacity(self):
        capacity = self.cleaned_data['capacity']
        booked = self.instance.seats_booked if self.instance.pk else 0
        if capacity < booked:
            raise forms.ValidationError(
                f'Capacity cannot be lower than the {booked} seat(s) already booked for this slot.')
        return capacity

    def clean_date(self):
        day = self.cleaned_data['date']
        if not self.instance.pk and day < timezone.localdate():
            raise forms.ValidationError('New tour dates must be today or later.')
        return day


WEEKDAY_CHOICES = [('0', 'Mon'), ('1', 'Tue'), ('2', 'Wed'), ('3', 'Thu'), ('4', 'Fri'), ('5', 'Sat'), ('6', 'Sun')]


class TourScheduleBulkForm(StyledFormMixin, forms.Form):
    """Create many dates at once, for example every Saturday for three months."""

    MAX_DAYS = 366

    package = forms.ModelChoiceField(queryset=TourPackage.objects.all(), label='Tour')
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    weekdays = forms.MultipleChoiceField(choices=WEEKDAY_CHOICES, widget=forms.CheckboxSelectMultiple,
                                         label='Days of the week')
    start_time = forms.TimeField(initial='09:00', widget=forms.TimeInput(attrs={'type': 'time'}),
                                 label='Start time')
    capacity = forms.IntegerField(min_value=1, max_value=500, initial=10, label='Maximum clients (seats)')
    is_open = forms.BooleanField(required=False, initial=True, label='Open for booking straight away')

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_date'), cleaned.get('end_date')
        if start and end:
            if end < start:
                raise forms.ValidationError('The end date must be after the start date.')
            if (end - start).days >= self.MAX_DAYS:
                raise forms.ValidationError(f'Create at most {self.MAX_DAYS} days at a time.')
            if start < timezone.localdate():
                raise forms.ValidationError('Start date must be today or later.')
        return cleaned


class ScheduleRuleForm(StyledFormMixin, forms.ModelForm):
    """
    The automatic timetable for one tour: start times, how long each group is on tour, the operating
    days, the daily capacity and the group sizes. Group sizes are saved on the tour itself.
    """

    min_guests = forms.IntegerField(min_value=1, max_value=50, label='Smallest group (guests)')
    max_guests = forms.IntegerField(min_value=1, max_value=50, label='Largest group (guests)')
    weekdays = forms.MultipleChoiceField(choices=WEEKDAY_CHOICES, widget=forms.CheckboxSelectMultiple,
                                         label='Operating days')

    class Meta:
        model = ScheduleRule
        fields = ['default_start', 'last_start', 'interval_hours', 'tour_hours', 'daily_capacity', 'days_ahead']
        widgets = {
            'default_start': forms.TimeInput(attrs={'type': 'time'}),
            'last_start': forms.TimeInput(attrs={'type': 'time'}),
        }
        labels = {
            'default_start': 'First start time',
            'last_start': 'Last start time',
            'interval_hours': 'Hours between start times',
            'tour_hours': 'Tour length (hours)',
            'daily_capacity': 'Daily capacity (guests per date)',
            'days_ahead': 'Open dates this many days ahead',
        }
        help_texts = {
            'interval_hours': 'For example 3 gives 08:00, 11:00, 14:00 and 17:00.',
            'tour_hours': 'A group occupies the tour for this long. Starts that would overlap it are not offered.',
            'daily_capacity': 'The most guests the tour takes on one date, across all of its start times.',
        }

    def __init__(self, *args, package, **kwargs):
        super().__init__(*args, **kwargs)
        self.package = package
        self.fields['min_guests'].initial = package.min_guests
        self.fields['max_guests'].initial = package.max_guests
        self.fields['weekdays'].initial = sorted(self.instance.operating_days.split(','))

    def clean(self):
        cleaned = super().clean()  # the model checks that the last start time is not before the first
        low, high = cleaned.get('min_guests'), cleaned.get('max_guests')
        if low and high and low > high:
            self.add_error('min_guests', 'The smallest group cannot be larger than the largest group.')
        daily = cleaned.get('daily_capacity')
        if high and daily and daily < high:
            self.add_error('daily_capacity',
                           f'The daily capacity must hold at least one largest group ({high} guests).')
        return cleaned

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.package = self.package
        rule.operating_days = ','.join(sorted(self.cleaned_data['weekdays']))
        if commit:
            rule.save()
            self.package.min_guests = self.cleaned_data['min_guests']
            self.package.max_guests = self.cleaned_data['max_guests']
            self.package.save(update_fields=['min_guests', 'max_guests'])
        return rule


class BroadcastForm(StyledFormMixin, forms.Form):
    title = forms.CharField(max_length=160, label='Notification title')
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}), max_length=2000)


class PaymentStatusForm(StyledFormMixin, forms.ModelForm):
    """Staff set a booking's payment status: Unpaid, Partially paid, Paid or Payment verified."""

    class Meta:
        model = Booking
        fields = ('payment_status',)
        labels = {'payment_status': 'Payment status'}


class CoHostForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CoHost
        fields = ['name', 'logo', 'description', 'website_url', 'order', 'is_active']
        widgets = {'description': forms.TextInput()}

    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and logo.size > MAX_LOGO_BYTES:
            raise forms.ValidationError('The logo must be 5 MB or smaller.')
        return logo


# ---------- Control Panel management forms (every area has its own form) ----------

class ContactSettingsForm(StyledFormMixin, forms.ModelForm):
    """Main contact details, WhatsApp and the payment phone number."""

    class Meta:
        model = SiteSettings
        fields = ['contact_intro', 'contact_email', 'contact_phone', 'contact_address',
                  'whatsapp_number', 'payment_phone']
        labels = {'contact_intro': 'Introduction on the Contact page'}

    def clean_whatsapp_number(self):
        digits = re.sub(r'[\s+\-()]', '', self.cleaned_data.get('whatsapp_number') or '')
        if digits and not digits.isdigit():
            raise forms.ValidationError('Use digits only, in international format, for example 250788000000.')
        if digits and not 8 <= len(digits) <= 15:
            raise forms.ValidationError('A WhatsApp number has between 8 and 15 digits.')
        return digits


class AboutTextForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['about_title', 'about_text', 'mission', 'vision']


class DetailsPageForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DetailsPage
        fields = ['title', 'intro', 'body', 'is_published']


class AboutSectionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = AboutSection
        fields = ['heading', 'body', 'image', 'order', 'is_active']


class ContactDetailForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ContactDetail
        fields = ['kind', 'label', 'value', 'order', 'is_active']


def _with_scheme(url):
    """Accounts and links are stored with https://. A bare 'example.com' becomes https://example.com."""
    url = (url or '').strip()
    if url.lower().startswith('http://'):
        url = 'https://' + url[len('http://'):]
    elif url and not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    return url


class SocialLinkForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SocialLink
        fields = ['platform', 'label', 'url', 'order', 'is_active']
        labels = {'url': 'Account link'}

    def clean_url(self):
        url = _with_scheme(self.cleaned_data.get('url'))
        if not url:
            raise forms.ValidationError('Enter the full link to the account.')
        return url


class DetailPhotoForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DetailPhoto
        fields = ['image', 'caption', 'order', 'is_active']


class DetailBookForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DetailBook
        fields = ['title', 'author', 'description', 'link', 'order', 'is_active']

    def clean_link(self):
        return _with_scheme(self.cleaned_data.get('link'))


class DetailLinkForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DetailLink
        fields = ['title', 'url', 'description', 'order', 'is_active']

    def clean_url(self):
        url = _with_scheme(self.cleaned_data.get('url'))
        if not url:
            raise forms.ValidationError('Enter the web address.')
        return url


class DetailSectionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DetailSection
        fields = ['heading', 'body', 'order', 'is_active']


class BackgroundImageForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BackgroundImage
        fields = ['image']


class AdminBookingForm(StyledFormMixin, forms.Form):
    """Administrators change a booking: its slot, guests, duration and the client's details."""

    schedule = forms.ModelChoiceField(queryset=TourSchedule.objects.none(), label='Date and start time')
    guests = forms.IntegerField(min_value=1, label='Number of guests')
    duration_hours = forms.TypedChoiceField(
        choices=[(h, f"{h} hour{'s' if h != 1 else ''}") for h in range(1, MAX_BOOKING_HOURS + 1)],
        coerce=int, label='Duration')
    full_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=30)
    country = forms.CharField(max_length=80)
    special_requests = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)
    destination = forms.ModelChoiceField(queryset=Destination.objects.none(), required=False,
                                         label='Destination the client will visit')

    def __init__(self, *args, package, **kwargs):
        super().__init__(*args, **kwargs)
        places = package.destinations.filter(is_active=True).order_by('name')
        self.fields['destination'].queryset = places if places.exists() else Destination.objects.filter(
            is_active=True).order_by('name')
        self.fields['schedule'].queryset = TourSchedule.objects.filter(package=package).order_by('date', 'start_time')
        self.fields['schedule'].label_from_instance = (
            lambda s: f'{s.date:%a %d %b %Y} at {s.start_time:%H:%M} · {s.seats_remaining} seats free')
