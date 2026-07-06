"""Dynamic filtering of items by their user-defined fields.

The active filters live entirely in the URL query string, so any filtered view
has a shareable link — exactly what the QR-code filters in the concept encode
(e.g. "all items in shelf 3"). Lookups are chosen to behave identically on
SQLite and PostgreSQL (exact / icontains / numeric & ISO-date range).
"""

from __future__ import annotations

from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from .models import FieldType

TEXTLIKE_TYPES = {
    FieldType.TEXT, FieldType.TEXTAREA, FieldType.ISBN,
    FieldType.BARCODE, FieldType.EMAIL, FieldType.URL,
}
NUMERIC_TYPES = {FieldType.NUMBER, FieldType.YEAR, FieldType.DECIMAL, FieldType.PRICE}
RANGE_DATE_TYPES = {FieldType.DATE, FieldType.DATETIME}
BOOL_CHOICES = [('', _('Alle')), ('true', _('Ja')), ('false', _('Nein'))]


class ItemFilterForm(forms.Form):
    """Optional filter inputs generated from a collection's fields."""

    def __init__(self, *args, collection, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.field_defs = list(collection.fields.all())

        self.fields['q'] = forms.CharField(
            required=False, label=_('Suche'),
            widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('Volltextsuche…')}),
        )
        item_types = collection.item_types.all()
        if item_types:
            self.fields['type'] = forms.ModelChoiceField(
                queryset=item_types, required=False, label=_('Art'), empty_label=_('Alle Arten'),
                widget=forms.Select(attrs={'class': 'form-select'}),
            )

        for fd in self.field_defs:
            self._add_field_filters(fd)

    def _add_field_filters(self, fd) -> None:
        t = fd.field_type
        sel = {'class': 'form-select'}
        ctl = {'class': 'form-control'}
        if t in (FieldType.CHOICE, FieldType.MULTICHOICE):
            choices = [('', _('Alle'))] + [(c, c) for c in (fd.config or {}).get('choices', [])]
            self.fields[f'f_{fd.key}'] = forms.ChoiceField(
                choices=choices, required=False, label=fd.label, widget=forms.Select(attrs=sel))
        elif t == FieldType.BOOLEAN:
            self.fields[f'f_{fd.key}'] = forms.ChoiceField(
                choices=BOOL_CHOICES, required=False, label=fd.label, widget=forms.Select(attrs=sel))
        elif t in NUMERIC_TYPES:
            self.fields[f'min_{fd.key}'] = forms.DecimalField(
                required=False, label=_('%(label)s von') % {'label': fd.label},
                widget=forms.NumberInput(attrs={**ctl, 'step': 'any'}))
            self.fields[f'max_{fd.key}'] = forms.DecimalField(
                required=False, label=_('%(label)s bis') % {'label': fd.label},
                widget=forms.NumberInput(attrs={**ctl, 'step': 'any'}))
        elif t in RANGE_DATE_TYPES:
            self.fields[f'from_{fd.key}'] = forms.DateField(
                required=False, label=_('%(label)s von') % {'label': fd.label},
                widget=forms.DateInput(attrs={**ctl, 'type': 'date'}, format='%Y-%m-%d'))
            self.fields[f'to_{fd.key}'] = forms.DateField(
                required=False, label=_('%(label)s bis') % {'label': fd.label},
                widget=forms.DateInput(attrs={**ctl, 'type': 'date'}, format='%Y-%m-%d'))
        elif t in TEXTLIKE_TYPES:
            self.fields[f'f_{fd.key}'] = forms.CharField(
                required=False, label=fd.label, widget=forms.TextInput(attrs=ctl))
        # IMAGE/FILE/MULTICHOICE: not filterable in this iteration.

    def apply(self, queryset):
        """Return the queryset narrowed by whatever filters are set."""
        if not self.is_valid():
            return queryset
        data = self.cleaned_data

        q_text = data.get('q')
        if q_text:
            search = Q()
            for fd in self.field_defs:
                if fd.field_type in TEXTLIKE_TYPES or fd.field_type == FieldType.CHOICE:
                    search |= Q(**{f'values__{fd.key}__icontains': q_text})
            if search:
                queryset = queryset.filter(search)

        if data.get('type'):
            queryset = queryset.filter(item_type=data['type'])

        for fd in self.field_defs:
            key, t = fd.key, fd.field_type
            if t == FieldType.CHOICE and data.get(f'f_{key}'):
                queryset = queryset.filter(**{f'values__{key}': data[f'f_{key}']})
            elif t == FieldType.BOOLEAN and data.get(f'f_{key}'):
                queryset = queryset.filter(**{f'values__{key}': data[f'f_{key}'] == 'true'})
            elif t in TEXTLIKE_TYPES and data.get(f'f_{key}'):
                queryset = queryset.filter(**{f'values__{key}__icontains': data[f'f_{key}']})
            elif t in NUMERIC_TYPES:
                if data.get(f'min_{key}') is not None:
                    queryset = queryset.filter(**{f'values__{key}__gte': float(data[f'min_{key}'])})
                if data.get(f'max_{key}') is not None:
                    queryset = queryset.filter(**{f'values__{key}__lte': float(data[f'max_{key}'])})
            elif t in RANGE_DATE_TYPES:
                if data.get(f'from_{key}'):
                    queryset = queryset.filter(**{f'values__{key}__gte': data[f'from_{key}'].isoformat()})
                if data.get(f'to_{key}'):
                    queryset = queryset.filter(**{f'values__{key}__lte': data[f'to_{key}'].isoformat()})

        # Multichoice values are JSON lists; membership is filtered in Python so it
        # works identically on SQLite and PostgreSQL (and handles non-ASCII options
        # that SQLite stores escaped). Done last, narrowing the DB-filtered set.
        multichoice = [
            (fd.key, data.get(f'f_{fd.key}'))
            for fd in self.field_defs
            if fd.field_type == FieldType.MULTICHOICE and data.get(f'f_{fd.key}')
        ]
        if multichoice:
            ids = [
                pk for pk, values in queryset.values_list('pk', 'values')
                if all(val in ((values or {}).get(key) or []) for key, val in multichoice)
            ]
            queryset = queryset.filter(pk__in=ids)
        return queryset

    @property
    def active_count(self) -> int:
        """How many filters are currently set (for UI badges)."""
        if not self.is_bound:
            return 0
        data = self.cleaned_data if self.is_valid() else {}
        return sum(1 for k, v in data.items() if v not in (None, '', []))
