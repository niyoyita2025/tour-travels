"""
Website Management areas added to the Control Panel: branding (name, logo, favicon), policies, and the
Email centre (Gmail status, notification settings, a test email and the delivery log). Plus the public
policy pages. Every management view is administrator-only.
"""

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import notifications
from .email_diagnostics import check_sign_in
from .emails import Message, absolute, deliver
from .forms import BrandingForm, NotificationSettingsForm, PoliciesForm
from .models import EmailLog, SiteSettings
from .views import staff_required

SMTP_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

from .context_processors import POLICY_PAGES  # noqa: E402  slug -> (SiteSettings field, page title)


def _single_form(request, form_class, template, done_message, activity, extra=None):
    """Edit the one SiteSettings row through `form_class`, then come back to the same page."""
    site = SiteSettings.load()
    if request.method == 'POST':
        form = form_class(request.POST, request.FILES, instance=site)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, activity, done_message)
            messages.success(request, done_message + ' The website is updated.')
            return redirect(request.path)
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = form_class(instance=site)
    return render(request, template, {'form': form, 'site': site, **(extra or {})})


@staff_required
def branding_manage(request):
    return _single_form(request, BrandingForm, 'manage/branding.html', 'Branding saved.', 'content.branding')


@staff_required
@require_POST
def branding_remove(request, field):
    """Remove the logo, dark logo or favicon, so the default letter mark or browser icon is used again."""
    if field not in ('logo', 'logo_dark', 'favicon'):
        raise Http404
    site = SiteSettings.load()
    image = getattr(site, field)
    if image:
        image.delete(save=False)
        setattr(site, field, '')
        site.save()
        notifications.log_activity(request.user, 'content.branding', f'Removed the {field.replace("_", " ")}')
        messages.success(request, 'Image removed. The default is shown again.')
    return redirect('branding_manage')


@staff_required
def policies_manage(request):
    site = SiteSettings.load()
    # Only policies with text have a public page to open.
    pages = [(slug, title) for slug, (field, title) in POLICY_PAGES.items() if getattr(site, field).strip()]
    return _single_form(request, PoliciesForm, 'manage/policies.html', 'Policies saved.', 'content.policies',
                        {'pages': pages})


def policy_page(request, slug):
    """Public terms, privacy and booking-policy pages. A page with no text is hidden."""
    if slug not in POLICY_PAGES:
        raise Http404
    field, title = POLICY_PAGES[slug]
    text = getattr(SiteSettings.load(), field).strip()
    if not text:
        raise Http404
    paragraphs = [block.strip() for block in text.split('\n\n') if block.strip()]
    return render(request, 'policy.html', {'title': title, 'paragraphs': paragraphs})


def gmail_status():
    """What the Email centre shows about the mail setup. Never includes the password itself."""
    smtp = settings.EMAIL_BACKEND == SMTP_BACKEND
    brevo = getattr(settings, 'EMAIL_PROVIDER', '') == 'brevo'
    return {
        'brevo': brevo,
        'brevo_key_set': bool(getattr(settings, 'BREVO_API_KEY', '')),  # yes or no only, never the key
        'smtp': smtp,
        'backend': settings.EMAIL_BACKEND.rsplit('.', 2)[-2],
        'host': f'{settings.EMAIL_HOST}:{settings.EMAIL_PORT}',
        'tls': settings.EMAIL_USE_TLS,
        'user': settings.EMAIL_HOST_USER,
        'password_set': bool(settings.EMAIL_HOST_PASSWORD),
        'sender': settings.DEFAULT_FROM_EMAIL,
        'ready': brevo or (not smtp) or bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD),
    }


@staff_required
def email_center(request):
    site = SiteSettings.load()
    if request.method == 'POST' and request.POST.get('action') == 'check':
        ok, message = check_sign_in()
        notifications.log_activity(request.user, 'email.check',
                                   'Gmail sign-in check: ' + ('accepted' if ok else 'refused'))
        (messages.success if ok else messages.error)(request, message)
        return redirect('email_center')
    if request.method == 'POST' and request.POST.get('action') == 'settings':
        form = NotificationSettingsForm(request.POST, instance=site)
        if form.is_valid():
            form.save()
            notifications.log_activity(request.user, 'email.settings', 'Email notification settings updated')
            messages.success(request, 'Notification settings saved.')
            return redirect('email_center')
        messages.error(request, 'Please fix the highlighted fields.')
    else:
        form = NotificationSettingsForm(instance=site)
    logs = EmailLog.objects.all()
    status_filter = request.GET.get('status', '')
    if status_filter in EmailLog.Status.values:
        logs = logs.filter(status=status_filter)
    page = Paginator(logs, 30).get_page(request.GET.get('page'))
    return render(request, 'manage/emails.html', {
        'form': form,
        'gmail': gmail_status(),
        'page': page,
        'status_filter': status_filter,
        'failed_count': EmailLog.objects.filter(status=EmailLog.Status.FAILED).count(),
        'sent_count': EmailLog.objects.filter(status=EmailLog.Status.SENT).count(),
    })


@staff_required
@require_POST
def email_test(request):
    """Send a test email to the signed-in administrator, and say truthfully whether Gmail accepted it."""
    to = request.user.email
    if not to:
        messages.error(request, 'Your account has no email address. Add one on your profile first.')
        return redirect('email_center')
    site = SiteSettings.load()
    sent = deliver(Message(kind='test', to=to, subject=f'{site.site_name}: test email', title='Email is working',
                           paragraphs=['This is a test from the Email centre. If you can read it, the website '
                                       'can send emails through Gmail.'],
                           button_url=absolute('/'), button_text='Open the website'))
    if sent:
        messages.success(request, f'Gmail accepted the test email to {to}. Check the inbox and the spam folder.')
    else:
        failure = EmailLog.objects.filter(recipient=to, status=EmailLog.Status.FAILED).first()
        messages.error(request, 'The test email was not sent. ' + (failure.error if failure else ''))
    return redirect('email_center')
