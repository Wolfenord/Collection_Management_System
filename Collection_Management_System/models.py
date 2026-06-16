"""Data model for the Collection Management System.

Design (chosen with the user):
  * Each Collection is owned by a user and can be shared with others (view/edit).
  * Fields are NOT hardcoded columns. Every column a user sees is a
    ``FieldDefinition`` row belonging to the collection — this makes the schema
    fully dynamic (add/remove/reorder fields at any time, also on existing data).
  * Item values are stored as JSON on the ``Item`` (``values`` mapping field
    ``key`` -> value). Files/images can't live in JSON, so they are stored as
    ``ItemAsset`` rows and referenced from the JSON by asset id.
  * "Art" (item type) is modelled as ``ItemType``; per type a different set of
    fields can be marked required.

The two truly fixed columns from the concept are structural, not FieldDefinitions:
  * the mandatory item ID  -> ``Item.id`` (UUID primary key)
  * "Art"                  -> ``Item.item_type``
Everything else ("Name", "Ort", "Kaufdatum", "Preis", "Beleg", "Bild") is seeded
as removable *system* FieldDefinitions on collection creation.
"""

import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class FieldType(models.TextChoices):
    TEXT = 'text', 'Text (kurz)'
    TEXTAREA = 'textarea', 'Text (lang)'
    NUMBER = 'number', 'Ganzzahl'
    DECIMAL = 'decimal', 'Dezimalzahl'
    PRICE = 'price', 'Preis / Währung'
    BOOLEAN = 'boolean', 'Ja / Nein'
    DATE = 'date', 'Datum'
    YEAR = 'year', 'Jahr'
    TIME = 'time', 'Uhrzeit'
    DATETIME = 'datetime', 'Datum & Uhrzeit'
    CHOICE = 'choice', 'Auswahl (eine)'
    MULTICHOICE = 'multichoice', 'Auswahl (mehrere)'
    ISBN = 'isbn', 'ISBN'
    BARCODE = 'barcode', 'Barcode / EAN'
    URL = 'url', 'Link (URL)'
    EMAIL = 'email', 'E-Mail'
    IMAGE = 'image', 'Bild'
    FILE = 'file', 'Datei'


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Collection(TimeStampedModel):
    """A user's collection. Has its own stable, unique UUID."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='collections',
    )
    name = models.CharField('Name', max_length=200)
    description = models.TextField('Beschreibung', blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name

    def user_permission(self, user) -> str | None:
        """Return 'owner', 'edit', 'view' or None for the given user."""
        if self.owner_id == getattr(user, 'id', None):
            return 'owner'
        share = self.shares.filter(user=user).first()
        return share.permission if share else None


class CollectionShare(models.Model):
    """Grants another user access to a collection (row-level sharing)."""

    class Permission(models.TextChoices):
        VIEW = 'view', 'Nur ansehen'
        EDIT = 'edit', 'Bearbeiten'

    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='shares')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='shared_collections',
    )
    permission = models.CharField(max_length=10, choices=Permission.choices, default=Permission.VIEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['collection', 'user'], name='unique_collection_share'),
        ]

    def __str__(self) -> str:
        return f'{self.user} → {self.collection} ({self.permission})'


class ItemType(models.Model):
    """The "Art" of an item within a collection (e.g. Buch, Film, Bild)."""

    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='item_types')
    name = models.CharField('Bezeichnung', max_length=120)
    description = models.TextField('Beschreibung', blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['collection', 'name'], name='unique_itemtype_per_collection'),
        ]

    def __str__(self) -> str:
        return self.name


class FieldDefinition(models.Model):
    """A single dynamic column of a collection."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='fields')
    key = models.SlugField('Schlüssel', max_length=80)  # stable id used in Item.values
    label = models.CharField('Bezeichnung', max_length=150)
    field_type = models.CharField('Typ', max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    help_text = models.CharField('Hilfetext', max_length=300, blank=True)
    required = models.BooleanField('Pflichtfeld', default=False)
    order = models.PositiveIntegerField(default=0)
    is_system = models.BooleanField(default=False)  # seeded default field (still removable)
    # Type-specific options: choices list, currency, min/max, decimals, etc.
    config = models.JSONField(default=dict, blank=True)
    # Per "Art": fields additionally required when the item has one of these types.
    required_for_types = models.ManyToManyField(
        ItemType, blank=True, related_name='required_fields'
    )

    class Meta:
        ordering = ['order', 'label']
        constraints = [
            models.UniqueConstraint(fields=['collection', 'key'], name='unique_field_key_per_collection'),
        ]

    def __str__(self) -> str:
        return f'{self.label} ({self.get_field_type_display()})'


class Item(TimeStampedModel):
    """A single object in a collection. Its own UUID is the mandatory item ID."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='items')
    item_type = models.ForeignKey(
        ItemType, on_delete=models.SET_NULL, null=True, blank=True, related_name='items'
    )
    # Dynamic field values: {field_key: value}. Files/images store an asset id.
    values = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_items',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return self.values.get('name') or str(self.id)


def item_asset_path(instance: 'ItemAsset', filename: str) -> str:
    return f'collections/{instance.item.collection_id}/items/{instance.item_id}/{filename}'


class ItemAsset(models.Model):
    """An uploaded file/image bound to one item field (referenced from JSON)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='assets')
    field_key = models.SlugField(max_length=80)
    # max_length raised from the default 100: upload paths contain two UUIDs.
    file = models.FileField(upload_to=item_asset_path, max_length=255)
    original_name = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.original_name or self.file.name
