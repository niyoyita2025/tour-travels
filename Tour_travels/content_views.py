"""
Staff content management. Every item that appears on the public website can be
created, edited, published/unpublished and deleted here, without the Django admin.
"""

from django.contrib import messages
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import notifications
from .destination_tours import auto_tours_blocking, ensure_destination_tour, remove_auto_tours
from .forms import (
    AboutSectionForm, CategoryForm, CoHostForm, ContactDetailForm, CultureItemForm, DestinationForm,
    DestinationPhotoForm, DetailBookForm, DetailLinkForm, DetailPhotoForm, DetailSectionForm, FAQForm,
    ReviewAdminForm, SiteSettingsForm, SocialLinkForm, TestimonialForm, TourPackageForm, TourPhotoForm,
)
from .models import (
    FAQ, AboutSection, Category, CoHost, ContactDetail, CultureItem, Destination, DestinationPhoto, DetailBook,
    DetailLink, DetailPhoto, DetailSection, Review, SiteSettings, SocialLink, Testimonial, TourPackage, TourPhoto,
)
from .views import staff_required

# Each section: model, form, what to show in the list, and help text.
# columns: (heading, attribute name on the object)
SECTIONS = {
    'destinations': {
        'model': Destination, 'form': DestinationForm, 'title': 'Destinations', 'singular': 'destination',
        'help': 'Places shown on the destinations page and the home page.',
        'columns': [('Name', 'name'), ('Location', 'location'), ('Featured', 'is_featured'),
                    ('Published', 'is_active')],
        'can_add': True,
    },
    'tours': {
        'model': TourPackage, 'form': TourPackageForm, 'title': 'Tours', 'singular': 'tour',
        'help': 'Tour packages, prices and details. Unpublished tours are hidden from customers.',
        'columns': [('Title', 'title'), ('Category', 'category'), ('Price (USD)', 'price'),
                    ('Featured', 'is_featured'), ('Published', 'is_active')],
        'can_add': True,
    },
    'destination_photos': {
        'model': DestinationPhoto, 'form': DestinationPhotoForm, 'title': 'Destination galleries',
        'singular': 'destination photo',
        'help': 'Extra photos shown in the gallery on each destination page. The main photo is set on the '
                'destination itself.',
        'columns': [('Destination', 'destination'), ('Caption', 'caption'), ('Order', 'order'),
                    ('Shown', 'is_active')],
        'can_add': True,
    },
    'tour_photos': {
        'model': TourPhoto, 'form': TourPhotoForm, 'title': 'Tour galleries', 'singular': 'tour photo',
        'help': 'Extra photos shown in the gallery on each tour page. The main photo is set on the tour itself.',
        'columns': [('Tour', 'package'), ('Caption', 'caption'), ('Order', 'order'), ('Shown', 'is_active')],
        'can_add': True,
    },
    'testimonials': {
        'model': Testimonial, 'form': TestimonialForm, 'title': 'Testimonials', 'singular': 'testimonial',
        'help': 'Quotes from past guests shown on the home page. Tour reviews written by customers are '
                'managed under Reviews.',
        'columns': [('Name', 'name'), ('From', 'origin'), ('Rating', 'rating'), ('Order', 'order'),
                    ('Published', 'is_active')],
        'can_add': True,
    },
    'categories': {
        'model': Category, 'form': CategoryForm, 'title': 'Categories', 'singular': 'category',
        'help': 'Groups used to filter tours.',
        'columns': [('Name', 'name'), ('Slug', 'slug')],
        'can_add': True,
    },
    'faqs': {
        'model': FAQ, 'form': FAQForm, 'title': 'FAQs', 'singular': 'question',
        'help': 'Questions and answers on the FAQ page. Lower order numbers appear first.',
        'columns': [('Question', 'question'), ('Order', 'order'), ('Published', 'is_active')],
        'can_add': True,
    },
    'culture': {
        'model': CultureItem, 'form': CultureItemForm, 'title': 'Culture cards', 'singular': 'culture card',
        'help': 'Cards in the "Discover the culture" section on the home page.',
        'columns': [('Icon', 'icon'), ('Title', 'title'), ('Order', 'order'), ('Published', 'is_active')],
        'can_add': True,
    },
    'social': {
        'model': SocialLink, 'form': SocialLinkForm, 'title': 'Social media', 'singular': 'social account',
        'help': 'Facebook, Instagram, X and other accounts. Each active account appears as an icon in the '
                'footer and opens in a new tab.',
        'columns': [('Platform', 'get_platform_display'), ('Label', 'display_label'), ('Link', 'url'),
                    ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'contact_details': {
        'model': ContactDetail, 'form': ContactDetailForm, 'title': 'Other contact details',
        'singular': 'contact detail',
        'help': 'Extra phone lines, emails, opening hours and offices shown on the Contact page.',
        'columns': [('Kind', 'get_kind_display'), ('Label', 'label'), ('Value', 'value'),
                    ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'about_sections': {
        'model': AboutSection, 'form': AboutSectionForm, 'title': 'About page sections',
        'singular': 'section',
        'help': 'Blocks of text, with an optional photo, shown on the About page in the order you set.',
        'columns': [('Heading', 'heading'), ('Photo', 'image'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'detail_photos': {
        'model': DetailPhoto, 'form': DetailPhotoForm, 'title': 'Details: photo gallery', 'singular': 'photo',
        'help': 'Photographs shown in the gallery on the Details page.',
        'columns': [('Caption', 'caption'), ('Photo', 'image'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'detail_books': {
        'model': DetailBook, 'form': DetailBookForm, 'title': 'Details: recommended books', 'singular': 'book',
        'help': 'Books to read before or after your visit. Each can link to where to find it.',
        'columns': [('Title', 'title'), ('Author', 'author'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'detail_links': {
        'model': DetailLink, 'form': DetailLinkForm, 'title': 'Details: useful links', 'singular': 'link',
        'help': 'External websites shown on the Details page. They open in a new tab.',
        'columns': [('Title', 'title'), ('Address', 'url'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'detail_sections': {
        'model': DetailSection, 'form': DetailSectionForm, 'title': 'Details: sections', 'singular': 'section',
        'help': 'Any other information for the Details page: a heading and its text.',
        'columns': [('Heading', 'heading'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'cohosts': {
        'model': CoHost, 'form': CoHostForm, 'title': 'Co-hosts', 'singular': 'co-host',
        'help': 'Partners shown in the footer with their logo and name. Hide a co-host with Active. '
                'When no co-host is active the footer section is hidden.',
        'columns': [('Name', 'name'), ('Order', 'order'), ('Active', 'is_active')],
        'can_add': True,
    },
    'reviews': {
        'model': Review, 'form': ReviewAdminForm, 'title': 'Reviews', 'singular': 'review',
        'help': 'Guest reviews. Untick Published to hide a review without deleting it. '
                'Customers write reviews from the tour page.',
        'columns': [('Tour', 'package'), ('Guest', 'user'), ('Rating', 'rating'), ('Published', 'is_published')],
        'can_add': False,
    },
}


def _section(key):
    if key not in SECTIONS:
        raise Http404
    return SECTIONS[key]


def _cell(obj, attr):
    value = getattr(obj, attr)
    if callable(value):
        value = value()
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if value is None or value == '':
        return '—'
    return value


@staff_required
def content_index(request):
    sections = [
        {'key': key, 'title': cfg['title'], 'help': cfg['help'], 'count': cfg['model'].objects.count()}
        for key, cfg in SECTIONS.items()
    ]
    return render(request, 'content/index.html', {'sections': sections})


@staff_required
def content_list(request, section):
    cfg = _section(section)
    objects = cfg['model'].objects.all()
    rows = [(obj, [_cell(obj, attr) for _, attr in cfg['columns']]) for obj in objects]
    return render(request, 'content/list.html', {
        'section': section,
        'cfg': cfg,
        'columns': [label for label, _ in cfg['columns']],
        'rows': rows,
    })


@staff_required
def content_form(request, section, pk=None):
    cfg = _section(section)
    if pk is None and not cfg['can_add']:
        messages.error(request, f'New {cfg["title"].lower()} are created by customers. You can edit or hide them here.')
        return redirect('content_list', section=section)
    obj = get_object_or_404(cfg['model'], pk=pk) if pk is not None else None
    if request.method == 'POST':
        form = cfg['form'](request.POST, request.FILES, instance=obj)
        if form.is_valid():
            item = form.save()
            verb = 'Updated' if obj else 'Created'
            notifications.log_activity(request.user, f'content.{section}.saved', f'{verb} {cfg["singular"]}: {item}')
            extra = ''
            if section == 'destinations' and obj is None:
                # Every new destination gets its own bookable tour, with open dates, straight away.
                tour = ensure_destination_tour(item)
                extra = f' Its bookable tour "{tour.title}" is ready with open dates.'
            messages.success(request, f'{verb} {cfg["singular"]}: {item}.{extra}')
            return redirect('content_list', section=section)
        messages.error(request, 'Please fix the highlighted fields and try again.')
    else:
        form = cfg['form'](instance=obj)
    return render(request, 'content/form.html', {'section': section, 'cfg': cfg, 'form': form, 'obj': obj})


@staff_required
@require_POST
def content_delete(request, section, pk):
    cfg = _section(section)
    obj = get_object_or_404(cfg['model'], pk=pk)
    name = str(obj)
    if section == 'destinations':
        # Tours are never changed by deleting a destination. Only the automatic tour made for it goes with it,
        # and only when nobody has booked it and it visits no other place. Anything else refuses the deletion.
        blocking = auto_tours_blocking(obj)
        if blocking:
            count = len(blocking)
            messages.error(request, f'"{name}" is visited by {count} tour{"s" if count != 1 else ""} that must stay '
                                    '(they have bookings or other places), so it cannot be deleted. Untick Active '
                                    'to hide it. Its tours stay as they are.')
            return redirect('content_list', section=section)
    try:
        with transaction.atomic():
            if section == 'destinations':
                remove_auto_tours(obj)
            obj.delete()
    except ProtectedError:
        messages.error(request, f'"{name}" cannot be deleted because bookings use it. '
                                'Untick Published to hide it instead.')
    else:
        notifications.log_activity(request.user, f'content.{section}.deleted', f'Deleted {cfg["singular"]}: {name}')
        messages.success(request, f'Deleted "{name}".')
    return redirect('content_list', section=section)


@staff_required
def site_settings_edit(request):
    site = SiteSettings.load()
    if request.method == 'POST':
        form = SiteSettingsForm(request.POST, request.FILES, instance=site)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'content.site_settings', 'Website text and settings updated')
            messages.success(request, 'Website text and settings saved. The changes are live.')
            return redirect('site_settings')
        messages.error(request, 'Please fix the highlighted fields and try again.')
    else:
        form = SiteSettingsForm(instance=site)
    return render(request, 'content/site_settings.html', {'form': form})
