from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('collections/', views.collection_list, name='collection_list'),
    path('collections/new/', views.collection_create, name='collection_create'),
    path('collections/<uuid:pk>/', views.collection_detail, name='collection_detail'),

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

    # Item types ("Art")
    path('collections/<uuid:pk>/types/new/', views.type_create, name='type_create'),

    # Items
    path('collections/<uuid:pk>/items/new/', views.item_create, name='item_create'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/', views.item_detail, name='item_detail'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/edit/', views.item_edit, name='item_edit'),
    path('collections/<uuid:pk>/items/<uuid:item_pk>/delete/', views.item_delete, name='item_delete'),
]
