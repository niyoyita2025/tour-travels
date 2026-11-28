from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import BackgroundImage, CoHost, Destination, SiteSettings, SocialLink, UserProfile
from .roles import is_admin_user

POLICY_PAGES = {  # slug -> (SiteSettings field, title). The public pages are in website_views.policy_page.
    'terms': ('terms_text', 'Terms and conditions'),
    'privacy': ('privacy_text', 'Privacy policy'),
    'booking-policy': ('booking_policy_text', 'Booking and cancellation policy'),
}


def _initials(name):
    parts = name.split()
    return ''.join(part[0] for part in parts[:2]).upper() or '?'


def site_settings(request):
    """Website text, contact details, backgrounds, footer content and the role layout for every template."""
    site = SiteSettings.load()
    user = getattr(request, 'user', None)

    context = {
        'site': site,
        'site_name': site.site_name,
        'whatsapp_number': site.whatsapp_number,
        'payment_instructions': site.payment_instructions,
        'current_year': timezone.localdate().year,
        'hold_hours': settings.BOOKING_HOLD_HOURS,
        'cancel_hours': settings.CANCELLATION_NOTICE_HOURS,
        # Background photos, keyed by section (empty string when a section has none)
        'backgrounds': {image.section: image.url for image in BackgroundImage.objects.exclude(image='')},
        # Footer content
        'footer_social': SocialLink.objects.filter(is_active=True),
        'footer_destinations': Destination.objects.filter(is_active=True).order_by('-is_featured', 'name')[:4],
        'footer_cohosts': CoHost.objects.filter(is_active=True),
        'footer_hosted': bool(site.hosted_by_name or site.hosted_by_logo),
        # Policy pages that have text (an empty policy has no page and no link)
        'footer_policies': [(slug, title) for slug, (field, title) in POLICY_PAGES.items()
                            if getattr(site, field).strip()],
        # Role information used by templates.
        'is_admin_user': is_admin_user(user),
        'layout': 'base.html',
        'dash_label': '',
        'dash_home': reverse('home'),
        'unread_count': 0,
        # The signed-in person's name and photo, for the header and dashboard. Empty when signed out.
        'me_name': '',
        'me_first': '',
        'me_initials': '',
        'me_photo': '',
    }
    if user and user.is_authenticated:
        if is_admin_user(user):
            context.update(layout='layouts/admin.html', dash_label='Administrator',
                           dash_home=reverse('admin_dashboard'))
        else:
            context.update(layout='layouts/client.html', dash_label='My account',
                           dash_home=reverse('dashboard'))
        name = user.get_full_name().strip() or user.username
        profile = UserProfile.objects.filter(user=user).first()
        context.update(
            me_name=name,
            me_first=user.first_name or user.username,
            me_initials=_initials(name),
            me_photo=profile.photo_url if profile else '',
        )
        context['unread_count'] = user.notifications.filter(is_read=False).count()
    return context
