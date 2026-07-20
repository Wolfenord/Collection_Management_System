"""Template context processors."""

import ipaddress
import socket
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from . import runtime_settings


class _RuntimeSettingsProxy:
    """Lazy, read-only template access to the runtime settings.

    Resolution happens per lookup (``{{ site_config.registration_enabled }}``),
    so templates always see the current database/INI value — nothing is
    evaluated for pages that don't use it.
    """

    def __getitem__(self, key):
        return runtime_settings.get_setting(key)

    def __contains__(self, key) -> bool:
        return key in runtime_settings.REGISTRY


def site_config(request):
    return {'site_config': _RuntimeSettingsProxy()}


def notifications(request):
    """Bell-menu data for base.html: recent notifications, unread count and
    the number of overdue loans in the user's own collections.

    Everything is lazy — nothing is queried for responses that never render
    the navbar (redirects, downloads, API)."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}

    def items():
        return list(user.notifications.all()[:8])

    def unread():
        return user.notifications.filter(read_at__isnull=True).count()

    def overdue():
        from .models import Loan
        today = timezone.localdate()
        cutoff = today - timedelta(days=runtime_settings.get_setting('loan_overdue_days'))
        return Loan.objects.filter(
            item__collection__owner=user, returned_at__isnull=True,
            item__deleted_at__isnull=True,
        ).filter(Q(due_at__lt=today) | Q(due_at__isnull=True, lent_at__lte=cutoff)).count()

    return {
        'notif_items': SimpleLazyObject(items),
        'notif_unread': SimpleLazyObject(unread),
        'notif_overdue': SimpleLazyObject(overdue),
    }


def passkey_hint(request):
    """When the site is opened via a raw IP, offer the working passkey URL.

    WebAuthn refuses IP addresses as relying-party ID, so the passkey buttons
    can never work on ``https://192.168.x.x:…``. In that case templates get
    ``passkey_mdns_url`` — the same page via this machine's mDNS name
    (``<hostname>.local``), which phones and desktops resolve in the LAN.
    On a proper domain the host isn't an IP and this stays empty.
    """
    host = request.get_host()
    hostname, sep, port = host.partition(':')
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return {}  # already a name — passkeys can work here
    if hostname == '127.0.0.1':
        return {}  # localhost is fine via the literal name; no hint needed
    mdns = socket.gethostname().split('.')[0].lower() + '.local'
    return {'passkey_mdns_url': f'https://{mdns}{sep}{port}{request.get_full_path()}'}
