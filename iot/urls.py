from django.urls import path

from . import views

# Mounted at /manage/iot/ (administrators only).
urlpatterns = [
    path('', views.monitor, name='iot_monitor'),
    path('live.json', views.live_json, name='iot_live'),
    path('alerts/feed.json', views.alerts_feed, name='iot_alerts_feed'),
    path('devices/', views.devices, name='iot_devices'),
    path('devices/new/', views.device_form, name='iot_device_new'),
    path('devices/<int:pk>/edit/', views.device_form, name='iot_device_edit'),
    path('devices/<int:pk>/token/', views.device_token_regenerate, name='iot_device_token'),
    path('devices/<int:pk>/toggle/', views.device_toggle, name='iot_device_toggle'),
    path('devices/<int:pk>/delete/', views.device_delete, name='iot_device_delete'),
    path('devices/token/', views.device_token_show, name='iot_token_show'),
    path('devices/<int:pk>/thresholds/', views.thresholds, name='iot_thresholds'),
    path('thresholds/<int:pk>/delete/', views.threshold_delete, name='iot_threshold_delete'),
    path('alerts/', views.alerts, name='iot_alerts'),
    path('alerts/<int:pk>/<str:action>/', views.alert_action, name='iot_alert_action'),
    path('history/', views.history, name='iot_history'),
]
