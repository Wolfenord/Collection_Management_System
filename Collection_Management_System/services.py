"""Business logic helpers for collections (kept out of views/models)."""

from __future__ import annotations

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from .models import Collection, FieldDefinition, FieldType, ItemType

# Standard columns every new collection starts with. The mandatory item ID
# (Item.id) and "Art" (Item.item_type) are structural and not listed here.
# All of these are removable later (is_system only marks their origin).
DEFAULT_FIELDS: list[tuple[str, str, str, bool, dict]] = [
    ('name', _('Name / Bezeichnung'), FieldType.TEXT, True, {}),
    ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
    ('kaufdatum', _('Kaufdatum'), FieldType.DATE, False, {}),
    ('preis', _('Preis'), FieldType.PRICE, False, {}),
    ('beleg', _('Beleg'), FieldType.FILE, False, {}),
    ('bild', _('Bild'), FieldType.IMAGE, False, {}),
]


def _price_config() -> dict:
    """Config for a new price field: currency from the runtime settings."""
    from .runtime_settings import get_setting
    return {'currency': get_setting('default_currency')}


def create_default_fields(collection: Collection) -> None:
    """Seed a freshly created collection with the standard removable columns."""
    fields = [
        FieldDefinition(
            collection=collection,
            key=key,
            label=label,
            field_type=field_type,
            required=required,
            order=index,
            is_system=True,
            config=_price_config() if field_type == FieldType.PRICE else config,
        )
        for index, (key, label, field_type, required, config) in enumerate(DEFAULT_FIELDS)
    ]
    FieldDefinition.objects.bulk_create(fields)


# Ready-to-use presets ("Vorlagen") for common collection kinds — not just
# books. Each field: (key, label, field_type, required, config). Every preset
# carries ``lookup_attribute`` mappings (see lookup_providers.ATTRIBUTES) so a
# code scan or a suggestion search fills the form from the matching external
# databases; ``kind`` (lookup_providers.MEDIA_KINDS) selects those databases
# and the fitting price-search platforms.
PRESETS: dict[str, dict] = {
    'books': {
        'label': _('Bücher'),
        'kind': 'books',
        'fields': [
            ('titel', _('Titel'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('autor', _('Autor(en)'), FieldType.TEXT, False, {'lookup_attribute': 'authors'}),
            ('verlag', _('Verlag'), FieldType.TEXT, False, {'lookup_attribute': 'publisher'}),
            ('erscheinungsjahr', _('Erscheinungsjahr'), FieldType.YEAR, False, {'lookup_attribute': 'year'}),
            ('seiten', _('Seitenzahl'), FieldType.NUMBER, False, {'lookup_attribute': 'pages'}),
            ('beschreibung', _('Beschreibung'), FieldType.TEXTAREA, False, {'lookup_attribute': 'description'}),
            ('genre', _('Kategorien / Genre'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('sprache', _('Sprache'), FieldType.TEXT, False, {'lookup_attribute': 'language'}),
            ('isbn', _('ISBN'), FieldType.ISBN, False, {'lookup_attribute': 'isbn'}),  # query field (scannable)
            ('cover_url', _('Cover (URL)'), FieldType.URL, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
        ],
    },
    'movies': {
        'label': _('Filme & Serien'),
        'kind': 'movies',
        'fields': [
            ('titel', _('Titel'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('regie', _('Regie'), FieldType.TEXT, False, {'lookup_attribute': 'director'}),
            ('erscheinungsjahr', _('Erscheinungsjahr'), FieldType.YEAR, False, {'lookup_attribute': 'year'}),
            ('medium', _('Medium'), FieldType.CHOICE, False,
             {'choices': ['DVD', 'Blu-ray', '4K UHD', 'VHS', 'Digital']}),
            ('laufzeit', _('Laufzeit (Minuten)'), FieldType.NUMBER, False, {'lookup_attribute': 'runtime'}),
            ('genre', _('Genre'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('beschreibung', _('Beschreibung'), FieldType.TEXTAREA, False, {'lookup_attribute': 'description'}),
            ('ean', _('Barcode / EAN'), FieldType.BARCODE, False, {'lookup_attribute': 'ean'}),
            ('bild', _('Cover / Bild'), FieldType.IMAGE, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
        ],
    },
    'music': {
        'label': _('Musik / Tonträger'),
        'kind': 'music',
        'fields': [
            ('titel', _('Titel / Album'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('interpret', _('Interpret'), FieldType.TEXT, False, {'lookup_attribute': 'artist'}),
            ('label', _('Label'), FieldType.TEXT, False, {'lookup_attribute': 'publisher'}),
            ('erscheinungsjahr', _('Erscheinungsjahr'), FieldType.YEAR, False, {'lookup_attribute': 'year'}),
            ('medium', _('Medium'), FieldType.CHOICE, False,
             {'choices': ['CD', 'Vinyl', 'Kassette', 'Digital'], 'lookup_attribute': 'format'}),
            ('genre', _('Genre'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('ean', _('Barcode / EAN'), FieldType.BARCODE, False, {'lookup_attribute': 'ean'}),
            ('bild', _('Cover / Bild'), FieldType.IMAGE, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
        ],
    },
    'games': {
        'label': _('Videospiele'),
        'kind': 'games',
        'fields': [
            ('titel', _('Titel'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('plattform', _('Plattform'), FieldType.TEXT, False, {'lookup_attribute': 'platform'}),
            ('erscheinungsjahr', _('Erscheinungsjahr'), FieldType.YEAR, False, {'lookup_attribute': 'year'}),
            ('genre', _('Genre'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('usk', _('Altersfreigabe'), FieldType.CHOICE, False,
             {'choices': ['USK 0', 'USK 6', 'USK 12', 'USK 16', 'USK 18']}),
            ('ean', _('Barcode / EAN'), FieldType.BARCODE, False, {'lookup_attribute': 'ean'}),
            ('bild', _('Cover / Bild'), FieldType.IMAGE, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
        ],
    },
    'boardgames': {
        'label': _('Brett- & Gesellschaftsspiele'),
        'kind': 'boardgames',
        'fields': [
            ('titel', _('Titel'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('verlag', _('Verlag'), FieldType.TEXT, False, {'lookup_attribute': 'publisher'}),
            ('erscheinungsjahr', _('Erscheinungsjahr'), FieldType.YEAR, False, {'lookup_attribute': 'year'}),
            ('spieler', _('Spieleranzahl'), FieldType.TEXT, False, {'lookup_attribute': 'players'}),
            ('spieldauer', _('Spieldauer (Minuten)'), FieldType.NUMBER, False, {'lookup_attribute': 'runtime'}),
            ('genre', _('Kategorien'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('beschreibung', _('Beschreibung'), FieldType.TEXTAREA, False, {'lookup_attribute': 'description'}),
            ('ean', _('Barcode / EAN'), FieldType.BARCODE, False, {'lookup_attribute': 'ean'}),
            ('bild', _('Cover / Bild'), FieldType.IMAGE, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
        ],
    },
    'generic': {
        'label': _('Sonstiges / Gemischt'),
        'kind': '',
        'fields': [
            ('name', _('Name / Bezeichnung'), FieldType.TEXT, True, {'lookup_attribute': 'title'}),
            ('marke', _('Marke / Hersteller'), FieldType.TEXT, False, {'lookup_attribute': 'brand'}),
            ('kategorie', _('Kategorie'), FieldType.TEXT, False, {'lookup_attribute': 'categories'}),
            ('beschreibung', _('Beschreibung'), FieldType.TEXTAREA, False, {'lookup_attribute': 'description'}),
            ('ean', _('Barcode / EAN'), FieldType.BARCODE, False, {'lookup_attribute': 'ean'}),
            ('bild', _('Bild'), FieldType.IMAGE, False, {'lookup_attribute': 'cover_url'}),
            ('ort', _('Ort / Platz'), FieldType.TEXT, False, {}),
            ('kaufdatum', _('Kaufdatum'), FieldType.DATE, False, {}),
            ('preis', _('Preis'), FieldType.PRICE, False, {}),
            ('beleg', _('Beleg'), FieldType.FILE, False, {}),
        ],
    },
}


def create_preset(collection: Collection, preset: str) -> None:
    """Seed a collection with one of the ready-to-use presets.

    Also stores the preset's media kind on the collection so auto-fill and
    price search query the matching sources/platforms.
    """
    fields = []
    for index, (key, label, field_type, required, config) in enumerate(PRESETS[preset]['fields']):
        config = dict(config)
        if field_type == FieldType.PRICE:
            config.update(_price_config())
        fields.append(FieldDefinition(
            collection=collection, key=key, label=label, field_type=field_type,
            required=required, order=index, is_system=True, config=config,
        ))
    FieldDefinition.objects.bulk_create(fields)
    kind = PRESETS[preset].get('kind', '')
    if collection.lookup_provider != kind:
        collection.lookup_provider = kind
        collection.save(update_fields=['lookup_provider', 'updated_at'])


def collections_for_user(user):
    """All collections a user may at least view (owned or shared with them)."""
    return (
        Collection.objects.filter(Q(owner=user) | Q(shares__user=user))
        .distinct()
        .select_related('owner')
    )


def copy_structure(source: Collection, target: Collection) -> None:
    """Copy a collection's configuration (fields + item types) onto a new one.

    Implements "reuse already configured fields from another collection". Only the
    structure is copied, not the items — and the copies are fully editable
    afterwards (system fields become normal fields).
    """
    if target.lookup_provider != source.lookup_provider:
        target.lookup_provider = source.lookup_provider  # same media kind
        target.save(update_fields=['lookup_provider', 'updated_at'])
    FieldDefinition.objects.bulk_create([
        FieldDefinition(
            collection=target, key=fd.key, label=fd.label, field_type=fd.field_type,
            help_text=fd.help_text, required=fd.required, order=fd.order,
            is_system=fd.is_system, config=fd.config,
        )
        for fd in source.fields.all()
    ])
    key_to_field = {f.key: f for f in target.fields.all()}

    for it in source.item_types.all():
        new_type = ItemType.objects.create(
            collection=target, name=it.name, description=it.description, order=it.order,
        )
        required = [key_to_field[k] for k in it.required_fields.values_list('key', flat=True)
                    if k in key_to_field]
        if required:
            new_type.required_fields.set(required)
