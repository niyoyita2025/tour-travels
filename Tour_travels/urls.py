from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import (
    accounts_views, admin_schedule_views, admin_views, booking_views, client_views, content_views, manage_views,
    profile_views, views,
)

urlpatterns = [
    # ----- Public pages -----
    path('', views.home, name='home'),
    path('about/', views.about, name='about'),
    path('details/', manage_views.details_page, name='details'),
    path('faq/', views.faq, name='faq'),
    path('contact/', views.contact, name='contact'),
    path('destinations/', views.destination_list, name='destinations'),
    path('destinations/<slug:slug>/', views.destination_detail, name='destination_detail'),
    path('tours/', views.package_list, name='packages'),
    path('tours/<slug:slug>/', views.package_detail, name='package_detail'),
    path('tours/<slug:slug>/calendar/', booking_views.tour_calendar, name='tour_calendar'),
    path('tours/<slug:slug>/availability/', booking_views.tour_availability, name='tour_availability'),
    path('tours/<slug:slug>/book/', booking_views.book_package, name='book_package'),
    path('tours/<slug:slug>/review/', views.review_create, name='review_create'),
    path('booking/start/', booking_views.booking_start, name='booking_start'),
    path('booking/resume/', booking_views.booking_resume, name='booking_resume'),

    # ----- Sign-up with email verification, one sign-in for both roles, and account security -----
    path('register/', accounts_views.register, name='register'),
    path('verify-email/', accounts_views.verify_email, name='verify_email'),
    path('verify-email/resend/', accounts_views.resend_verification, name='verify_email_resend'),
    path('login/', accounts_views.RoleLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('profile/', profile_views.profile, name='profile'),
    path('profile/email/', profile_views.request_email_change, name='profile_email'),
    path('profile/email/verify/', profile_views.verify_email_change, name='profile_email_verify'),
    path('profile/email/resend/', profile_views.resend_email_change, name='profile_email_resend'),
    path('password-change/', auth_views.PasswordChangeView.as_view(
        template_name='accounts/password_change_form.html',
        success_url=reverse_lazy('password_change_done')), name='password_change'),
    path('password-change/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='accounts/password_change_done.html'), name='password_change_done'),

    # Forgot password: a six-digit code is emailed, then a new password is chosen.
    path('password-reset/', accounts_views.forgot_password, name='password_reset'),
    path('password-reset/have-code/', accounts_views.reset_have_code, name='password_reset_have_code'),
    path('password-reset/verify/', accounts_views.reset_verify, name='password_reset_verify'),
    path('password-reset/resend/', accounts_views.reset_resend, name='password_reset_resend'),
    path('password-reset/new/', accounts_views.reset_new_password, name='password_reset_new'),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html'), name='password_reset_complete'),
    # Reset link emailed by an administrator from the Control Panel.
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='accounts/password_reset_confirm.html',
        success_url=reverse_lazy('password_reset_complete')), name='password_reset_confirm'),

    # ----- Client area (administrators are redirected to the Control Panel) -----
    path('dashboard/', views.dashboard, name='dashboard'),
    path('bookings/', views.booking_history, name='booking_history'),
    path('bookings/<str:reference>/', views.booking_detail, name='booking_detail'),
    path('bookings/<str:reference>/cancel/', views.cancel_booking, name='cancel_booking'),
    path('payments/', client_views.my_payments, name='my_payments'),
    path('reviews/', client_views.my_reviews, name='my_reviews'),
    path('notifications/', client_views.notifications_list, name='notifications'),
    path('notifications/read-all/', client_views.notifications_read_all, name='notifications_read_all'),
    path('notifications/<int:pk>/read/', client_views.notification_read, name='notification_read'),

    # ----- Control Panel (administrators only) -----
    path('manage/', views.admin_dashboard, name='admin_dashboard'),
    path('manage/bookings/', admin_views.admin_bookings, name='admin_bookings'),
    path('manage/bookings/<int:pk>/edit/', admin_views.admin_booking_edit, name='admin_booking_edit'),
    path('manage/bookings/<int:pk>/status/', views.admin_booking_status, name='admin_booking_status'),
    path('manage/bookings/<int:pk>/payments/', admin_views.admin_record_payment, name='admin_record_payment'),
    path('manage/bookings/<int:pk>/payment-status/', views.admin_booking_payment_status,
         name='admin_booking_payment_status'),
    path('manage/payments/', admin_views.admin_payments, name='admin_payments'),
    path('manage/customers/', admin_views.admin_customers, name='admin_customers'),
    path('manage/customers/<int:pk>/', admin_views.admin_customer_detail, name='admin_customer_detail'),
    path('manage/customers/<int:pk>/role/', admin_views.admin_customer_role, name='admin_customer_role'),
    path('manage/customers/<int:pk>/reset/', admin_views.admin_customer_reset, name='admin_customer_reset'),
    path('manage/admins/new/', admin_views.admin_create_admin, name='admin_create'),
    path('manage/inquiries/', admin_views.admin_inquiries, name='admin_inquiries'),
    path('manage/inquiries/<int:pk>/resolve/', views.admin_inquiry_resolve, name='admin_inquiry_resolve'),
    path('manage/inquiries/<int:pk>/reopen/', admin_views.admin_inquiry_reopen, name='admin_inquiry_reopen'),
    path('manage/activity/', admin_views.admin_activity, name='admin_activity'),
    path('manage/broadcast/', admin_views.admin_broadcast, name='admin_broadcast'),
    path('manage/calendar/', admin_schedule_views.admin_calendar, name='admin_calendar'),
    path('manage/schedules/', admin_schedule_views.admin_schedules, name='admin_schedules'),
    path('manage/schedules/new/', admin_schedule_views.admin_schedule_form, name='admin_schedule_new'),
    path('manage/schedules/bulk/', admin_schedule_views.admin_schedule_bulk, name='admin_schedule_bulk'),
    path('manage/schedules/<int:pk>/edit/', admin_schedule_views.admin_schedule_form, name='admin_schedule_edit'),
    path('manage/schedules/<int:pk>/delete/', admin_schedule_views.admin_schedule_delete,
         name='admin_schedule_delete'),
    path('manage/scheduling/', admin_schedule_views.admin_scheduling, name='admin_scheduling'),
    path('manage/scheduling/<int:package_id>/', admin_schedule_views.admin_scheduling_edit,
         name='admin_scheduling_edit'),
    path('manage/scheduling/<int:package_id>/off/', admin_schedule_views.admin_scheduling_off,
         name='admin_scheduling_off'),

    # Control Panel: each management area has its own page and form
    path('manage/backgrounds/', manage_views.background_manage, name='background_manage'),
    path('manage/backgrounds/<str:section>/save/', manage_views.background_save, name='background_save'),
    path('manage/contact/', manage_views.contact_manage, name='contact_manage'),
    path('manage/about/', manage_views.about_manage, name='about_manage'),
    path('manage/details/', manage_views.details_manage, name='details_manage'),
    path('manage/site-text/', content_views.site_settings_edit, name='site_settings'),

    # Website content lists (social media, FAQs, About sections, Details lists, co-hosts, ...).
    # The fixed routes above come first, so the generic routes below never shadow them.
    path('manage/content/', content_views.content_index, name='content_index'),
    path('manage/content/<str:section>/', content_views.content_list, name='content_list'),
    path('manage/content/<str:section>/new/', content_views.content_form, name='content_new'),
    path('manage/content/<str:section>/<int:pk>/edit/', content_views.content_form, name='content_edit'),
    path('manage/content/<str:section>/<int:pk>/delete/', content_views.content_delete, name='content_delete'),
]
