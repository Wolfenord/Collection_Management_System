"""Project middleware."""

from django.conf import settings
from django.shortcuts import render


class DefaultLanguageMiddleware:
    """Make ``LANGUAGE_CODE`` (German) the default UI language.

    Django's LocaleMiddleware falls back to the browser's Accept-Language
    header, so visitors with an English browser got the whole UI — including
    all template texts — primarily in English. This middleware (placed right
    BEFORE LocaleMiddleware) drops that header unless the user has explicitly
    chosen a language via the switcher (language cookie / session), so the
    site defaults to German while the manual switcher keeps working.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        has_explicit_choice = (
            settings.LANGUAGE_COOKIE_NAME in request.COOKIES
            or (hasattr(request, 'session') and 'django_language' in request.session)
        )
        if not has_explicit_choice:
            request.META.pop('HTTP_ACCEPT_LANGUAGE', None)
        return self.get_response(request)


def _is_exempt(path: str) -> bool:
    """Paths that must stay reachable during maintenance.

    Login/logout/password reset so staff can get in (and users out), the admin,
    the language switcher, PWA root files and static/media assets. The
    registration page is deliberately NOT exempt.
    """
    if path.startswith(('/admin/', '/i18n/', '/static/', '/media/')):
        return True
    if path.startswith('/accounts/') and not path.startswith('/accounts/register/'):
        return True
    return path in ('/manifest.webmanifest', '/sw.js')


class MaintenanceModeMiddleware:
    """Serve a 503 maintenance page to non-staff while ``maintenance_mode`` is on.

    The flag is a runtime setting (database-backed, cached), so staff can flip
    it on the settings page and it takes effect immediately — no restart, no
    redeploy. Staff users keep full access and see a warning banner instead
    (rendered in base.html via ``site_config``).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .runtime_settings import get_setting

        if (
            get_setting('maintenance_mode')
            and not request.user.is_staff
            and not _is_exempt(request.path)
        ):
            return render(request, 'maintenance.html', status=503)
        return self.get_response(request)
