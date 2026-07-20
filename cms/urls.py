"""URL configuration for the Collection Management System."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView
from django.views.i18n import JavaScriptCatalog


def robots_txt(_request):
    """Keep every crawler out: the CMS only holds private collection data."""
    return HttpResponse('User-agent: *\nDisallow: /\n', content_type='text/plain')

urlpatterns = [
    path('admin/', admin.site.urls),
    # Language switcher endpoint (django.views.i18n.set_language).
    path('i18n/', include('django.conf.urls.i18n')),
    # Translation catalogue for the static JS files (gettext()/interpolate()).
    # Under /i18n/ so it stays reachable in maintenance mode (see middleware).
    path('i18n/js/', JavaScriptCatalog.as_view(), name='javascript-catalog'),
    # PWA: manifest + service worker must be served from the root scope.
    path('manifest.webmanifest',
         TemplateView.as_view(template_name='manifest.webmanifest',
                              content_type='application/manifest+json'),
         name='webmanifest'),
    path('sw.js',
         TemplateView.as_view(template_name='sw.js',
                              content_type='application/javascript'),
         name='service_worker'),
    path('robots.txt', robots_txt, name='robots_txt'),
    # Legal pages (public — required to be reachable without an account).
    path('impressum/', TemplateView.as_view(template_name='pages/imprint.html'),
         name='imprint'),
    path('datenschutz/', TemplateView.as_view(template_name='pages/privacy.html'),
         name='privacy'),
    # Browsers request /favicon.ico regardless of <link rel="icon">.
    path('favicon.ico', RedirectView.as_view(
        url=settings.STATIC_URL + 'icons/icon-192.png', permanent=True)),
    path('accounts/', include('accounts.urls')),
    path('', include('Collection_Management_System.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
