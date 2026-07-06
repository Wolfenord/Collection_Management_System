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
from datetime import timedelta

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class FieldType(models.TextChoices):
    TEXT = 'text', _('Text (kurz)')
    TEXTAREA = 'textarea', _('Text (lang)')
    NUMBER = 'number', _('Ganzzahl')
    DECIMAL = 'decimal', _('Dezimalzahl')
    PRICE = 'price', _('Preis / Währung')
    BOOLEAN = 'boolean', _('Ja / Nein')
    DATE = 'date', _('Datum')
    YEAR = 'year', _('Jahr')
    TIME = 'time', _('Uhrzeit')
    DATETIME = 'datetime', _('Datum & Uhrzeit')
    CHOICE = 'choice', _('Auswahl (eine)')
    MULTICHOICE = 'multichoice', _('Auswahl (mehrere)')
    ISBN = 'isbn', _('ISBN')
    BARCODE = 'barcode', _('Barcode / EAN')
    URL = 'url', _('Link (URL)')
    EMAIL = 'email', _('E-Mail')
    IMAGE = 'image', _('Bild')
    FILE = 'file', _('Datei')


class SiteSetting(models.Model):
    """One database-backed runtime setting (page size, loan period, …).

    Only *overrides* are stored: a missing row means "use the INI/code default".
    The set of known keys, their types, defaults and validation live in
    ``runtime_settings.REGISTRY``; values are read through
    ``runtime_settings.get_setting`` (cached) and edited by staff users on the
    settings page or in the Django admin.
    """

    key = models.CharField(_('Schlüssel'), max_length=64, unique=True)
    value = models.JSONField(_('Wert'), null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name=_('Geändert von'),
    )

    class Meta:
        ordering = ['key']
        verbose_name = _('Systemeinstellung')
        verbose_name_plural = _('Systemeinstellungen')

    def __str__(self) -> str:
        return f'{self.key} = {self.value!r}'


class SettingChange(models.Model):
    """Audit history: one row per *effective* change of a runtime setting.

    Written by ``runtime_settings.set_setting`` / the admin — never edited.
    ``old_value`` holds the previously effective value (which may have come
    from the INI/code default, not a DB row).
    """

    key = models.CharField(_('Schlüssel'), max_length=64, db_index=True)
    old_value = models.JSONField(_('Alter Wert'), null=True, blank=True)
    new_value = models.JSONField(_('Neuer Wert'), null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name=_('Geändert von'),
    )
    changed_at = models.DateTimeField(_('Geändert am'), auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']
        verbose_name = _('Einstellungsänderung')
        verbose_name_plural = _('Einstellungsänderungen')

    def __str__(self) -> str:
        return f'{self.key}: {self.old_value!r} → {self.new_value!r}'


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
    name = models.CharField(_('Name'), max_length=200)
    description = models.TextField(_('Beschreibung'), blank=True)
    # Legacy column: auto-fill used to be tied to ONE selected provider per
    # collection. Lookups now always query every registered database (see
    # ``lookup_providers.auto_provider``), so this value is ignored. The column
    # only survives to avoid a destructive migration.
    lookup_provider = models.CharField(_('Externe Datenbank'), max_length=40, blank=True, default='')

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
        VIEW = 'view', _('Nur ansehen')
        EDIT = 'edit', _('Bearbeiten')

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
    name = models.CharField(_('Bezeichnung'), max_length=120)
    description = models.TextField(_('Beschreibung'), blank=True)
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
    key = models.SlugField(_('Schlüssel'), max_length=80)  # stable id used in Item.values
    label = models.CharField(_('Bezeichnung'), max_length=150)
    field_type = models.CharField(_('Typ'), max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    help_text = models.CharField(_('Hilfetext'), max_length=300, blank=True)
    required = models.BooleanField(_('Pflichtfeld'), default=False)
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


class ItemManager(models.Manager):
    """Default manager: hides soft-deleted (trashed) items everywhere.

    Being the *default* manager, reverse relations (``collection.items``) and
    ``get_object_or_404(Item, …)`` exclude the trash automatically. Use
    ``Item.all_objects`` to reach trashed items (trash page, restore, purge).
    Note: aggregates over the reverse relation (``Count('items')``) bypass
    managers — filter those with ``Q(items__deleted_at__isnull=True)``.
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Item(TimeStampedModel):
    """A single object in a collection. Its own UUID is the mandatory item ID.

    Deleting via the UI is a *soft* delete (``deleted_at`` set — the item moves
    to the collection's trash and can be restored). Rows are really removed by
    ``purge()`` / the trash retention (runtime setting ``trash_retention_days``).
    """

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
    deleted_at = models.DateTimeField(_('Gelöscht am'), null=True, blank=True, db_index=True)

    objects = ItemManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return self.values.get('name') or str(self.id)

    @property
    def active_loan(self) -> 'Loan | None':
        return self.loans.filter(returned_at__isnull=True).first()

    def soft_delete(self) -> None:
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at', 'updated_at'])

    def restore(self) -> None:
        self.deleted_at = None
        self.save(update_fields=['deleted_at', 'updated_at'])

    def purge(self) -> None:
        """Delete for real, including the uploaded files on disk."""
        for asset in self.assets.all():
            asset.file.delete(save=False)
        self.delete()


class Loan(models.Model):
    """Records that an item is (or was) lent to somebody.

    A loan with ``returned_at IS NULL`` is open; an item has at most one open
    loan (enforced in the view). Closed loans stay as history.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='loans')
    borrower = models.CharField(_('Verliehen an'), max_length=120)
    lent_at = models.DateField(_('Verliehen am'), default=timezone.localdate)
    due_at = models.DateField(_('Rückgabe bis'), null=True, blank=True)
    note = models.CharField(_('Notiz'), max_length=255, blank=True)
    returned_at = models.DateField(_('Zurückgegeben am'), null=True, blank=True)
    # When the last overdue reminder e-mail went out (see send_loan_reminders).
    reminder_sent_at = models.DateTimeField(_('Erinnerung gesendet am'), null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['-lent_at']

    def __str__(self) -> str:
        return f'{self.item} → {self.borrower}'

    @property
    def is_overdue(self) -> bool:
        """Open loan past its agreed return date (or, without one, older than the
        configurable default loan period — runtime setting ``loan_overdue_days``)."""
        if self.returned_at is not None:
            return False
        today = timezone.localdate()
        if self.due_at:
            return self.due_at < today
        from .runtime_settings import get_setting  # deferred: avoids import cycle
        return self.lent_at <= today - timedelta(days=get_setting('loan_overdue_days'))


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
