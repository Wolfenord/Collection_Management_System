from django.urls import path

from . import api, views

urlpatterns = [
    # JSON API v1 (token auth, gated by the api_enabled runtime setting)
    path('api/collections/', api.api_collections, name='api_collections'),
    path('api/collections/<uuid:pk>/', api.api_collection_detail, name='api_collection_detail'),
    path('api/collections/<uuid:pk>/items/', api.api_items, name='api_items'),
    path('api/collections/<uuid:pk>/items/<uuid:item_pk>/', api.api_item, name='api_item'),

    path('', views.dashboard, name='dashboard'),
    path('search/', views.global_search, name='global_search'),
    path('settings/', views.site_settings, name='site_settings'),
    path('settings/export.ini', views.site_settings_export, name='site_settings_export'),
    path('collections/', views.collection_list, name='collection_list'),
    path('collections/new/', views.collection_create, name='collection_create'),
    path('collections/<uuid:pk>/', views.collection_detail, name='collection_detail'),
    path('collections/<uuid:pk>/edit/', views.collection_edit, name='collection_edit'),
    path('collections/<uuid:pk>/delete/', views.collection_delete, name='collection_delete'),

    # Fields (dynamic columns)
    path('collections/<uuid:pk>/fields/new/', views.field_create, name='field_create'),
    path('collections/<uuid:pk>/fields/reorder/', views.field_reorder, name='field_reorder'),
    path('collections/<uuid:pk>/fields/<uuid:field_pk>/edit/', views.field_edit, name='field_edit'),
    path('collections/<uuid:pk>/fields/<uuid:field_pk>/delete/', views.field_delete, name='field_delete'),

    # Sharing
    path('collections/<uuid:pk>/shares/', views.collection_shares, name='collection_shares'),
    path('collections/<uuid:pk>/shares/<int:share_pk>/delete/', views.share_delete, name='share_delete'),

    # Statistics
    path('collections/<uuid:pk>/stats/', views.collection_statistics, name='collection_statistics'),

    # Export
    path('collections/<uuid:pk>/export.xlsx', views.collection_export, name='collection_export'),
    path('collections/<uuid:pk>/labels.pdf', views.collection_labels, name='collection_labels'),

    # QR / barcode
    path('collections/<uuid:pk>/qr/', views.collection_qr, name='collection_qr'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/qr/', views.item_qr, name='item_qr'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/barcode/', views.item_barcode, name='item_barcode'),

    # External-database auto-fill
    path('collections/<uuid:pk>/lookup/', views.item_lookup, name='item_lookup'),
    path('collections/<uuid:pk>/search/', views.item_search, name='item_search'),
    path('collections/<uuid:pk>/find/', views.item_find, name='item_find'),
    path('collections/<uuid:pk>/loans/', views.collection_loans, name='collection_loans'),
    path('collections/<uuid:pk>/import/', views.collection_import, name='collection_import'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/lend/', views.item_lend, name='item_lend'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/return/', views.item_return, name='item_return'),

    # Item types ("Art")
    path('collections/<uuid:pk>/types/new/', views.type_create, name='type_create'),

    # Trash (soft-deleted items)
    path('collections/<uuid:pk>/trash/', views.collection_trash, name='collection_trash'),
    path('collections/<uuid:pk>/trash/empty/', views.trash_empty, name='trash_empty'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/restore/', views.item_restore, name='item_restore'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/purge/', views.item_purge, name='item_purge'),

    # Items
    path('collections/<uuid:pk>/items/bulk/', views.items_bulk, name='items_bulk'),
    path('collections/<uuid:pk>/items/new/', views.item_create, name='item_create'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/', views.item_detail, name='item_detail'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/edit/', views.item_edit, name='item_edit'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/duplicate/', views.item_duplicate, name='item_duplicate'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/delete/', views.item_delete, name='item_delete'),
]
