from django import forms
from django.contrib import admin
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from . import runtime_settings
from .models import (
    Collection,
    CollectionShare,
    FieldDefinition,
    Item,
    ItemAsset,
    ItemType,
    Loan,
    SettingChange,
    SiteSetting,
)


class FieldDefinitionInline(admin.TabularInline):
    model = FieldDefinition
    extra = 0


class ItemTypeInline(admin.TabularInline):
    model = ItemType
    extra = 0


class CollectionShareInline(admin.TabularInline):
    model = CollectionShare
    extra = 0


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'created_at')
    search_fields = ('name', 'owner__username')
    inlines = [ItemTypeInline, FieldDefinitionInline, CollectionShareInline]


@admin.register(FieldDefinition)
class FieldDefinitionAdmin(admin.ModelAdmin):
    list_display = ('label', 'collection', 'field_type', 'required', 'is_system', 'order')
    list_filter = ('field_type', 'required', 'is_system')


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'collection', 'item_type', 'created_at')
    list_filter = ('collection', 'item_type')


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ('item', 'borrower', 'lent_at', 'returned_at')
    list_filter = ('returned_at',)
    search_fields = ('borrower',)


admin.site.register(ItemType)
admin.site.register(CollectionShare)
admin.site.register(ItemAsset)


class SiteSettingForm(forms.ModelForm):
    """Validates the raw JSON value against the setting registry, so the admin
    can't store keys or values the application wouldn't understand."""

    class Meta:
        model = SiteSetting
        fields = ['key', 'value']

    def clean(self):
        cleaned = super().clean()
        key = cleaned.get('key')
        definition = runtime_settings.REGISTRY.get(key)
        if definition is None:
            known = ', '.join(sorted(runtime_settings.REGISTRY))
            raise forms.ValidationError(
                _('Unbekannter Schlüssel „%(key)s“. Bekannt sind: %(known)s')
                % {'key': key, 'known': known})
        try:
            cleaned['value'] = definition.coerce(cleaned.get('value'))
        except (ValueError, TypeError) as exc:
            raise forms.ValidationError(
                _('Ungültiger Wert für „%(key)s“: %(error)s') % {'key': key, 'error': exc})
        return cleaned


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    form = SiteSettingForm
    list_display = ('key', 'value', 'updated_at', 'updated_by')
    readonly_fields = ('updated_at', 'updated_by')

    def save_model(self, request, obj, form, change):
        old = (runtime_settings.get_setting(obj.key)
               if obj.key in runtime_settings.REGISTRY else None)
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        cache.delete(runtime_settings._CACHE_KEY)
        if old != obj.value:
            SettingChange.objects.create(key=obj.key, old_value=old,
                                         new_value=obj.value, changed_by=request.user)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        cache.delete(runtime_settings._CACHE_KEY)

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        cache.delete(runtime_settings._CACHE_KEY)


@admin.register(SettingChange)
class SettingChangeAdmin(admin.ModelAdmin):
    """Read-only audit log of runtime-setting changes."""

    list_display = ('changed_at', 'key', 'old_value', 'new_value', 'changed_by')
    list_filter = ('key',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
