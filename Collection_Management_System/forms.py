from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from . import lookup_providers, runtime_settings
from .models import Collection, CollectionShare, FieldDefinition, FieldType, ItemType

CHOICE_FIELD_TYPES = {FieldType.CHOICE, FieldType.MULTICHOICE}
User = get_user_model()

def _preset_choices():
    """Built-in, ready-to-use presets from ``services.PRESETS`` (books, movies,
    music, games, …) — lazy so the registry stays the single source of truth."""
    from .services import PRESETS
    return [('', _('— Standardfelder verwenden —'))] + [
        (key, preset['label']) for key, preset in PRESETS.items()
    ]


class CollectionForm(forms.ModelForm):
    preset = forms.ChoiceField(
        choices=_preset_choices, required=False,
        label=_('Vorlage'),
        help_text=_('Fertige Feld-Vorlage mit passenden Spalten (z. B. Bücher, Filme, Musik, '
                    'Videospiele). Bei „Bücher“ befüllt die automatische Datenbanksuche die '
                    'Felder per ISBN-Scan oder Vorschlagssuche. Alles nachträglich anpassbar.'),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    template = forms.ModelChoiceField(
        queryset=Collection.objects.none(), required=False,
        label=_('…oder Felder übernehmen aus'),
        empty_label=_('— bestehende Sammlung wählen —'),
        help_text=_('Optional: Felder & Arten einer bestehenden Sammlung übernehmen (frei anpassbar).'),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = Collection
        fields = ['name', 'description', 'lookup_provider']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('z. B. Meine Büchersammlung')}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Note: UUID pks have a default, so a *new* instance already carries a pk —
        # `_state.adding` is the reliable "not yet saved" signal here.
        if not self.instance._state.adding:
            # Editing an existing collection: presets/templates only apply on
            # creation — instead the media kind (which external databases and
            # price platforms are used) becomes changeable.
            del self.fields['preset']
            del self.fields['template']
            self.fields['lookup_provider'] = forms.ChoiceField(
                choices=lookup_providers.MEDIA_KINDS, required=False,
                label=_('Schwerpunkt der Sammlung'),
                help_text=_('Bestimmt, welche externen Datenbanken die automatische '
                            'Suche abfragt und welche Plattformen der Preisvergleich '
                            'vorschlägt.'),
                widget=forms.Select(attrs={'class': 'form-select'}),
            )
        else:
            # Creation: the preset/template determines the media kind.
            del self.fields['lookup_provider']
            if user is not None:
                # Import here to avoid a circular import at module load.
                from .services import collections_for_user
                self.fields['template'].queryset = collections_for_user(user)


class FieldDefinitionForm(forms.ModelForm):
    """Create/edit one dynamic column of a collection."""

    choices_text = forms.CharField(
        required=False, label=_('Auswahlmöglichkeiten'),
        help_text=_('Eine Option pro Zeile (nur für Auswahl-Felder).'),
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    lookup_attribute = forms.ChoiceField(
        required=False, label=_('Automatisch befüllen mit'),
        help_text=_('Optional: Welcher Wert aus der externen Datenbank (z. B. nach ISBN-Scan) '
                    'dieses Feld füllt. Nur wirksam, wenn die Sammlung eine externe Datenbank nutzt.'),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = FieldDefinition
        fields = ['label', 'key', 'field_type', 'required', 'help_text', 'order']
        widgets = {
            'label': forms.TextInput(attrs={'class': 'form-control'}),
            'key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('wird aus der Bezeichnung erzeugt')}),
            'field_type': forms.Select(attrs={'class': 'form-select'}),
            'required': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'help_text': forms.TextInput(attrs={'class': 'form-control'}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
        help_texts = {
            'key': _('Interner, eindeutiger Name (z. B. „kaufdatum“). Leer lassen – wird aus der Bezeichnung erzeugt.'),
            'field_type': _('Bestimmt Eingabe und Auswertung (z. B. Preis = rechenbar, ISBN = scanbar).'),
            'required': _('Muss beim Anlegen eines Gegenstands immer ausgefüllt werden.'),
            'help_text': _('Optionaler Hinweis, der dem Nutzer am Eingabefeld angezeigt wird.'),
            'order': _('Reihenfolge der Spalte (kleinere Zahl = weiter vorne).'),
        }

    def __init__(self, *args, collection, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.instance.collection = collection  # so the unique (collection, key) check works
        self.fields['key'].required = False
        self.fields['lookup_attribute'].choices = (
            [('', _('— nicht automatisch befüllen —'))] + lookup_providers.ATTRIBUTES
        )
        if self.instance.pk:
            config = self.instance.config or {}
            self.fields['choices_text'].initial = '\n'.join(config.get('choices', []))
            self.fields['lookup_attribute'].initial = config.get('lookup_attribute', '')

    def clean_key(self):
        key = self.cleaned_data.get('key') or ''
        if not key:
            key = slugify(self.cleaned_data.get('label', ''))
        if key.startswith('_'):
            # Leading underscores are reserved for internal keys (e.g. the
            # per-item photo gallery stores assets under '__gallery').
            raise forms.ValidationError(
                _('Schlüssel dürfen nicht mit „_“ beginnen (intern reserviert).'))
        return key

    def clean(self):
        cleaned = super().clean()
        config = dict(self.instance.config or {})
        if cleaned.get('field_type') in CHOICE_FIELD_TYPES:
            options = [line.strip() for line in (cleaned.get('choices_text') or '').splitlines() if line.strip()]
            config['choices'] = options
        else:
            config.pop('choices', None)

        attribute = cleaned.get('lookup_attribute')
        if attribute:
            config['lookup_attribute'] = attribute
        else:
            config.pop('lookup_attribute', None)

        self.instance.config = config
        return cleaned


class ItemTypeForm(forms.ModelForm):
    """Create/edit an "Art" and choose which fields are required for it."""

    class Meta:
        model = ItemType
        fields = ['name', 'description', 'order']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, collection, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.instance.collection = collection
        self.fields['required_fields'] = forms.ModelMultipleChoiceField(
            queryset=collection.fields.all(), required=False, label=_('Pflichtfelder für diese Art'),
            widget=forms.SelectMultiple(attrs={'class': 'form-select', 'size': 6}),
            initial=(self.instance.required_fields.all() if self.instance.pk else None),
        )

    def save(self, commit=True):
        item_type = super().save(commit=commit)
        if commit:
            item_type.required_fields.set(self.cleaned_data['required_fields'])
        return item_type


class SiteSettingsForm(forms.Form):
    """Staff form for the database-backed runtime settings.

    Fully dynamic: one input per entry in ``runtime_settings.REGISTRY`` — a new
    registered setting appears here (and on the settings page) automatically.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, definition in runtime_settings.REGISTRY.items():
            initial = runtime_settings.get_setting(key)
            if definition.kind == 'bool':
                self.fields[key] = forms.BooleanField(
                    required=False, label=definition.label, help_text=definition.help_text,
                    initial=initial,
                    widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
                )
            elif definition.kind == 'int':
                self.fields[key] = forms.IntegerField(
                    label=definition.label, help_text=definition.help_text,
                    initial=initial,
                    min_value=definition.min_value, max_value=definition.max_value,
                    widget=forms.NumberInput(attrs={'class': 'form-control'}),
                )
            elif definition.choices is not None:
                self.fields[key] = forms.ChoiceField(
                    label=definition.label, help_text=definition.help_text,
                    initial=initial, choices=definition.choices,
                    widget=forms.Select(attrs={'class': 'form-select'}),
                )
            else:
                # required=False: the empty string is a legitimate value
                # (e.g. "no extension restriction").
                widget = (forms.Textarea(attrs={'class': 'form-control', 'rows': 3})
                          if definition.multiline
                          else forms.TextInput(attrs={'class': 'form-control'}))
                self.fields[key] = forms.CharField(
                    required=False, label=definition.label, help_text=definition.help_text,
                    initial=initial, max_length=definition.max_length,
                    widget=widget,
                )

    def save(self, user=None) -> None:
        for key in runtime_settings.REGISTRY:
            runtime_settings.set_setting(key, self.cleaned_data[key], user=user)


class ShareForm(forms.Form):
    """Share a collection with another user by username or e-mail."""

    identifier = forms.CharField(
        label=_('Benutzername oder E-Mail'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('z. B. anna oder anna@example.com')}),
    )
    permission = forms.ChoiceField(
        label=_('Berechtigung'), choices=CollectionShare.Permission.choices,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def __init__(self, *args, collection, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.target_user = None

    def clean_identifier(self):
        ident = self.cleaned_data['identifier'].strip()
        user = User.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first()
        if user is None:
            raise forms.ValidationError(_('Kein Nutzer mit diesem Namen oder dieser E-Mail gefunden.'))
        if user == self.collection.owner:
            raise forms.ValidationError(_('Diese Sammlung gehört dem Nutzer bereits.'))
        self.target_user = user
        return ident

    def save(self):
        share, created = CollectionShare.objects.update_or_create(
            collection=self.collection, user=self.target_user,
            defaults={'permission': self.cleaned_data['permission']},
        )
        # Tell the recipient about the (new or changed) access — bell menu.
        from django.urls import reverse
        from .models import Notification
        Notification.push(
            share.user, kind=Notification.KIND_SHARE, key=f'share:{share.pk}',
            url=reverse('collection_detail', args=[self.collection.pk]),
            payload={'user': str(self.collection.owner),
                     'collection': self.collection.name,
                     'permission': share.permission},
        )
        return share, created
