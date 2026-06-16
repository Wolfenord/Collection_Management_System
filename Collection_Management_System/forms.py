from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.text import slugify

from .models import Collection, CollectionShare, FieldDefinition, FieldType, ItemType

CHOICE_FIELD_TYPES = {FieldType.CHOICE, FieldType.MULTICHOICE}
User = get_user_model()


class CollectionForm(forms.ModelForm):
    template = forms.ModelChoiceField(
        queryset=Collection.objects.none(), required=False,
        label='Felder übernehmen aus',
        empty_label='— Standardfelder verwenden —',
        help_text='Optional: Felder & Arten einer bestehenden Sammlung übernehmen (frei anpassbar).',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = Collection
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'z. B. Meine Büchersammlung'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            # Import here to avoid a circular import at module load.
            from .services import collections_for_user
            self.fields['template'].queryset = collections_for_user(user)


class FieldDefinitionForm(forms.ModelForm):
    """Create/edit one dynamic column of a collection."""

    choices_text = forms.CharField(
        required=False, label='Auswahlmöglichkeiten',
        help_text='Eine Option pro Zeile (nur für Auswahl-Felder).',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )

    class Meta:
        model = FieldDefinition
        fields = ['label', 'key', 'field_type', 'required', 'help_text', 'order']
        widgets = {
            'label': forms.TextInput(attrs={'class': 'form-control'}),
            'key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'wird aus der Bezeichnung erzeugt'}),
            'field_type': forms.Select(attrs={'class': 'form-select'}),
            'required': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'help_text': forms.TextInput(attrs={'class': 'form-control'}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
        help_texts = {
            'key': 'Interner, eindeutiger Name (z. B. „kaufdatum“). Leer lassen – wird aus der Bezeichnung erzeugt.',
            'field_type': 'Bestimmt Eingabe und Auswertung (z. B. Preis = rechenbar, ISBN = scanbar).',
            'required': 'Muss beim Anlegen eines Gegenstands immer ausgefüllt werden.',
            'help_text': 'Optionaler Hinweis, der dem Nutzer am Eingabefeld angezeigt wird.',
            'order': 'Reihenfolge der Spalte (kleinere Zahl = weiter vorne).',
        }

    def __init__(self, *args, collection, **kwargs):
        super().__init__(*args, **kwargs)
        self.collection = collection
        self.instance.collection = collection  # so the unique (collection, key) check works
        self.fields['key'].required = False
        if self.instance.pk:
            self.fields['choices_text'].initial = '\n'.join((self.instance.config or {}).get('choices', []))

    def clean_key(self):
        key = self.cleaned_data.get('key') or ''
        if not key:
            key = slugify(self.cleaned_data.get('label', ''))
        return key

    def clean(self):
        cleaned = super().clean()
        config = dict(self.instance.config or {})
        if cleaned.get('field_type') in CHOICE_FIELD_TYPES:
            options = [line.strip() for line in (cleaned.get('choices_text') or '').splitlines() if line.strip()]
            config['choices'] = options
        else:
            config.pop('choices', None)
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
            queryset=collection.fields.all(), required=False, label='Pflichtfelder für diese Art',
            widget=forms.SelectMultiple(attrs={'class': 'form-select', 'size': 6}),
            initial=(self.instance.required_fields.all() if self.instance.pk else None),
        )

    def save(self, commit=True):
        item_type = super().save(commit=commit)
        if commit:
            item_type.required_fields.set(self.cleaned_data['required_fields'])
        return item_type


class ShareForm(forms.Form):
    """Share a collection with another user by username or e-mail."""

    identifier = forms.CharField(
        label='Benutzername oder E-Mail',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'z. B. anna oder anna@example.com'}),
    )
    permission = forms.ChoiceField(
        label='Berechtigung', choices=CollectionShare.Permission.choices,
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
            raise forms.ValidationError('Kein Nutzer mit diesem Namen oder dieser E-Mail gefunden.')
        if user == self.collection.owner:
            raise forms.ValidationError('Diese Sammlung gehört dem Nutzer bereits.')
        self.target_user = user
        return ident

    def save(self):
        return CollectionShare.objects.update_or_create(
            collection=self.collection, user=self.target_user,
            defaults={'permission': self.cleaned_data['permission']},
        )
