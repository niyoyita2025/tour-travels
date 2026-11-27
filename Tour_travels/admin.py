from django.contrib import admin, messages
from django.db import transaction

from . import scheduling
from .destination_tours import auto_tours_blocking, ensure_destination_tour, remove_auto_tours
from .forms import TourScheduleForm

from .models import (
    FAQ, AboutSection, ActivityLog, BackgroundImage, Booking, Category, CoHost, ContactDetail, CultureItem,
    Destination, DetailBook, DetailLink, DetailPhoto, DetailSection, DetailsPage, Inquiry, Notification,
    Payment, Review, ScheduleRule, SiteSettings, SocialLink, TourPackage, TourSchedule,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    list_display = ('name', 'location', 'is_featured', 'is_active')
    list_editable = ('is_featured', 'is_active')
    list_filter = ('is_featured', 'is_active')
    search_fields = ('name', 'location', 'description')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            ensure_destination_tour(obj)  # every new destination gets its own bookable tour

    # The same rules as the Control Panel: a destination with a hand-made or booked tour is never deleted.
    def has_delete_permission(self, request, obj=None):
        if obj is not None and auto_tours_blocking(obj):
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        with transaction.atomic():
            remove_auto_tours(obj)
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for destination in queryset:
            if auto_tours_blocking(destination):
                self.message_user(request, f'"{destination}" is visited by a tour that must stay, so it was not deleted.',
                                  messages.ERROR)
                continue
            self.delete_model(request, destination)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ('amount', 'method', 'transaction_reference', 'note', 'recorded_by')
    readonly_fields = ('recorded_by',)


@admin.register(TourPackage)
class TourPackageAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'places', 'price', 'duration_days', 'is_featured', 'is_active')

    @admin.display(description='Destinations')
    def places(self, obj):
        return ', '.join(d.name for d in obj.destinations.all()) or '—'
    list_editable = ('is_featured', 'is_active')
    list_filter = ('category', 'is_featured', 'is_active')
    search_fields = ('title', 'summary', 'description')
    prepopulated_fields = {'slug': ('title',)}


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('reference', 'full_name', 'package', 'travel_date', 'guests', 'total_price', 'status',
                    'payment_status', 'created_at')
    list_editable = ('payment_status',)
    list_filter = ('status', 'payment_status', 'travel_date')
    search_fields = ('reference', 'full_name', 'email', 'package__title')
    readonly_fields = ('reference', 'total_price', 'created_at', 'updated_at')
    inlines = [PaymentInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('booking', 'amount', 'method', 'transaction_reference', 'recorded_by', 'created_at')
    list_filter = ('method',)
    search_fields = ('booking__reference', 'transaction_reference')


@admin.register(BackgroundImage)
class BackgroundImageAdmin(admin.ModelAdmin):
    list_display = ('section', 'image', 'updated_at')


@admin.register(DetailsPage)
class DetailsPageAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(reverse('admin:Tour_travels_detailspage_change', args=[DetailsPage.load().pk]))


@admin.register(SocialLink)
class SocialLinkAdmin(admin.ModelAdmin):
    list_display = ('platform', 'label', 'url', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(ContactDetail)
class ContactDetailAdmin(admin.ModelAdmin):
    list_display = ('kind', 'label', 'value', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(AboutSection)
class AboutSectionAdmin(admin.ModelAdmin):
    list_display = ('heading', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(DetailPhoto)
class DetailPhotoAdmin(admin.ModelAdmin):
    list_display = ('caption', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(DetailBook)
class DetailBookAdmin(admin.ModelAdmin):
    list_display = ('title', 'author', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(DetailLink)
class DetailLinkAdmin(admin.ModelAdmin):
    list_display = ('title', 'url', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(DetailSection)
class DetailSectionAdmin(admin.ModelAdmin):
    list_display = ('heading', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(CoHost)
class CoHostAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'is_active')
    list_editable = ('order', 'is_active')
    search_fields = ('name',)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('package', 'user', 'rating', 'is_published', 'created_at')
    list_filter = ('rating', 'is_published')
    list_editable = ('is_published',)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = SiteSettings.load()
        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(reverse('admin:Tour_travels_sitesettings_change', args=[obj.pk]))


@admin.register(CultureItem)
class CultureItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'icon', 'order', 'is_active')
    list_editable = ('order', 'is_active')


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'package', 'is_resolved', 'created_at')
    list_filter = ('is_resolved',)
    list_editable = ('is_resolved',)
    search_fields = ('name', 'email', 'message')


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action', 'description', 'actor')
    list_filter = ('action',)
    search_fields = ('description', 'action')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'recipient', 'is_read', 'created_at')
    list_filter = ('is_read',)


@admin.register(TourSchedule)
class TourScheduleAdmin(admin.ModelAdmin):
    form = TourScheduleForm  # same capacity checks as the staff dashboard
    list_display = ('package', 'date', 'start_time', 'capacity', 'seats_booked', 'is_open')
    list_filter = ('is_open', 'package', 'date')
    list_editable = ('is_open',)
    readonly_fields = ('seats_booked',)
    search_fields = ('package__title',)


@admin.register(ScheduleRule)
class ScheduleRuleAdmin(admin.ModelAdmin):
    """Tour timetables. Saving here opens the start times at once, as the Automatic timetables page does."""
    list_display = ('package', 'default_start', 'last_start', 'interval_hours', 'tour_hours', 'daily_capacity')
    fields = ('package', 'default_start', 'last_start', 'interval_hours', 'tour_hours', 'operating_days',
              'daily_capacity', 'days_ahead')
    search_fields = ('package__title',)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            scheduling.sync_rule(obj.package)


@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ('question', 'order', 'is_active')
    list_editable = ('order', 'is_active')
