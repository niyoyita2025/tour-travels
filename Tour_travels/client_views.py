"""
Client pages: payments overview, reviews and notifications.
Also used by administrators for their own notification inbox.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import BroadcastForm
from .models import Booking, Notification, Review
from .roles import is_admin_user
from .views import _back


@login_required
def my_payments(request):
    bookings = request.user.bookings.select_related('package').prefetch_related('payments')
    return render(request, 'payments.html', {'bookings': bookings})


@login_required
def my_reviews(request):
    reviews = Review.objects.filter(user=request.user).select_related('package')
    reviewed_ids = set(reviews.values_list('package_id', flat=True))
    eligible = (request.user.bookings
                .filter(status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED])
                .exclude(package_id__in=reviewed_ids)
                .select_related('package')
                .distinct())
    return render(request, 'my_reviews.html', {'reviews': reviews, 'eligible': eligible})


@login_required
def notifications_list(request):
    items = request.user.notifications.all()
    context = {'page': _paginate(request, items), 'unread': items.filter(is_read=False).count()}
    if is_admin_user(request.user):
        context['broadcast_form'] = BroadcastForm()
    return render(request, 'notifications.html', context)


def _paginate(request, queryset):
    from django.core.paginator import Paginator
    return Paginator(queryset, 20).get_page(request.GET.get('page'))


@login_required
@require_POST
def notification_read(request, pk):
    item = get_object_or_404(Notification, pk=pk, recipient=request.user)
    item.is_read = True
    item.save(update_fields=['is_read'])
    if item.link:
        return redirect(item.link)
    return redirect('notifications')


@login_required
@require_POST
def notifications_read_all(request):
    updated = request.user.notifications.filter(is_read=False).update(is_read=True)
    messages.success(request, f'Marked {updated} notification(s) as read.')
    return _back(request, 'notifications')
