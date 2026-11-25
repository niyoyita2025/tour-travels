"""
Control Panel areas for the public pages: background images, contact, About, and the Details page.
Each area saves its own form to the database. Public pages read the same rows, so changes appear
on the website as soon as they are saved.

Every view here is administrator-only (see views.staff_required).
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import notifications
from .forms import AboutTextForm, BackgroundImageForm, ContactSettingsForm, DetailsPageForm
from .image_frames import frame_for
from .models import AboutSection, BackgroundImage, ContactDetail, DetailBook, DetailLink, DetailPhoto, \
    DetailSection, DetailsPage, SiteSettings
from .views import staff_required


# ---------- background images ----------

BACKGROUND_SECTIONS = list(BackgroundImage.Section)


@staff_required
def background_manage(request):
    existing = {row.section: row for row in BackgroundImage.objects.all()}
    rows = []
    for section in BACKGROUND_SECTIONS:
        row = existing.get(section.value)
        width, height = frame_for(section.value)
        rows.append({
            'key': section.value,
            'label': section.label,
            'width': width,
            'height': height,
            'image': row.image if row and row.image else None,
            'url': row.url if row else '',
            'form': BackgroundImageForm(instance=row) if row else BackgroundImageForm(),
        })
    return render(request, 'manage/backgrounds.html', {'rows': rows})


@staff_required
@require_POST
def background_save(request, section):
    if section not in BackgroundImage.Section.values:
        raise Http404
    row, _ = BackgroundImage.objects.get_or_create(section=section)
    label = BackgroundImage.Section(section).label
    if request.POST.get('action') == 'remove':
        if row.image:
            row.image.delete(save=False)
        row.image = ''
        row.save()
        notifications.log_activity(request.user, 'background.removed', f'Background removed: {label}')
        messages.success(request, f'Background removed from {label}. The default design is shown again.')
    else:
        old_name = row.image.name if row.image else ''
        form = BackgroundImageForm(request.POST, request.FILES, instance=row)
        if not request.FILES.get('image'):
            messages.error(request, 'Choose an image file first (JPG, PNG or WebP, up to 5 MB).')
        elif form.is_valid():
            form.save()
            if old_name and old_name != row.image.name:
                row.image.storage.delete(old_name)  # replace: remove the previous file
            notifications.log_activity(request.user, 'background.saved', f'Background updated: {label}')
            width, height = frame_for(section)
            messages.success(request, f'Background saved for {label}, cropped to {width} × {height} px. It is live now.')
        else:
            messages.error(request, ' '.join(e for errors in form.errors.values() for e in errors))
    return redirect('background_manage')


# ---------- contact ----------

@staff_required
def contact_manage(request):
    site = SiteSettings.load()
    if request.method == 'POST':
        form = ContactSettingsForm(request.POST, instance=site)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'content.contact', 'Contact details updated')
            messages.success(request, 'Contact details saved. The website is updated.')
            return redirect('contact_manage')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = ContactSettingsForm(instance=site)
    return render(request, 'manage/contact.html', {
        'form': form,
        'extra_contacts': ContactDetail.objects.all(),
    })


# ---------- About ----------

@staff_required
def about_manage(request):
    site = SiteSettings.load()
    if request.method == 'POST':
        form = AboutTextForm(request.POST, instance=site)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'content.about', 'About page text updated')
            messages.success(request, 'About page saved. The website is updated.')
            return redirect('about_manage')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = AboutTextForm(instance=site)
    return render(request, 'manage/about.html', {
        'form': form,
        'sections': AboutSection.objects.all(),
    })


# ---------- Details ----------

@staff_required
def details_manage(request):
    page = DetailsPage.load()
    if request.method == 'POST':
        form = DetailsPageForm(request.POST, instance=page)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'content.details', 'Details page updated')
            messages.success(request, 'Details page saved.')
            return redirect('details_manage')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = DetailsPageForm(instance=page)
    return render(request, 'manage/details.html', {
        'form': form,
        'page': page,
        'counts': {
            'photos': DetailPhoto.objects.count(),
            'books': DetailBook.objects.count(),
            'links': DetailLink.objects.count(),
            'sections': DetailSection.objects.count(),
        },
    })


def details_page(request):
    """The public Details page."""
    page = DetailsPage.load()
    if not page.is_published:
        raise Http404
    return render(request, 'details.html', {
        'page': page,
        'photos': DetailPhoto.objects.filter(is_active=True),
        'books': DetailBook.objects.filter(is_active=True),
        'links': DetailLink.objects.filter(is_active=True),
        'sections': DetailSection.objects.filter(is_active=True),
    })
