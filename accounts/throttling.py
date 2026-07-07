"""Cache-based brute-force throttling for the auth endpoints.

Counters live in Django's cache with a TTL, so there is no extra table and no
cleanup job. With the default LocMemCache the limits apply per process — good
enough for small deployments; point CACHES at Redis/Memcached in production if
you run several workers and want strict global limits.

Failed logins are counted twice: per (IP, username) — locks a targeted account
quickly — and per IP alone with a higher ceiling, so rotating usernames from
one address doesn't help. Successful logins clear both counters.
"""

import logging
from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.translation import gettext as _

security_log = logging.getLogger('cms.security')

# Failed logins: 5 tries per account, 20 per IP, then 15 minutes lockout.
LOGIN_MAX_PER_USER = 5
LOGIN_MAX_PER_IP = 20
LOGIN_LOCKOUT_SECONDS = 15 * 60


def client_ip(request) -> str:
    """The peer address. Behind a reverse proxy REMOTE_ADDR is the proxy —
    configure the proxy to pass the real client IP (and see SECURITY.md);
    X-Forwarded-For is deliberately not trusted here (trivially spoofable)."""
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _count(key: str, window: int) -> int:
    """Increment ``key`` and return the new count, starting a TTL window."""
    added = cache.add(key, 1, timeout=window)
    if added:
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout=window)
        return 1


def login_blocked(request, username: str) -> bool:
    ip = client_ip(request)
    user_key = f'bf:login:{ip}:{(username or "").lower()}'
    ip_key = f'bf:login:{ip}'
    return (
        (cache.get(user_key) or 0) >= LOGIN_MAX_PER_USER
        or (cache.get(ip_key) or 0) >= LOGIN_MAX_PER_IP
    )


def login_failed(request, username: str) -> None:
    ip = client_ip(request)
    per_user = _count(f'bf:login:{ip}:{(username or "").lower()}', LOGIN_LOCKOUT_SECONDS)
    _count(f'bf:login:{ip}', LOGIN_LOCKOUT_SECONDS)
    if per_user == LOGIN_MAX_PER_USER:
        security_log.warning(
            'Login locked out after %d failures: user=%r ip=%s',
            per_user, username, ip,
        )


def login_succeeded(request, username: str) -> None:
    ip = client_ip(request)
    cache.delete(f'bf:login:{ip}:{(username or "").lower()}')
    cache.delete(f'bf:login:{ip}')


def ratelimit_post(scope: str, max_requests: int, window_seconds: int):
    """Limit POSTs per client IP (registration, password reset, …).

    GETs stay unlimited — the page itself may always render; only the
    state-changing/mail-sending submission is throttled.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method == 'POST':
                ip = client_ip(request)
                if _count(f'bf:{scope}:{ip}', window_seconds) > max_requests:
                    security_log.warning('Rate limit hit: scope=%s ip=%s', scope, ip)
                    return HttpResponse(
                        _('Zu viele Anfragen. Bitte warte einige Minuten und '
                          'versuche es dann erneut.'),
                        status=429, content_type='text/plain; charset=utf-8',
                    )
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def allow(scope: str, ident: str, max_requests: int, window_seconds: int) -> bool:
    """Generic counter for signed-in abuse protection (e.g. external lookups).

    Returns False once ``ident`` exceeded ``max_requests`` in the window."""
    if _count(f'bf:{scope}:{ident}', window_seconds) > max_requests:
        security_log.info('Rate limit hit: scope=%s ident=%s', scope, ident)
        return False
    return True
