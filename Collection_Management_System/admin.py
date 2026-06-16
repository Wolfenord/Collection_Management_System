from django.contrib import admin

from .models import (
    Collection,
    CollectionShare,
    FieldDefinition,
    Item,
    ItemAsset,
    ItemType,
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


admin.site.register(ItemType)
admin.site.register(CollectionShare)
admin.site.register(ItemAsset)
