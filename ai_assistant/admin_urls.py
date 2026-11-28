from django.urls import path

from . import views

# Mounted at /manage/ai/ (administrators only).
urlpatterns = [
    path('analytics/', views.analytics_page, name='ai_analytics'),
    path('reports/', views.reports_page, name='ai_reports'),
    path('assistant/', views.assistant_settings, name='ai_assistant_settings'),
]
