"""Business logic helpers for collections (kept out of views/models)."""

from __future__ import annotations

from django.db.models import Q

from .models import Collection, FieldDefinition, FieldType, ItemType

# Standard columns every new collection starts with. The mandatory item ID
# (Item.id) and "Art" (Item.item_type) are structural and not listed here.
# All of these are removable later (is_system only marks their origin).
DEFAULT_FIELDS: list[tuple[str, str, str, bool, dict]] = [
    ('name', 'Name / Bezeichnung', FieldType.TEXT, True, {}),
    ('ort', 'Ort / Platz', FieldType.TEXT, False, {}),
    ('kaufdatum', 'Kaufdatum', FieldType.DATE, False, {}),
    ('preis', 'Preis', FieldType.PRICE, False, {'currency': 'EUR'}),
    ('beleg', 'Beleg', FieldType.FILE, False, {}),
    ('bild', 'Bild', FieldType.IMAGE, False, {}),
]


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
            config=config,
        )
        for index, (key, label, field_type, required, config) in enumerate(DEFAULT_FIELDS)
    ]
    FieldDefinition.objects.bulk_create(fields)


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
