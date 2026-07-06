"""URL configuration for the Collection Management System."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Language switcher endpoint (django.views.i18n.set_language).
    path('i18n/', include('django.conf.urls.i18n')),
    # PWA: manifest + service worker must be served from the root scope.
    path('manifest.webmanifest',
         TemplateView.as_view(template_name='manifest.webmanifest',
                              content_type='application/manifest+json'),
         name='webmanifest'),
    path('sw.js',
         TemplateView.as_view(template_name='sw.js',
                              content_type='application/javascript'),
         name='service_worker'),
    path('accounts/', include('accounts.urls')),
    path('', include('Collection_Management_System.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
