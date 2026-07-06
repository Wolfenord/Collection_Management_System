"""Build a real Django form on the fly from a collection's FieldDefinitions.

This is what makes items fully dynamic: there is no fixed Item form — instead we
generate one form field per FieldDefinition, map it to an appropriate widget,
prefill it from the item's JSON ``values`` (and ``ItemAsset`` files), validate it
(including per-"Art" required rules) and write the result back into JSON + assets.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django import forms
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.translation import gettext_lazy as _

from . import lookup_providers
from .models import FieldType, Item, ItemAsset

FILE_TYPES = {FieldType.IMAGE, FieldType.FILE}
NUMERIC_TYPES = {FieldType.NUMBER, FieldType.YEAR, FieldType.DECIMAL, FieldType.PRICE}
ITEM_TYPE_KEY = '__item_type'


def _to_jsonable(value):
    """Convert cleaned values to JSON-native types.

    Decimals are stored as floats (not strings) so numeric range filtering works
    the same way on SQLite and PostgreSQL.
    """
    if isinstance(value, Decimal):
        return float(value)
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def build_form_field(fd, *, required: bool) -> forms.Field:
    """Return a form field + widget appropriate for one FieldDefinition."""
    cfg = fd.config or {}
    common = {'label': fd.label, 'required': required, 'help_text': fd.help_text}
    t = fd.field_type

    if t == FieldType.TEXTAREA:
        return forms.CharField(widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}), **common)
    if t == FieldType.NUMBER:
        return forms.IntegerField(widget=forms.NumberInput(attrs={'class': 'form-control'}), **common)
    if t == FieldType.DECIMAL:
        return forms.DecimalField(widget=forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}), **common)
    if t == FieldType.PRICE:
        return forms.DecimalField(
            max_digits=12, decimal_places=2,
            widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}), **common,
        )
    if t == FieldType.BOOLEAN:
        return forms.BooleanField(widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
                                  **{**common, 'required': False})
    if t == FieldType.DATE:
        return forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'},
                                                      format='%Y-%m-%d'), **common)
    if t == FieldType.YEAR:
        return forms.IntegerField(min_value=0, max_value=9999,
                                  widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'max': 9999}),
                                  **common)
    if t == FieldType.TIME:
        return forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'},
                                                      format='%H:%M'), **common)
    if t == FieldType.DATETIME:
        return forms.DateTimeField(
            input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'],
            widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'},
                                       format='%Y-%m-%dT%H:%M'), **common,
        )
    if t in (FieldType.CHOICE, FieldType.MULTICHOICE):
        choices = [(c, c) for c in cfg.get('choices', [])]
        if t == FieldType.MULTICHOICE:
            return forms.MultipleChoiceField(choices=choices,
                                             widget=forms.SelectMultiple(attrs={'class': 'form-select'}), **common)
        return forms.ChoiceField(choices=[('', '---------')] + choices,
                                 widget=forms.Select(attrs={'class': 'form-select'}), **common)
    if t == FieldType.ISBN:
        return forms.CharField(max_length=20,
                               widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '978-…',
                                                             'data-scan': 'isbn'}), **common)
    if t == FieldType.BARCODE:
        return forms.CharField(max_length=64,
                               widget=forms.TextInput(attrs={'class': 'form-control', 'data-scan': 'barcode'}), **common)
    if t == FieldType.URL:
        return forms.URLField(widget=forms.URLInput(attrs={'class': 'form-control'}), **common)
    if t == FieldType.EMAIL:
        return forms.EmailField(widget=forms.EmailInput(attrs={'class': 'form-control'}), **common)
    if t == FieldType.IMAGE:
        return forms.ImageField(widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*',
                                                                       'data-capture': 'image'}),
                                **common)
    if t == FieldType.FILE:
        return forms.FileField(widget=forms.ClearableFileInput(attrs={'class': 'form-control',
                                                                      'data-capture': 'file'}), **common)

    # Default: short text.
    return forms.CharField(max_length=cfg.get('max_length') or 255,
                           widget=forms.TextInput(attrs={'class': 'form-control'}), **common)


class DynamicItemForm(forms.Form):
    """A form whose fields are generated from a collection's FieldDefinitions."""

    def __init__(self, *args, collection, instance: Item | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.instance = instance
        self.field_defs = list(collection.fields.all())
        values = (instance.values if instance else {}) or {}
        assets = {a.field_key: a for a in instance.assets.all()} if instance else {}

        # "Art" selector (only if the collection defines item types).
        item_types = collection.item_types.all()
        if item_types:
            self.fields[ITEM_TYPE_KEY] = forms.ModelChoiceField(
                queryset=item_types, required=False, label=_('Art'),
                widget=forms.Select(attrs={'class': 'form-select'}),
                initial=(instance.item_type_id if instance else None),
            )

        for fd in self.field_defs:
            self.fields[fd.key] = build_form_field(fd, required=fd.required)
            if fd.field_type in FILE_TYPES:
                # Prefill the existing file so it isn't lost / re-required on edit.
                asset = assets.get(fd.key)
                if asset:
                    self.initial[fd.key] = asset.file
            elif fd.key in values and values[fd.key] not in (None, ''):
                self.initial[fd.key] = values[fd.key]

    def clean(self):
        cleaned = super().clean()
        item_type = cleaned.get(ITEM_TYPE_KEY)
        if item_type:
            required_keys = set(item_type.required_fields.values_list('key', flat=True))
            for fd in self.field_defs:
                if fd.key in required_keys and fd.field_type != FieldType.BOOLEAN:
                    if cleaned.get(fd.key) in (None, '', []):
                        self.add_error(fd.key, _('Für die gewählte Art ist dieses Feld erforderlich.'))
        self._validate_uploads(cleaned)
        return cleaned

    def _validate_uploads(self, cleaned) -> None:
        """Enforce the runtime upload limits (``upload_max_mb`` and
        ``upload_allowed_extensions``) on every newly uploaded file."""
        from .runtime_settings import allowed_upload_extensions, get_setting

        max_mb = get_setting('upload_max_mb')
        allowed = allowed_upload_extensions()
        for fd in self.field_defs:
            if fd.field_type not in FILE_TYPES:
                continue
            value = cleaned.get(fd.key)
            if not isinstance(value, UploadedFile):
                continue  # unchanged stored file / empty: nothing new to check
            if value.size > max_mb * 1024 * 1024:
                self.add_error(fd.key, _('Datei ist zu groß (maximal %(mb)s MB).') % {'mb': max_mb})
            name = value.name or ''
            extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if allowed and extension not in allowed:
                self.add_error(fd.key, _('Dateityp „%(ext)s“ ist nicht erlaubt. Erlaubt: %(allowed)s')
                               % {'ext': extension or '?', 'allowed': ', '.join(sorted(allowed))})

    def save(self, user=None) -> Item:
        instance = self.instance or Item(collection=self.collection)
        if user and instance._state.adding:
            instance.created_by = user
        if ITEM_TYPE_KEY in self.fields:
            instance.item_type = self.cleaned_data.get(ITEM_TYPE_KEY)
        instance.save()  # ensure a PK exists for assets

        values = dict(instance.values or {})
        for fd in self.field_defs:
            value = self.cleaned_data.get(fd.key)
            if fd.field_type in FILE_TYPES:
                self._save_file(instance, fd, value, values)
                if not isinstance(value, UploadedFile) and value is not False:
                    self._save_cover(instance, fd, values)
            else:
                values[fd.key] = _to_jsonable(value)
        instance.values = values
        instance.save()
        return instance

    def _save_cover(self, instance: Item, fd, values: dict) -> None:
        """Adopt an auto-fill cover: lookup.js posts the provider's cover URL as
        ``<key>__cover_url``; download it server-side (host-whitelisted) into an
        asset. An uploaded file or an already stored one always wins.
        """
        cover_url = (self.data.get(f'{fd.key}__cover_url') or '').strip()
        if not cover_url or instance.assets.filter(field_key=fd.key).exists():
            return
        result = lookup_providers.fetch_cover(cover_url)
        if not result:
            return
        body, extension = result
        name = f'cover.{extension}'
        asset = ItemAsset.objects.create(
            item=instance, field_key=fd.key, file=ContentFile(body, name=name), original_name=name,
        )
        values[fd.key] = {'asset_id': str(asset.id), 'name': asset.original_name, 'url': asset.file.url}

    @staticmethod
    def _save_file(instance: Item, fd, value, values: dict) -> None:
        if value is False:  # "Clear" checkbox ticked
            instance.assets.filter(field_key=fd.key).delete()
            values.pop(fd.key, None)
        elif isinstance(value, UploadedFile):  # newly uploaded file replaces old
            instance.assets.filter(field_key=fd.key).delete()
            asset = ItemAsset.objects.create(
                item=instance, field_key=fd.key, file=value, original_name=value.name[:255],
            )
            values[fd.key] = {'asset_id': str(asset.id), 'name': asset.original_name, 'url': asset.file.url}
        # value is a FieldFile (unchanged) or None (empty): keep what's stored.
