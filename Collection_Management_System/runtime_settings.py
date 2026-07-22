"""Database-backed runtime settings (with INI/code-default fallback).

Deployment settings (database, e-mail, security …) are fixed at process start
and live in the environment / ``config.ini`` (see ``cms/conf.py``). Everything
here, in contrast, may change *while the application is running* — page size,
loan periods, registration policy — so it is resolved per request:

  1. ``SiteSetting`` row in the database (edited by staff on the settings page
     or in the Django admin)
  2. ``config.ini`` / environment variable of the same name
     (section ``[app-defaults]`` in ``config.example.ini``)
  3. The code default in :data:`REGISTRY`

Adding a new runtime setting = adding one :class:`SettingDef` to ``REGISTRY``.
The settings form, admin validation and lookup all derive from the registry,
so no further code is needed.

Reads are cached (60 s, invalidated on every save through this module) — a
``get_setting`` call in a loop or template does not hit the database.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from cms import conf

from .models import SettingChange, SiteSetting

_CACHE_KEY = 'cms.site_settings'
_CACHE_SECONDS = 60


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    help_text: str
    kind: str  # 'int' | 'bool' | 'str'
    default: object
    min_value: int | None = None
    max_value: int | None = None
    max_length: int | None = None
    # For 'str' kind: restrict to a fixed set — ((value, human label), …).
    choices: tuple[tuple[str, object], ...] | None = None
    # Users may override this setting for themselves (User.preferences).
    per_user: bool = False
    # For 'str' kind: render a textarea (multi-line values, e.g. addresses).
    multiline: bool = False

    def coerce(self, raw: object):
        """Validate/convert a raw (DB/INI/form) value; raise ValueError if bad."""
        if self.kind == 'bool':
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {'1', 'true', 'yes', 'on'}
            raise ValueError(f'{self.key}: expected a boolean, got {raw!r}')
        if self.kind == 'int':
            value = int(raw)  # raises ValueError/TypeError for junk
            if self.min_value is not None and value < self.min_value:
                raise ValueError(f'{self.key}: {value} < minimum {self.min_value}')
            if self.max_value is not None and value > self.max_value:
                raise ValueError(f'{self.key}: {value} > maximum {self.max_value}')
            return value
        value = str(raw).strip()
        if self.max_length is not None:
            value = value[:self.max_length]
        if self.choices is not None and value not in {c[0] for c in self.choices}:
            valid = ', '.join(c[0] for c in self.choices)
            raise ValueError(f'{self.key}: {value!r} not one of: {valid}')
        return value


REGISTRY: dict[str, SettingDef] = {
    setting.key: setting
    for setting in [
        SettingDef(
            key='maintenance_mode',
            label=_('Wartungsmodus'),
            help_text=_('Wenn aktiv, sehen normale Benutzer nur eine Wartungsseite. '
                        'Administratoren können weiterarbeiten.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='items_per_page',
            label=_('Gegenstände pro Seite'),
            help_text=_('Seitengröße der Gegenstandstabelle einer Sammlung. '
                        'Benutzer können dies im Profil für sich überschreiben.'),
            kind='int', default=50, min_value=5, max_value=500,
            per_user=True,
        ),
        SettingDef(
            key='loan_overdue_days',
            label=_('Leihfrist ohne Rückgabedatum (Tage)'),
            help_text=_('Nach so vielen Tagen gilt eine Ausleihe ohne vereinbartes '
                        'Rückgabedatum als überfällig.'),
            kind='int', default=30, min_value=1, max_value=3650,
        ),
        SettingDef(
            key='api_enabled',
            label=_('JSON-API aktivieren'),
            help_text=_('Zugriff auf /api/… mit persönlichen API-Tokens '
                        '(im Profil erstellbar). Aus = alle API-Anfragen werden abgelehnt.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='loan_reminders_enabled',
            label=_('Erinnerungen für überfällige Ausleihen'),
            help_text=_('Der Befehl „send_loan_reminders“ (z. B. per Cron) schickt dem '
                        'Besitzer der Sammlung eine E-Mail-Übersicht überfälliger Ausleihen.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='loan_reminder_interval_days',
            label=_('Erinnerungsintervall (Tage)'),
            help_text=_('Frühestens nach so vielen Tagen wird für dieselbe Ausleihe '
                        'erneut erinnert.'),
            kind='int', default=7, min_value=1, max_value=365,
        ),
        SettingDef(
            key='trash_retention_days',
            label=_('Papierkorb-Aufbewahrung (Tage)'),
            help_text=_('Gelöschte Gegenstände bleiben so lange im Papierkorb und werden '
                        'danach endgültig entfernt.'),
            kind='int', default=30, min_value=1, max_value=3650,
        ),
        SettingDef(
            key='registration_enabled',
            label=_('Registrierung erlauben'),
            help_text=_('Wenn deaktiviert, können keine neuen Konten angelegt werden — '
                        'die Registrierungsseite ist geschlossen.'),
            kind='bool', default=True,
        ),
        SettingDef(
            key='registration_auto_approve',
            label=_('Registrierungen automatisch freigeben'),
            help_text=_('Wenn aktiv, können sich neue Konten sofort anmelden — '
                        'ohne Freigabe durch einen Administrator.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='notify_admins_on_registration',
            label=_('Admins bei neuer Registrierung benachrichtigen'),
            help_text=_('Sendet eine E-Mail an alle Administratoren, wenn eine neue '
                        'Registrierung auf Freigabe wartet.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='announcement_text',
            label=_('Ankündigungsbanner'),
            help_text=_('Wird auf jeder Seite oben angezeigt, z. B. für Wartungshinweise. '
                        'Leer = kein Banner.'),
            kind='str', default='', max_length=500,
        ),
        SettingDef(
            key='announcement_style',
            label=_('Stil des Ankündigungsbanners'),
            help_text=_('Farbe des Banners.'),
            kind='str', default='warning',
            choices=(
                ('info', _('Blau (Info)')),
                ('warning', _('Gelb (Warnung)')),
                ('danger', _('Rot (Wichtig)')),
                ('success', _('Grün (Erfolg)')),
            ),
        ),
        SettingDef(
            key='upload_max_mb',
            label=_('Maximale Upload-Größe (MB)'),
            help_text=_('Obergrenze pro hochgeladener Datei (Bilder, Belege, Dokumente).'),
            kind='int', default=20, min_value=1, max_value=500,
        ),
        SettingDef(
            key='upload_allowed_extensions',
            label=_('Erlaubte Dateiendungen'),
            help_text=_('Kommagetrennt, z. B. „pdf, jpg, png“. Leer = alle Endungen erlaubt.'),
            kind='str', default='', max_length=300,
        ),
        SettingDef(
            key='default_currency',
            label=_('Standardwährung'),
            help_text=_('Währungscode (ISO 4217, z. B. EUR) für neue Preisfelder.'),
            kind='str', default='EUR', max_length=3,
        ),
        SettingDef(
            key='lookup_timeout',
            label=_('Timeout externe Datenbanken (Sekunden)'),
            help_text=_('Maximale Wartezeit für Anfragen an Open Library, '
                        'Google Books usw.'),
            kind='int', default=8, min_value=1, max_value=60,
        ),
        SettingDef(
            key='tmdb_api_key',
            label=_('TMDb-API-Schlüssel (Filme & Serien)'),
            help_text=_('Kostenloser API-Schlüssel von themoviedb.org — schaltet die '
                        'Film-/Seriensuche frei. Leer = TMDb wird nicht abgefragt.'),
            kind='str', default='', max_length=64,
        ),
        SettingDef(
            key='rawg_api_key',
            label=_('RAWG-API-Schlüssel (Videospiele)'),
            help_text=_('Kostenloser API-Schlüssel von rawg.io — schaltet die '
                        'Videospielsuche frei. Leer = RAWG wird nicht abgefragt.'),
            kind='str', default='', max_length=64,
        ),
        SettingDef(
            key='live_offers_enabled',
            label=_('Live-Angebote im Preisvergleich'),
            help_text=_('Wenn aktiv, holt der Preisvergleich echte Angebote (Titel, Zustand, '
                        'Preis, Händler) direkt vom Server und zeigt sie inline an – zusätzlich '
                        'zu den Plattform-Links. Nur für Plattformen mit offizieller API und '
                        'hinterlegtem Zugang (aktuell Discogs). Angaben ohne Gewähr.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='discogs_token',
            label=_('Discogs-Token (Musik-Angebote)'),
            help_text=_('Persönlicher Zugriffstoken von discogs.com (Einstellungen → '
                        'Developers). Schaltet Live-Angebote für Musik/Tonträger frei. '
                        'Leer = Discogs wird nicht abgefragt.'),
            kind='str', default='', max_length=128,
        ),
        SettingDef(
            key='book_offers_enabled',
            label=_('Buch-Angebote bündeln (Booklooker, AbeBooks, ZVAB)'),
            help_text=_('Durchsucht mehrere Buch-/Antiquariatsplattformen (Booklooker, '
                        'AbeBooks, ZVAB) gleichzeitig nach ISBN oder Titel – neue & '
                        'gebrauchte Bücher, antiquarische Werke und Handschriften – und '
                        'zeigt die Angebote gebündelt und dedupliziert an (ViaLibri-artig). '
                        'Benötigt keinen Schlüssel, ruft die Plattformen aber live ab – '
                        'bitte deren Nutzungsbedingungen beachten. Nur wirksam, wenn '
                        '„Live-Angebote“ aktiv ist.'),
            kind='bool', default=False,
        ),
        SettingDef(
            key='legal_operator',
            label=_('Impressum: Betreiber'),
            help_text=_('Name des Betreibers/Verantwortlichen für Impressum und '
                        'Datenschutzerklärung.'),
            kind='str', default='', max_length=200,
        ),
        SettingDef(
            key='legal_address',
            label=_('Impressum: Anschrift'),
            help_text=_('Postanschrift des Betreibers (mehrzeilig möglich).'),
            kind='str', default='', max_length=500, multiline=True,
        ),
        SettingDef(
            key='legal_email',
            label=_('Impressum: Kontakt-E-Mail'),
            help_text=_('E-Mail-Adresse für Anfragen zu Impressum und Datenschutz.'),
            kind='str', default='', max_length=200,
        ),
        SettingDef(
            key='global_search_max_items',
            label=_('Maximale Treffer der globalen Suche'),
            help_text=_('Obergrenze der Gegenstands-Treffer auf der Suchseite.'),
            kind='int', default=50, min_value=10, max_value=500,
        ),
    ]
}


def _db_overrides() -> dict[str, object]:
    """All stored overrides as {key: raw value}, cached across requests."""
    values = cache.get(_CACHE_KEY)
    if values is None:
        values = dict(SiteSetting.objects.values_list('key', 'value'))
        cache.set(_CACHE_KEY, values, _CACHE_SECONDS)
    return values


def get_setting(key: str):
    """Resolve one runtime setting: database > config.ini/env > code default."""
    definition = REGISTRY[key]
    raw = _db_overrides().get(key)
    if raw is not None:
        try:
            return definition.coerce(raw)
        except (ValueError, TypeError):
            pass  # corrupt override (e.g. hand-edited in admin): fall through
    ini = conf.get(key.upper(), '')
    if ini != '':
        try:
            return definition.coerce(ini)
        except (ValueError, TypeError):
            pass
    return definition.default


def set_setting(key: str, value, user=None) -> None:
    """Store one override (validated against the registry) and refresh the cache.

    Setting a value equal to the current INI/code fallback still stores it —
    that pins the value against later INI changes, which is what an explicit
    save in the UI should mean. ``user`` is recorded as ``updated_by`` (audit),
    and every *effective* value change lands in the ``SettingChange`` history
    (no-op saves are not logged).
    """
    definition = REGISTRY[key]
    new = definition.coerce(value)
    old = get_setting(key)
    SiteSetting.objects.update_or_create(
        key=key, defaults={'value': new, 'updated_by': user},
    )
    cache.delete(_CACHE_KEY)
    if old != new:
        SettingChange.objects.create(key=key, old_value=old, new_value=new, changed_by=user)


def get_setting_for(user, key: str):
    """Resolve a setting honouring a personal override (``User.preferences``).

    Only settings marked ``per_user`` can be overridden; everything else (and
    anonymous users) falls through to :func:`get_setting`.
    """
    definition = REGISTRY[key]
    if definition.per_user and getattr(user, 'is_authenticated', False):
        raw = (user.preferences or {}).get(key)
        if raw is not None:
            try:
                return definition.coerce(raw)
            except (ValueError, TypeError):
                pass  # corrupt preference: ignore, use the site-wide value
    return get_setting(key)


def all_settings() -> dict[str, object]:
    """Every registered setting with its effective value (for the settings page)."""
    return {key: get_setting(key) for key in REGISTRY}


def allowed_upload_extensions() -> set[str]:
    """Parsed ``upload_allowed_extensions``: lower-case, dots stripped.

    An empty set means "no restriction".
    """
    raw = get_setting('upload_allowed_extensions')
    return {part.strip().lstrip('.').lower() for part in raw.split(',') if part.strip()}
