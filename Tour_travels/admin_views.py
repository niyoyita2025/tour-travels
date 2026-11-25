"""
Administrator pages beyond the overview: bookings, payments, customers,
administrator accounts, enquiries, activity log and broadcast notifications.

Every view here is protected by @staff_required (administrators only).
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import notifications
from .bookings import BookingUnavailable, update_booking
from .forms import (
    AdminAccountForm, AdminBookingForm, AdminCustomerForm, BroadcastForm, BookingStatusForm, PaymentForm,
    PaymentStatusForm, ProfileForm,
)
from .models import ActivityLog, Booking, Inquiry, Payment, Review, UserProfile
from .views import _back, staff_required

User = get_user_model()


def _paginate(request, queryset, per_page=20):
    return Paginator(queryset, per_page).get_page(request.GET.get('page'))


# ---------- bookings ----------

@staff_required
def admin_bookings(request):
    bookings = Booking.objects.select_related('package', 'user', 'schedule')
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    if q:
        bookings = bookings.filter(Q(reference__icontains=q) | Q(full_name__icontains=q)
                                   | Q(email__icontains=q) | Q(package__title__icontains=q))
    if status in Booking.Status.values:
        bookings = bookings.filter(status=status)
    return render(request, 'admin/bookings.html', {
        'page': _paginate(request, bookings),
        'q': q,
        'status': status,
        'status_choices': Booking.Status.choices,
    })


# ---------- payments ----------

@staff_required
def admin_payments(request):
    """Every booking with its manual payment status, balance and recorded amounts."""
    bookings = Booking.objects.select_related('package', 'user').exclude(status=Booking.Status.CANCELLED)
    status = request.GET.get('status', '')
    if status in Booking.PaymentStatus.values:
        bookings = bookings.filter(payment_status=status)
    q = request.GET.get('q', '').strip()
    if q:
        bookings = bookings.filter(Q(reference__icontains=q) | Q(full_name__icontains=q))
    return render(request, 'admin/payments.html', {
        'page': _paginate(request, bookings),
        'status': status,
        'q': q,
        'status_choices': Booking.PaymentStatus.choices,
        'payment_status_form': PaymentStatusForm(),
        'recorded_total': Payment.objects.aggregate(s=Sum('amount'))['s'] or 0,
        'status_counts': [(value, label, Booking.objects.filter(payment_status=value).count())
                          for value, label in Booking.PaymentStatus.choices],
    })


@staff_required
@require_POST
def admin_record_payment(request, pk):
    """Record a payment received directly (cash or at the office). It is confirmed immediately."""
    booking = get_object_or_404(Booking.objects.select_related('package', 'user'), pk=pk)
    form = PaymentForm(request.POST, booking=booking)
    if form.is_valid():
        payment = form.save(commit=False)
        payment.booking = booking
        payment.recorded_by = request.user
        payment.save()
        notifications.payment_recorded(payment, actor=request.user)
        messages.success(request, f'Recorded USD {payment.amount} for {booking.reference}.')
    else:
        messages.error(request, ' '.join(e for errs in form.errors.values() for e in errs))
    return redirect('booking_detail', reference=booking.reference)


# ---------- customers ----------

@staff_required
def admin_customers(request):
    users = User.objects.annotate(booking_count=Count('bookings')).order_by('-date_joined')
    q = request.GET.get('q', '').strip()
    role = request.GET.get('role', '')
    if q:
        users = users.filter(Q(username__icontains=q) | Q(email__icontains=q)
                             | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    if role == 'admin':
        users = users.filter(is_staff=True)
    elif role == 'client':
        users = users.filter(is_staff=False)
    return render(request, 'admin/customers.html', {
        'page': _paginate(request, users),
        'q': q,
        'role': role,
    })


@staff_required
def admin_customer_detail(request, pk):
    customer = get_object_or_404(User, pk=pk)
    profile, _ = UserProfile.objects.get_or_create(user=customer)
    if request.method == 'POST':
        form = AdminCustomerForm(request.POST, instance=customer)
        profile_form = ProfileForm(request.POST, instance=profile)
        if form.is_valid() and profile_form.is_valid():
            if customer == request.user and not form.cleaned_data['is_active']:
                form.add_error('is_active', 'You cannot deactivate your own account.')
            else:
                form.save()
                profile_form.save()
                notifications.log_activity(request.user, 'customer.updated', f'Updated account {customer.username}')
                messages.success(request, f'Saved changes to {customer.username}.')
                return redirect('admin_customer_detail', pk=customer.pk)
    else:
        form = AdminCustomerForm(instance=customer)
        profile_form = ProfileForm(instance=profile)
    return render(request, 'admin/customer_detail.html', {
        'customer': customer,
        'form': form,
        'profile_form': profile_form,
        'bookings': customer.bookings.select_related('package')[:50],
        'review_count': Review.objects.filter(user=customer).count(),
    })


@staff_required
@require_POST
def admin_customer_role(request, pk):
    """Grant or remove administrator access. Only an administrator can do this."""
    customer = get_object_or_404(User, pk=pk)
    action = request.POST.get('action')
    if customer == request.user:
        messages.error(request, 'You cannot change your own administrator access.')
    elif action == 'grant':
        customer.is_staff = True
        customer.save(update_fields=['is_staff'])
        notifications.log_activity(request.user, 'admin.granted', f'Administrator access granted to {customer.username}')
        notifications.notify_user(customer, 'Administrator access granted',
                                  'You can now sign in at the administrator sign-in page.', reverse('admin_dashboard'))
        messages.success(request, f'{customer.username} is now an administrator.')
    elif action == 'revoke':
        customer.is_staff = False
        customer.save(update_fields=['is_staff'])
        notifications.log_activity(request.user, 'admin.revoked', f'Administrator access removed from {customer.username}')
        messages.success(request, f'{customer.username} is now a client account.')
    else:
        messages.error(request, 'Unknown role action.')
    return redirect('admin_customer_detail', pk=customer.pk)


@staff_required
@require_POST
def admin_customer_reset(request, pk):
    """Email the customer a password reset link on the administrator's behalf."""
    customer = get_object_or_404(User, pk=pk)
    form = PasswordResetForm({'email': customer.email})
    if not customer.email:
        messages.error(request, 'This account has no email address.')
    elif form.is_valid():
        form.save(
            request=request,
            use_https=request.is_secure(),
            from_email=settings.DEFAULT_FROM_EMAIL,
            email_template_name='accounts/password_reset_email.txt',
            subject_template_name='accounts/password_reset_subject.txt',
        )
        notifications.log_activity(request.user, 'customer.password_reset', f'Reset link sent to {customer.username}')
        messages.success(request, f'A password reset link was emailed to {customer.email}.')
    else:
        messages.error(request, 'The reset link could not be sent. Check the account email address.')
    return redirect('admin_customer_detail', pk=customer.pk)


@staff_required
def admin_create_admin(request):
    if request.method == 'POST':
        form = AdminAccountForm(request.POST)
        if form.is_valid():
            admin = form.save(commit=False)
            admin.is_staff = True
            admin.is_superuser = False
            admin.save()
            notifications.log_activity(request.user, 'admin.created', f'Administrator account created: {admin.username}')
            messages.success(request, f'Administrator account {admin.username} created.')
            return redirect('admin_customers', )
    else:
        form = AdminAccountForm()
    return render(request, 'admin/admin_create.html', {'form': form})


# ---------- enquiries ----------

@staff_required
def admin_inquiries(request):
    inquiries = Inquiry.objects.select_related('package')
    state = request.GET.get('state', 'open')
    if state == 'open':
        inquiries = inquiries.filter(is_resolved=False)
    elif state == 'resolved':
        inquiries = inquiries.filter(is_resolved=True)
    return render(request, 'admin/inquiries.html', {
        'page': _paginate(request, inquiries),
        'state': state,
    })


@staff_required
@require_POST
def admin_inquiry_reopen(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk)
    inquiry.is_resolved = False
    inquiry.save(update_fields=['is_resolved'])
    messages.success(request, f'Enquiry from {inquiry.name} reopened.')
    return _back(request, reverse('admin_inquiries'))


# ---------- activity ----------

@staff_required
def admin_activity(request):
    logs = ActivityLog.objects.select_related('actor')
    action = request.GET.get('action', '').strip()
    if action:
        logs = logs.filter(action__startswith=action)
    return render(request, 'admin/activity.html', {
        'page': _paginate(request, logs, per_page=30),
        'action': action,
    })


# ---------- broadcast notifications ----------

@staff_required
def admin_broadcast(request):
    if request.method != 'POST':
        return redirect('notifications')  # the broadcast form lives on the notifications page
    form = BroadcastForm(request.POST)
    if form.is_valid():
        from .models import Notification
        clients = User.objects.filter(is_staff=False, is_active=True)
        Notification.objects.bulk_create([
            Notification(recipient=client, title=form.cleaned_data['title'][:160],
                         message=form.cleaned_data['message'])
            for client in clients
        ])
        notifications.log_activity(request.user, 'notification.broadcast',
                                   f'Sent "{form.cleaned_data["title"]}" to {clients.count()} client(s)')
        messages.success(request, f'Notification sent to {clients.count()} client(s).')
    else:
        messages.error(request, 'Please give the notification a title and a message.')
    return redirect('notifications')


# ---------- booking changes (administrators) ----------

@staff_required
def admin_booking_edit(request, pk):
    """Change a booking's slot, guests, duration or the client's details. Seats and time are re-checked."""
    booking = get_object_or_404(Booking.objects.select_related('package', 'schedule', 'user'), pk=pk)
    if request.method == 'POST':
        form = AdminBookingForm(request.POST, package=booking.package)
        if form.is_valid():
            data = form.cleaned_data
            try:
                update_booking(
                    booking, schedule=data['schedule'], guests=data['guests'],
                    duration_hours=data['duration_hours'], full_name=data['full_name'], email=data['email'],
                    phone=data['phone'], country=data['country'], special_requests=data['special_requests'],
                    destination=data.get('destination'),
                )
            except BookingUnavailable as exc:
                form.add_error(None, str(exc))
            else:
                notifications.booking_updated(booking, actor=request.user)
                messages.success(request, f'Booking {booking.reference} updated. The client has been notified.')
                return redirect('booking_detail', reference=booking.reference)
    else:
        form = AdminBookingForm(package=booking.package, initial={
            'schedule': booking.schedule_id, 'guests': booking.guests,
            'duration_hours': booking.duration_hours, 'full_name': booking.full_name,
            'email': booking.email, 'phone': booking.phone, 'country': booking.country,
            'special_requests': booking.special_requests,
            'destination': booking.destination_id,
        })
    return render(request, 'admin/booking_edit.html', {'booking': booking, 'form': form})
