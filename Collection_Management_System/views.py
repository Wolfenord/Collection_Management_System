from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
import json

from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils.text import slugify

from . import codes, export, labels, services, statistics
from .dynamic_forms import DynamicItemForm
from .filters import ItemFilterForm
from .forms import CollectionForm, FieldDefinitionForm, ItemTypeForm, ShareForm
from .models import Collection, CollectionShare, FieldDefinition, Item, ItemAsset, ItemType
from .rendering import item_row
from .services import collections_for_user, create_default_fields


def _get_collection_for(user, pk, *, need_edit=False) -> Collection:
    """Fetch a collection only if ``user`` may access it, else raise 403/404."""
    collection = get_object_or_404(Collection, pk=pk)
    permission = collection.user_permission(user)
    if permission is None:
        raise PermissionDenied
    if need_edit and permission not in ('owner', 'edit'):
        raise PermissionDenied
    return collection


@login_required
def dashboard(request):
    collections = list(
        collections_for_user(request.user)
        .annotate(item_count=Count('items'))
        .prefetch_related('fields', 'items')
    )
    owned = [c for c in collections if c.owner_id == request.user.id]
    shared = [c for c in collections if c.owner_id != request.user.id]
    total_value = sum(
        statistics.total_price_value(c.items.all(), c.fields.all()) for c in collections
    )
    context = {
        'collections': collections,
        'owned_count': len(owned),
        'shared_count': len(shared),
        'total_items': sum(c.item_count for c in collections),
        'total_value': total_value,
    }
    return render(request, 'collections/dashboard.html', context)


@login_required
def collection_list(request):
    collections = collections_for_user(request.user).annotate(item_count=Count('items'))
    return render(request, 'collections/collection_list.html', {'collections': collections})


@login_required
def collection_create(request):
    if request.method == 'POST':
        form = CollectionForm(request.POST, user=request.user)
        if form.is_valid():
            collection = form.save(commit=False)
            collection.owner = request.user
            collection.save()
            template = form.cleaned_data.get('template')
            if template:
                services.copy_structure(template, collection)
                messages.success(request, f'Sammlung erstellt — Felder aus „{template.name}“ übernommen.')
            else:
                create_default_fields(collection)
                messages.success(request, 'Sammlung erstellt — Standardfelder wurden angelegt.')
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = CollectionForm(user=request.user)
    return render(request, 'collections/collection_form.html', {'form': form})


@login_required
def collection_detail(request, pk):
    collection = _get_collection_for(request.user, pk)
    permission = collection.user_permission(request.user)
    fields = list(collection.fields.all())

    filter_form = ItemFilterForm(request.GET or None, collection=collection)
    items_qs = filter_form.apply(collection.items.select_related('item_type'))
    rows = [{'item': item, 'cells': item_row(item, fields)} for item in items_qs[:200]]

    context = {
        'collection': collection,
        'fields': fields,
        'item_types': collection.item_types.all(),
        'rows': rows,
        'item_count': collection.items.count(),
        'result_count': items_qs.count(),
        'filter_form': filter_form,
        'active_filters': filter_form.active_count,
        'share_url': request.build_absolute_uri(),
        'permission': permission,
        'can_edit': permission in ('owner', 'edit'),
    }
    return render(request, 'collections/collection_detail.html', context)


# --- Sharing -------------------------------------------------------------------

def _get_owned_collection(user, pk) -> Collection:
    """Only the owner may manage sharing."""
    collection = get_object_or_404(Collection, pk=pk)
    if collection.owner_id != user.id:
        raise PermissionDenied
    return collection


@login_required
def collection_shares(request, pk):
    collection = _get_owned_collection(request.user, pk)
    if request.method == 'POST':
        form = ShareForm(request.POST, collection=collection)
        if form.is_valid():
            _, created = form.save()
            messages.success(request, 'Sammlung freigegeben.' if created else 'Freigabe aktualisiert.')
            return redirect('collection_shares', pk=pk)
    else:
        form = ShareForm(collection=collection)
    return render(request, 'collections/shares.html', {
        'collection': collection,
        'form': form,
        'shares': collection.shares.select_related('user'),
    })


@login_required
def share_delete(request, pk, share_pk):
    collection = _get_owned_collection(request.user, pk)
    share = get_object_or_404(CollectionShare, pk=share_pk, collection=collection)
    if request.method == 'POST':
        user = share.user
        share.delete()
        messages.success(request, f'Freigabe für {user} entfernt.')
    return redirect('collection_shares', pk=pk)


# --- Field (column) management -------------------------------------------------

@login_required
def field_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = FieldDefinitionForm(request.POST, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, 'Feld hinzugefügt.')
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = FieldDefinitionForm(collection=collection)
    return render(request, 'collections/field_form.html',
                  {'form': form, 'collection': collection, 'title': 'Neues Feld'})


@login_required
def field_edit(request, pk, field_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    field = get_object_or_404(FieldDefinition, pk=field_pk, collection=collection)
    if request.method == 'POST':
        form = FieldDefinitionForm(request.POST, instance=field, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, 'Feld aktualisiert.')
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = FieldDefinitionForm(instance=field, collection=collection)
    return render(request, 'collections/field_form.html',
                  {'form': form, 'collection': collection, 'title': f'Feld bearbeiten: {field.label}'})


@login_required
@require_POST
def field_reorder(request, pk):
    """Persist a new field (column) order from the drag & drop UI (AJAX)."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    try:
        ids = json.loads(request.body or '{}').get('order', [])
    except json.JSONDecodeError:
        return HttpResponseBadRequest('invalid JSON')

    fields = {str(f.pk): f for f in collection.fields.all()}
    to_update = []
    for index, fid in enumerate(ids):
        field = fields.get(str(fid))
        if field and field.order != index:
            field.order = index
            to_update.append(field)
    FieldDefinition.objects.bulk_update(to_update, ['order'])
    return JsonResponse({'ok': True, 'updated': len(to_update)})


@login_required
def field_delete(request, pk, field_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    field = get_object_or_404(FieldDefinition, pk=field_pk, collection=collection)
    if request.method == 'POST':
        key, label = field.key, field.label
        # Remove the column's data everywhere it is used.
        for item in collection.items.all():
            if key in (item.values or {}):
                item.values.pop(key, None)
                item.save(update_fields=['values', 'updated_at'])
        ItemAsset.objects.filter(item__collection=collection, field_key=key).delete()
        field.delete()
        messages.success(request, f'Feld „{label}“ und zugehörige Daten wurden entfernt.')
        return redirect('collection_detail', pk=collection.pk)
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection, 'object_label': f'Feld „{field.label}“',
        'warning': 'Alle in diesem Feld gespeicherten Werte werden bei allen Gegenständen gelöscht.',
    })


# --- Item type ("Art") management ---------------------------------------------

@login_required
def type_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = ItemTypeForm(request.POST, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, 'Art hinzugefügt.')
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = ItemTypeForm(collection=collection)
    return render(request, 'collections/type_form.html',
                  {'form': form, 'collection': collection, 'title': 'Neue Art'})


# --- Item CRUD (dynamic forms) -------------------------------------------------

@login_required
def item_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = DynamicItemForm(request.POST, request.FILES, collection=collection)
        if form.is_valid():
            form.save(user=request.user)
            messages.success(request, 'Gegenstand hinzugefügt.')
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = DynamicItemForm(collection=collection)
    return render(request, 'collections/item_form.html',
                  {'form': form, 'collection': collection, 'title': 'Neuer Gegenstand'})


@login_required
def item_edit(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    if request.method == 'POST':
        form = DynamicItemForm(request.POST, request.FILES, collection=collection, instance=item)
        if form.is_valid():
            form.save(user=request.user)
            messages.success(request, 'Gegenstand aktualisiert.')
            return redirect('item_detail', pk=collection.pk, item_pk=item.pk)
    else:
        form = DynamicItemForm(collection=collection, instance=item)
    return render(request, 'collections/item_form.html',
                  {'form': form, 'collection': collection, 'title': 'Gegenstand bearbeiten'})


@login_required
def item_detail(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk)
    item = get_object_or_404(Item.objects.select_related('item_type'), pk=item_pk, collection=collection)
    fields = list(collection.fields.all())
    pairs = list(zip(fields, item_row(item, fields)))
    permission = collection.user_permission(request.user)
    return render(request, 'collections/item_detail.html', {
        'collection': collection, 'item': item, 'pairs': pairs,
        'can_edit': permission in ('owner', 'edit'),
    })


@login_required
def item_delete(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    if request.method == 'POST':
        item.delete()
        messages.success(request, 'Gegenstand gelöscht.')
        return redirect('collection_detail', pk=collection.pk)
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection, 'object_label': f'Gegenstand „{item}“',
        'warning': 'Der Gegenstand und seine hochgeladenen Dateien werden gelöscht.',
    })


# --- Statistics ----------------------------------------------------------------

@login_required
def collection_statistics(request, pk):
    """Dynamic statistics for a collection (optionally over a filtered subset)."""
    collection = _get_collection_for(request.user, pk)
    fields = list(collection.fields.all())
    filter_form = ItemFilterForm(request.GET or None, collection=collection)
    items = list(filter_form.apply(collection.items.select_related('item_type')))

    stats = statistics.collection_stats(collection, items, fields)
    context = {
        'collection': collection,
        'stats': stats,
        'total_value': statistics.total_price_value(items, fields),
        'filter_form': filter_form,
        'active_filters': filter_form.active_count,
        'permission': collection.user_permission(request.user),
    }
    return render(request, 'collections/statistics.html', context)


# --- Excel export --------------------------------------------------------------

@login_required
def collection_export(request, pk):
    """Download the collection (honouring active filters) as an .xlsx file."""
    collection = _get_collection_for(request.user, pk)
    fields = list(collection.fields.all())
    filter_form = ItemFilterForm(request.GET or None, collection=collection)
    items = filter_form.apply(collection.items.select_related('item_type'))

    data = export.build_workbook(collection, items, fields, request.build_absolute_uri)
    filename = f"{slugify(collection.name) or 'export'}.xlsx"
    response = HttpResponse(
        data, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# --- Printable label sheet -----------------------------------------------------

@login_required
def collection_labels(request, pk):
    """Download a printable PDF of QR-code labels (honouring active filters)."""
    collection = _get_collection_for(request.user, pk)
    filter_form = ItemFilterForm(request.GET or None, collection=collection)
    items = list(filter_form.apply(collection.items.select_related('item_type')))
    pdf = labels.build_label_pdf(
        items, request.build_absolute_uri,
        lambda item: reverse('item_detail', args=[collection.pk, item.pk]),
    )
    filename = f"etiketten-{slugify(collection.name) or 'labels'}.pdf"
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# --- QR codes & barcodes -------------------------------------------------------

def _png(data: bytes, filename: str, download: bool) -> HttpResponse:
    response = HttpResponse(data, content_type='image/png')
    disposition = 'attachment' if download else 'inline'
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return response


@login_required
def collection_qr(request, pk):
    """QR encoding the collection's detail URL including the current filters."""
    collection = _get_collection_for(request.user, pk)
    target = request.build_absolute_uri(reverse('collection_detail', args=[pk]))
    query = request.GET.copy()
    query.pop('download', None)
    if query:
        target += '?' + query.urlencode()
    return _png(codes.qr_png(target), f'filter-{pk}.png', 'download' in request.GET)


@login_required
def item_qr(request, pk, item_pk):
    """QR encoding the absolute URL of an item's detail page."""
    collection = _get_collection_for(request.user, pk)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    target = request.build_absolute_uri(reverse('item_detail', args=[pk, item.pk]))
    return _png(codes.qr_png(target), f'item-{item.pk}-qr.png', 'download' in request.GET)


@login_required
def item_barcode(request, pk, item_pk):
    """Code128 barcode of the item's short code."""
    collection = _get_collection_for(request.user, pk)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    return _png(codes.barcode_png(codes.item_short_code(item)),
                f'item-{item.pk}-barcode.png', 'download' in request.GET)
