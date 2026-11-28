from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    # IoT sensors: administrator pages, the device endpoint, and the AI assistant and analytics.
    path('manage/iot/', include('iot.urls')),
    path('api/iot/', include('iot.api_urls')),
    path('manage/ai/', include('ai_assistant.admin_urls')),
    path('assistant/', include('ai_assistant.urls')),
    path('', include('Tour_travels.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif settings.SERVE_MEDIA:
    # Uploaded images (logo, favicon, galleries, backgrounds, co-host logos) on a host without separate
    # file storage, such as Render. Django serves them itself; fine for a small site's traffic.
    urlpatterns += [
        re_path(r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'), serve,
                {'document_root': settings.MEDIA_ROOT}),
    ]

handler404 = 'Tour_travels.views.page_not_found'
handler403 = 'Tour_travels.views.permission_denied'
