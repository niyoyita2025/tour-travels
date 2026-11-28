from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import notifications
from .forms import (
    BookingForm, BookingStatusForm, InquiryForm, PaymentForm, PaymentStatusForm,
    ReviewForm, TourSearchForm,
)
from .models import (
    FAQ, ActivityLog, AboutSection, Booking, Category, ContactDetail, CultureItem, Destination, Inquiry,
    Notification, Payment, Review, Testimonial, TourPackage,
)
from .bookings import BookingUnavailable, expire_stale_holds, set_booking_status
from .destination_tours import top_up_dates
from .calendar_service import (
    booking_statistics, next_bookable_slots, slot_status, upcoming_bookable_slots,
)
from .roles import is_admin_user


# ---------- access control ----------

def staff_required(view):
    """
    Administrator-only views (the Admin Dashboard and everything under /manage/).

    * Anonymous visitors are sent to the administrator sign-in page.
    * Signed-in clients get a 403 Forbidden page. They never see admin controls.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url=reverse('login'))
        if not is_admin_user(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def _customer_booking(request, reference):
    """Booking the current user may view: their own, or any booking for an administrator."""
    qs = Booking.objects.select_related('package', 'user')
    if is_admin_user(request.user):
        return get_object_or_404(qs, reference=reference)
    return get_object_or_404(qs, reference=reference, user=request.user)


# ---------- public pages ----------

def page_not_found(request, exception=None):
    return render(request, '404.html', status=404)


def permission_denied(request, exception=None):
    return render(request, '403.html', status=403)


def home(request):
    destinations = Destination.objects.filter(is_active=True).order_by('-is_featured', 'name')[:8]
    packages = TourPackage.objects.filter(is_active=True).order_by('-is_featured', 'price')[:3]
    return render(request, 'home.html', {
        'destinations': destinations,
        'packages': packages,
        'culture_items': CultureItem.objects.filter(is_active=True),
        'testimonials': Testimonial.objects.filter(is_active=True)[:6],
        'categories': Category.objects.all(),
        'form': InquiryForm(),
    })


def about(request):
    return render(request, 'about.html', {'sections': AboutSection.objects.filter(is_active=True)})


def faq(request):
    return render(request, 'faq.html', {'faqs': FAQ.objects.filter(is_active=True)})


def destination_list(request):
    q = request.GET.get('q', '').strip()
    destinations = Destination.objects.filter(is_active=True)
    if q:
        destinations = destinations.filter(Q(name__icontains=q) | Q(location__icontains=q) | Q(description__icontains=q))
    page = Paginator(destinations, 9).get_page(request.GET.get('page'))
    return render(request, 'destinations.html', {'page': page, 'q': q})


def destination_detail(request, slug):
    destination = get_object_or_404(Destination, slug=slug, is_active=True)
    # The place first (details, gallery, map), then the published tours that visit it, with prices and
    # their next open date, each linking to its booking calendar.
    tours = _with_next_dates(destination.packages.filter(is_active=True).order_by('price', 'title'))
    return render(request, 'destination_detail.html', {
        'destination': destination,
        'photos': destination.photos.filter(is_active=True),
        'tours': tours,
    })


def _with_next_dates(tours):
    """Give each tour its next open date (`next_slot`) and how full that date is (`next_status`)."""
    tours = list(tours)
    best = next_bookable_slots([tour.pk for tour in tours])
    for tour in tours:
        tour.next_slot = best.get(tour.pk)
        tour.next_status = slot_status(tour.next_slot) if tour.next_slot else None
    return tours


def package_list(request):
    form = TourSearchForm(request.GET or None)
    packages = TourPackage.objects.filter(is_active=True).select_related('category').prefetch_related('destinations')
    if form.is_valid():
        data = form.cleaned_data
        if data['q']:
            packages = packages.filter(
                Q(title__icontains=data['q']) | Q(summary__icontains=data['q'])
                | Q(description__icontains=data['q']) | Q(destinations__name__icontains=data['q'])).distinct()
        if data['category']:
            packages = packages.filter(category=data['category'])
        if data['min_price'] is not None:
            packages = packages.filter(price__gte=data['min_price'])
        if data['max_price'] is not None:
            packages = packages.filter(price__lte=data['max_price'])
        packages = packages.order_by(data['sort'] or 'price')
    # Category chips: every category that has active tours, with how many. New categories appear automatically.
    category_chips = (Category.objects
                      .annotate(tour_count=Count('packages', filter=Q(packages__is_active=True), distinct=True))
                      .filter(tour_count__gt=0)
                      .order_by('name'))
    active_category = form.cleaned_data.get('category') if form.is_valid() else None
    page = Paginator(packages, 9).get_page(request.GET.get('page'))
    page.object_list = _with_next_dates(page.object_list)  # each tour shows its next open date and Book now
    return render(request, 'packages.html', {
        'page': page,
        'form': form,
        'categories': Category.objects.all(),
        'category_chips': category_chips,
        'active_category': active_category,
    })


def package_detail(request, slug):
    package = get_object_or_404(TourPackage.objects.select_related('category').prefetch_related('destinations'),
                                slug=slug, is_active=True)
    reviews = package.reviews.filter(is_published=True).select_related('user')[:20]
    can_review = False
    review_form = None
    user_has_reviewed = False
    if request.user.is_authenticated:
        user_has_reviewed = package.reviews.filter(user=request.user).exists()
        has_trip = package.bookings.filter(
            user=request.user,
            status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED],
        ).exists()
        can_review = has_trip and not user_has_reviewed
        if can_review:
            review_form = ReviewForm()
    top_up_dates(package)  # automatic destination tours always have dates ahead
    open_dates = [{'slot': slot, 'status': slot_status(slot)} for slot in upcoming_bookable_slots(package)]
    my_bookings = []
    if request.user.is_authenticated:
        my_bookings = (request.user.bookings
                       .filter(package=package, status__in=[Booking.Status.PENDING, Booking.Status.CONFIRMED])
                       .select_related('schedule')
                       .order_by('travel_date'))
    return render(request, 'package_detail.html', {
        'package': package,
        'reviews': reviews,
        'can_review': can_review,
        'user_has_reviewed': user_has_reviewed,
        'review_form': review_form,
        'open_dates': open_dates,
        'next_date': open_dates[0] if open_dates else None,
        'my_bookings': my_bookings,
        'photos': package.photos.filter(is_active=True),
        'rule': getattr(package, 'schedule_rule', None),
    })


def contact(request):
    if request.method == 'POST':
        form = InquiryForm(request.POST)
        if form.is_valid():
            inquiry = form.save()
            notifications.inquiry_received(inquiry)
            messages.success(request, 'Thank you! Your message has been sent. We will reply by email soon.')
            return redirect('contact')
    else:
        initial = {}
        if request.user.is_authenticated:
            initial = {'name': request.user.get_full_name(), 'email': request.user.email}
        package_slug = request.GET.get('tour')
        if package_slug:
            pkg = TourPackage.objects.filter(slug=package_slug, is_active=True).first()
            if pkg:
                initial['package'] = pkg.pk
        form = InquiryForm(initial=initial)
    return render(request, 'contact.html', {
        'form': form,
        'contact_details': ContactDetail.objects.filter(is_active=True),
    })


# ---------- accounts ----------
# Sign-up, sign-in and password recovery live in accounts_views.py.

# The signed-in person's profile lives in profile_views.py.


@login_required
def dashboard(request):
    if is_admin_user(request.user):
        return redirect('admin_dashboard')  # administrators use their own dashboard
    bookings = request.user.bookings.select_related('package')
    today = timezone.localdate()
    context = {
        'bookings': bookings[:5],
        'total_bookings': bookings.count(),
        'upcoming_count': bookings.filter(travel_date__gte=today).exclude(
            status=Booking.Status.CANCELLED).count(),
        'total_spent': bookings.filter(status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED])
        .aggregate(s=Sum('total_price'))['s'] or 0,
        'review_count': request.user.reviews.count(),
        'recent_notifications': request.user.notifications.all()[:5],
    }
    return render(request, 'dashboard.html', context)


@login_required
def booking_history(request):
    bookings = request.user.bookings.select_related('package')
    status = request.GET.get('status', '')
    if status in Booking.Status.values:
        bookings = bookings.filter(status=status)
    page = Paginator(bookings, 10).get_page(request.GET.get('page'))
    return render(request, 'booking_history.html', {
        'page': page,
        'status': status,
        'status_choices': Booking.Status.choices,
    })


# ---------- booking flow ----------

@login_required
def booking_detail(request, reference):
    booking = _customer_booking(request, reference)
    is_admin = is_admin_user(request.user)
    # Payment is arranged outside the website. Staff record amounts received and set the status.
    return render(request, 'booking_detail.html', {
        'booking': booking,
        'payments': booking.payments.select_related('recorded_by'),
        'record_form': PaymentForm(booking=booking) if (is_admin and booking.balance_due > 0) else None,
        'payment_status_form': PaymentStatusForm(instance=booking) if is_admin else None,
        'booking_status_choices': Booking.Status.choices,
    })


@login_required
@require_POST
def cancel_booking(request, reference):
    booking = _customer_booking(request, reference)
    if booking.user_id != request.user.id:
        raise Http404
    if not booking.can_cancel:
        messages.error(request, 'This booking can no longer be cancelled online. Please contact us.')
    else:
        try:
            set_booking_status(booking, Booking.Status.CANCELLED)  # releases the seats for other clients
        except BookingUnavailable as exc:
            messages.error(request, str(exc))
        else:
            notifications.booking_cancelled_by_customer(booking)
            messages.success(request, f'Booking {booking.reference} has been cancelled.')
    return redirect('booking_detail', reference=booking.reference)


@login_required
@require_POST
def review_create(request, slug):
    package = get_object_or_404(TourPackage, slug=slug, is_active=True)
    eligible = package.bookings.filter(
        user=request.user, status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED]).exists()
    if not eligible:
        messages.error(request, 'You can review a tour after a confirmed booking for it.')
        return redirect('package_detail', slug=slug)
    if package.reviews.filter(user=request.user).exists():
        messages.info(request, 'You have already reviewed this tour.')
        return redirect('package_detail', slug=slug)
    form = ReviewForm(request.POST)
    if form.is_valid():
        review = form.save(commit=False)
        review.package = package
        review.user = request.user
        review.save()
        messages.success(request, 'Thank you for your review!')
    else:
        messages.error(request, 'Please choose a rating and write a short comment.')
    return redirect('package_detail', slug=slug)


# ---------- staff dashboard ----------

@staff_required
def admin_dashboard(request):
    expire_stale_holds()
    bookings = Booking.objects.select_related('package', 'user')
    context = {
        'booking_count': bookings.count(),
        'pending_bookings': bookings.filter(status=Booking.Status.PENDING).count(),
        'recorded_total': Payment.objects.aggregate(s=Sum('amount'))['s'] or 0,
        'payment_counts': {value: bookings.filter(payment_status=value).count()
                           for value in Booking.PaymentStatus.values},
        'payment_status_choices': Booking.PaymentStatus.choices,
        'payment_followups': bookings.filter(payment_status__in=[Booking.PaymentStatus.UNPAID,
                                                                 Booking.PaymentStatus.PARTIAL])
                                     .exclude(status=Booking.Status.CANCELLED)[:15],
        'open_inquiries': Inquiry.objects.filter(is_resolved=False).select_related('package')[:20],
        'recent_bookings': bookings[:25],
        'package_count': TourPackage.objects.count(),
        'review_count': Review.objects.count(),
        'status_form': BookingStatusForm(),
        'confirmed_count': bookings.filter(status=Booking.Status.CONFIRMED).count(),
        'client_count': get_user_model().objects.filter(is_staff=False).count(),
        'admin_count': get_user_model().objects.filter(is_staff=True).count(),
        'destination_count': Destination.objects.count(),
        'open_inquiry_count': Inquiry.objects.filter(is_resolved=False).count(),
        'recent_activity': ActivityLog.objects.select_related('actor')[:8],
    }
    context.update(booking_statistics())
    return render(request, 'admin_dashboard.html', context)


def _back(request, default):
    """Return to the page the admin came from, if it is on this site."""
    from django.utils.http import url_has_allowed_host_and_scheme
    target = request.POST.get('next', '')
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                 require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


@staff_required
@require_POST
def admin_booking_status(request, pk):
    booking = get_object_or_404(Booking.objects.select_related('package', 'schedule'), pk=pk)
    new_status = request.POST.get('status')
    if new_status not in Booking.Status.values:
        messages.error(request, 'Invalid booking status.')
        return _back(request, reverse('admin_dashboard'))
    old = booking.status
    try:
        booking = set_booking_status(booking, new_status)  # keeps the tour's seat count correct
    except BookingUnavailable as exc:
        messages.error(request, str(exc))
    else:
        if booking.status != old:
            notifications.booking_status_changed(booking, actor=request.user)
        messages.success(request, f'Booking {booking.reference} is now {booking.get_status_display().lower()}.')
    return _back(request, reverse('admin_dashboard'))


@staff_required
@require_POST
def admin_booking_payment_status(request, pk):
    """Staff set a booking's payment status by hand: Unpaid, Partially paid, Paid or Payment verified."""
    booking = get_object_or_404(Booking.objects.select_related('package', 'user'), pk=pk)
    form = PaymentStatusForm(request.POST, instance=booking)
    if form.is_valid():
        old = Booking.objects.get(pk=pk).payment_status
        booking = form.save()
        if booking.payment_status != old:
            notifications.payment_status_changed(booking, actor=request.user)
        messages.success(request, f'Payment status for {booking.reference}: {booking.get_payment_status_display()}.')
    else:
        messages.error(request, 'Choose a valid payment status.')
    return _back(request, reverse('admin_dashboard'))


@staff_required
@require_POST
def admin_inquiry_resolve(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk)
    inquiry.is_resolved = True
    inquiry.save(update_fields=['is_resolved'])
    notifications.log_activity(request.user, 'inquiry.resolved', f'Enquiry from {inquiry.name} resolved')
    messages.success(request, f'Inquiry from {inquiry.name} marked as resolved.')
    return _back(request, reverse('admin_inquiries'))
