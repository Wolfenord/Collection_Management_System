"""Template context processors."""

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
