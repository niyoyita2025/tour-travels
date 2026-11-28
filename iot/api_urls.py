from django.urls import path

from . import api

# Mounted at /api/iot/ (device token authentication, no browser session).
urlpatterns = [
    path('readings/', api.submit_readings, name='iot_readings_api'),
]
