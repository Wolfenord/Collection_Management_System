from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db.models import Count, Q
import json
import os
import re
import uuid
from datetime import date
from urllib.parse import urlencode

from django.http import (FileResponse, HttpResponse, HttpResponseBadRequest, JsonResponse,
                         QueryDict)
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from . import (codes, export, imports, labels, lookup_providers, price_search, restore,
               services, statistics)
from .dynamic_forms import DynamicItemForm
from .filters import ItemFilterForm
from .forms import CollectionForm, FieldDefinitionForm, ItemTypeForm, ShareForm, SiteSettingsForm
from .models import (GALLERY_KEY, Collection, CollectionShare, FieldDefinition, FieldType,
                     Item, ItemAsset, ItemType, Loan, Notification, SavedView)
from .rendering import item_row
from .runtime_settings import get_setting, get_setting_for
from .services import collections_for_user, create_default_fields

# Field types that can't be edited inline in the items table (they need the full
# form: file pickers, multi-select). Everything else is a single-value input.
INLINE_NONEDITABLE_TYPES = {FieldType.IMAGE, FieldType.FILE, FieldType.MULTICHOICE}


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
    from django.db.models import Prefetch
    collections = list(
        collections_for_user(request.user)
        # Aggregates bypass the default manager, so exclude the trash explicitly.
        .annotate(item_count=Count('items', filter=Q(items__deleted_at__isnull=True)))
        # The total-value sum only reads ``values`` — don't drag every column
        # of every item into memory for it.
        .prefetch_related(
            'fields',
            Prefetch('items', queryset=Item.objects.only('values', 'collection_id')),
        )
    )
    owned = [c for c in collections if c.owner_id == request.user.id]
    shared = [c for c in collections if c.owner_id != request.user.id]
    total_value = sum(
        statistics.total_price_value(c.items.all(), c.fields.all()) for c in collections
    )
    open_loans = list(
        Loan.objects.filter(item__collection__in=collections, returned_at__isnull=True,
                            item__deleted_at__isnull=True)
        .select_related('item', 'item__collection')
        .order_by('lent_at')[:10]
    )
    for loan in open_loans:
        loan.overdue = loan.is_overdue
    context = {
        'collections': collections,
        'owned_count': len(owned),
        'shared_count': len(shared),
        'total_items': sum(c.item_count for c in collections),
        'total_value': total_value,
        'open_loans': open_loans,
    }
    return render(request, 'collections/dashboard.html', context)


@login_required
def global_search(request):
    """Search across all accessible collections at once: collection names and
    descriptions plus every text-like field value of their items."""
    query = (request.GET.get('q') or '').strip()
    collections = list(collections_for_user(request.user).prefetch_related('fields'))

    collection_hits, item_hits = [], []
    if query:
        collection_hits = [
            c for c in collections
            if query.lower() in c.name.lower() or query.lower() in c.description.lower()
        ][:20]

        from .filters import TEXTLIKE_TYPES
        searchable = Q()
        for c in collections:
            for fd in c.fields.all():
                if fd.field_type in TEXTLIKE_TYPES or fd.field_type == FieldType.CHOICE:
                    searchable |= Q(collection=c, **{f'values__{fd.key}__icontains': query})
        searchable |= Q(collection__in=collections, item_type__name__icontains=query)
        if searchable:
            item_hits = list(
                Item.objects.filter(searchable)
                .select_related('collection', 'item_type')
                .order_by('collection__name', '-created_at')[:get_setting('global_search_max_items')]
            )

    return render(request, 'collections/search.html', {
        'query': query,
        'collection_hits': collection_hits,
        'item_hits': item_hits,
    })


@login_required
def collection_list(request):
    collections = collections_for_user(request.user).annotate(
        item_count=Count('items', filter=Q(items__deleted_at__isnull=True)))
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
            preset = form.cleaned_data.get('preset')
            if template:
                services.copy_structure(template, collection)
                messages.success(request, _('Sammlung erstellt — Felder aus „%(name)s“ übernommen.')
                                 % {'name': template.name})
            elif preset in services.PRESETS:
                services.create_preset(collection, preset)
                messages.success(request, _('Sammlung erstellt — Vorlage „%(label)s“ angelegt. '
                                            'Felder lassen sich jederzeit anpassen.')
                                 % {'label': services.PRESETS[preset]['label']})
            else:
                create_default_fields(collection)
                messages.success(request, _('Sammlung erstellt — Standardfelder wurden angelegt.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = CollectionForm(user=request.user)
    return render(request, 'collections/collection_form.html', {'form': form})


def _apply_sort(request, items_qs, fields):
    """Order the item queryset by ``?sort=<field key|type>&dir=asc|desc``.

    Dynamic columns sort via the JSON key (works on SQLite and PostgreSQL);
    unknown keys are ignored so stale links can't raise.
    """
    sort = request.GET.get('sort') or ''
    descending = request.GET.get('dir') == 'desc'
    prefix = '-' if descending else ''
    if sort == 'type':
        items_qs = items_qs.order_by(f'{prefix}item_type__name', '-created_at')
    elif sort in {f.key for f in fields}:
        items_qs = items_qs.order_by(f'{prefix}values__{sort}', '-created_at')
    return items_qs, sort, descending


def _column_headers(request, fields, sort, descending):
    """Clickable table headers: each carries a link that sorts by its column
    (toggling the direction on the active one) while keeping all filters."""
    headers = [{'label': _('Art'), 'key': 'type'}]
    headers += [{'label': f.label, 'key': f.key} for f in fields]
    for header in headers:
        params = request.GET.copy()
        params.pop('page', None)
        params['sort'] = header['key']
        params['dir'] = 'desc' if (sort == header['key'] and not descending) else 'asc'
        header['url'] = '?' + params.urlencode()
        header['active'] = sort == header['key']
        header['desc'] = descending
    return headers


@login_required
def collection_detail(request, pk):
    collection = _get_collection_for(request.user, pk)
    permission = collection.user_permission(request.user)
    fields = list(collection.fields.all())
    # Annotate each field with the human label of its auto-fill mapping (if any).
    attribute_labels = lookup_providers.ATTRIBUTE_LABELS
    for field in fields:
        attribute = (field.config or {}).get('lookup_attribute')
        field.lookup_label = attribute_labels.get(attribute, '') if attribute else ''
        field.inline_editable = field.field_type not in INLINE_NONEDITABLE_TYPES

    filter_form = ItemFilterForm(request.GET or None, collection=collection)
    items_qs = filter_form.apply(collection.items.select_related('item_type'))
    items_qs, sort, descending = _apply_sort(request, items_qs, fields)
    # Quick page-size override (?per_page=) on top of the profile/site setting.
    try:
        per_page = min(500, max(5, int(request.GET.get('per_page', ''))))
    except (TypeError, ValueError):
        per_page = get_setting_for(request.user, 'items_per_page')
    paginator = Paginator(items_qs, per_page)
    page_obj = paginator.get_page(request.GET.get('page'))
    # Pair each cell with its field so the template can render data-labels
    # (needed for the stacked card layout of the items table on phones).
    rows = [
        {'item': item, 'cells': [
            {'field': fd, 'cell': cell, 'raw': (item.values or {}).get(fd.key)}
            for fd, cell in zip(fields, item_row(item, fields))
        ]}
        for item in page_obj
    ]

    context = {
        'collection': collection,
        'fields': fields,
        'item_types': collection.item_types.all(),
        'rows': rows,
        'item_count': collection.items.count(),
        'result_count': paginator.count,
        'page_obj': page_obj,
        'page_range': paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1),
        'columns': _column_headers(request, fields, sort, descending),
        'per_page': per_page,
        'per_page_options': (25, 50, 100),
        'filter_form': filter_form,
        'active_filters': filter_form.active_count,
        'share_url': request.build_absolute_uri(),
        'permission': permission,
        'can_edit': permission in ('owner', 'edit'),
        'lookup_provider_label': lookup_providers.provider_for(collection).label,
        'saved_views': collection.saved_views.all(),
        'current_querystring': request.GET.urlencode(),
        'open_loan_count': Loan.objects.filter(item__collection=collection,
                                               returned_at__isnull=True,
                                               item__deleted_at__isnull=True).count(),
        'trash_count': Item.all_objects.filter(collection=collection,
                                               deleted_at__isnull=False).count(),
    }
    return render(request, 'collections/collection_detail.html', context)


@login_required
def collection_edit(request, pk):
    """Rename a collection / change its description (owner or edit permission)."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = CollectionForm(request.POST, instance=collection, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Sammlung aktualisiert.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = CollectionForm(instance=collection, user=request.user)
    return render(request, 'collections/collection_form.html',
                  {'form': form, 'collection': collection,
                   'title': _('Sammlung bearbeiten: %(name)s') % {'name': collection.name}})


@login_required
def collection_delete(request, pk):
    """Delete a whole collection incl. items, fields and shares (owner only)."""
    collection = _get_owned_collection(request.user, pk)
    if request.method == 'POST':
        name = collection.name
        collection.delete()
        messages.success(request, _('Sammlung „%(name)s“ wurde gelöscht.') % {'name': name})
        return redirect('dashboard')
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection,
        'object_label': _('Sammlung „%(name)s“') % {'name': collection.name},
        'warning': _('Alle Gegenstände, Felder, Arten, Ausleihen und Freigaben dieser '
                     'Sammlung werden unwiderruflich gelöscht.'),
    })


# --- Site-wide runtime settings (staff only) ------------------------------------

@login_required
def site_settings(request):
    """Edit the database-backed runtime settings (page size, loan period,
    registration policy, …). Staff only; the form is generated from
    ``runtime_settings.REGISTRY``."""
    if not request.user.is_staff:
        raise PermissionDenied
    if request.method == 'POST':
        form = SiteSettingsForm(request.POST)
        if form.is_valid():
            form.save(user=request.user)
            messages.success(request, _('Einstellungen gespeichert — sie gelten ab sofort.'))
            return redirect('site_settings')
    else:
        form = SiteSettingsForm()
    from .models import SettingChange
    from .runtime_settings import REGISTRY
    history = list(SettingChange.objects.select_related('changed_by')[:20])
    for change in history:
        definition = REGISTRY.get(change.key)
        change.label = definition.label if definition else change.key
    return render(request, 'collections/site_settings.html',
                  {'form': form, 'history': history})


@login_required
def site_settings_export(request):
    """Download the effective runtime settings as an ``[app-defaults]`` INI
    snippet — paste it into another instance's ``config.ini`` to transfer the
    configuration (staff only)."""
    if not request.user.is_staff:
        raise PermissionDenied
    from . import runtime_settings
    lines = ['# CMS runtime settings export — merge into config.ini', '[app-defaults]']
    for key, value in runtime_settings.all_settings().items():
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        # Multi-line values (e.g. the imprint address) need INI continuation
        # indentation, otherwise the file would not parse back.
        value = str(value).replace('\n', '\n\t')
        lines.append(f'{key} = {value}')
    response = HttpResponse('\n'.join(lines) + '\n', content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="cms-settings.ini"'
    return response


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
            _share, created = form.save()
            messages.success(request, _('Sammlung freigegeben.') if created else _('Freigabe aktualisiert.'))
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
        # The access is gone — its bell notification must not linger.
        Notification.objects.filter(user=user, key=f'share:{share.pk}').delete()
        share.delete()
        messages.success(request, _('Freigabe für %(user)s entfernt.') % {'user': user})
    return redirect('collection_shares', pk=pk)


# --- Notifications (bell menu) ---------------------------------------------------

@login_required
def notification_open(request, pk):
    """Mark one notification read and jump to its target."""
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=['read_at'])
    return redirect(notification.url or 'dashboard')


@login_required
@require_POST
def notifications_read_all(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect(request.POST.get('next') or 'dashboard')


# --- Field (column) management -------------------------------------------------

@login_required
def field_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = FieldDefinitionForm(request.POST, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, _('Feld hinzugefügt.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = FieldDefinitionForm(collection=collection)
    return render(request, 'collections/field_form.html',
                  {'form': form, 'collection': collection, 'title': _('Neues Feld')})


@login_required
def field_edit(request, pk, field_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    field = get_object_or_404(FieldDefinition, pk=field_pk, collection=collection)
    if request.method == 'POST':
        form = FieldDefinitionForm(request.POST, instance=field, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, _('Feld aktualisiert.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = FieldDefinitionForm(instance=field, collection=collection)
    return render(request, 'collections/field_form.html',
                  {'form': form, 'collection': collection,
                   'title': _('Feld bearbeiten: %(label)s') % {'label': field.label}})


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
        # Remove the column's data everywhere it is used (incl. trashed items).
        for item in Item.all_objects.filter(collection=collection):
            if key in (item.values or {}):
                item.values.pop(key, None)
                item.save(update_fields=['values', 'updated_at'])
        ItemAsset.objects.filter(item__collection=collection, field_key=key).delete()
        field.delete()
        messages.success(request, _('Feld „%(label)s“ und zugehörige Daten wurden entfernt.') % {'label': label})
        return redirect('collection_detail', pk=collection.pk)
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection, 'object_label': _('Feld „%(label)s“') % {'label': field.label},
        'warning': _('Alle in diesem Feld gespeicherten Werte werden bei allen Gegenständen gelöscht.'),
    })


# --- Item type ("Art") management ---------------------------------------------

@login_required
def type_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = ItemTypeForm(request.POST, collection=collection)
        if form.is_valid():
            form.save()
            messages.success(request, _('Art hinzugefügt.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = ItemTypeForm(collection=collection)
    return render(request, 'collections/type_form.html',
                  {'form': form, 'collection': collection, 'title': _('Neue Art')})


@login_required
def type_edit(request, pk, type_pk):
    """Rename an "Art" / change its per-type required fields."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item_type = get_object_or_404(ItemType, pk=type_pk, collection=collection)
    if request.method == 'POST':
        form = ItemTypeForm(request.POST, collection=collection, instance=item_type)
        if form.is_valid():
            form.save()
            messages.success(request, _('Art aktualisiert.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        form = ItemTypeForm(collection=collection, instance=item_type)
    return render(request, 'collections/type_form.html',
                  {'form': form, 'collection': collection, 'title': _('Art bearbeiten')})


@login_required
def type_delete(request, pk, type_pk):
    """Delete an "Art". Items keep existing (their item_type is SET_NULL)."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item_type = get_object_or_404(ItemType, pk=type_pk, collection=collection)
    if request.method == 'POST':
        name = item_type.name
        item_type.delete()
        messages.success(request, _('Art „%(name)s“ wurde entfernt.') % {'name': name})
        return redirect('collection_detail', pk=collection.pk)
    count = collection.items.filter(item_type=item_type).count()
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection, 'object_label': _('Art „%(name)s“') % {'name': item_type.name},
        'warning': (_('%(count)s Gegenstand/Gegenstände verlieren dadurch ihre Art-Zuordnung '
                      '(die Gegenstände selbst bleiben erhalten).') % {'count': count}
                    if count else
                    _('Diese Art wird gelöscht. Es sind keine Gegenstände zugeordnet.')),
    })


# --- External-database auto-fill ----------------------------------------------

def _query_key_for(collection, provider) -> tuple[str, str] | tuple[None, None]:
    """The ``(field key, attribute)`` doubling as the scan/code input: the
    field mapped to the provider's query attribute (ISBN for books, EAN
    otherwise) — falling back to any other code attribute so mixed
    collections work either way."""
    mapped = {
        fd.key: (fd.config or {}).get('lookup_attribute')
        for fd in collection.fields.all() if (fd.config or {}).get('lookup_attribute')
    }
    for attribute in (provider.query_attribute, *lookup_providers.QUERY_ATTRIBUTES):
        for key, mapped_attribute in mapped.items():
            if mapped_attribute == attribute:
                return key, attribute
    return None, None


def _lookup_context(collection, form=None) -> dict:
    """Context for the item form's auto-fill. Lookups run through the combined
    provider of the collection's media kind (books → book databases, music →
    MusicBrainz, …) — active as soon as at least one field is mapped to a
    lookup attribute.

    There is a single top search bar (see lookup.js) that handles both text
    search (``lookup_search_url``) and code lookup (``lookup_url``); individual
    fields are no longer searchable. ``lookup_query_key`` is only used to put a
    ``data-scan`` attribute on the ISBN/EAN field so a code can still be scanned
    straight into it via scanner.js.
    """
    provider = lookup_providers.provider_for(collection)
    mapped = {
        fd.key: (fd.config or {}).get('lookup_attribute')
        for fd in collection.fields.all() if (fd.config or {}).get('lookup_attribute')
    }
    if not mapped:
        return {}
    # The single top search bar handles both text search and code (ISBN/EAN)
    # lookup; there are no more per-field search buttons. The camera scan on the
    # ISBN/EAN field stays (data-scan) so a code can still be scanned straight
    # into its field when the search yields nothing usable.
    query_key, query_attribute = (_query_key_for(collection, provider)
                                  if provider.fetch else (None, None))
    if form is not None and query_key and query_key in form.fields:
        form.fields[query_key].widget.attrs.setdefault('data-scan', query_attribute)
    return {
        'lookup_url': reverse('item_lookup', args=[collection.pk]) if provider.fetch else '',
        'lookup_search_url': reverse('item_search', args=[collection.pk]) if provider.search else '',
        'lookup_query_key': query_key or '',
        'lookup_provider_label': provider.label,
    }


def _lookup_allowed(request) -> bool:
    """Cap external-database lookups at 30/minute per user: the server makes
    outbound requests to DNB/Google/Open Library on behalf of the user, so an
    abusive client must not be able to turn it into a request cannon."""
    from accounts.throttling import allow
    return allow('lookup', str(request.user.pk), max_requests=30, window_seconds=60)


def _lookup_throttled() -> JsonResponse:
    return JsonResponse({'ok': False, 'error': _('Zu viele Anfragen. Bitte warte '
                                                 'einige Minuten und versuche es dann '
                                                 'erneut.')}, status=429)


@login_required
def item_lookup(request, pk):
    """AJAX: look up ``?q=`` in the collection's external databases and return
    the values for every field that is mapped to a provider attribute.

    Fully dynamic: the response is keyed by *this collection's* field keys, built
    from each field's ``config['lookup_attribute']`` mapping.
    """
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if not _lookup_allowed(request):
        return _lookup_throttled()
    provider = lookup_providers.provider_for(collection)
    if provider.fetch is None or not _has_lookup_mapping(collection):
        return JsonResponse({'ok': False, 'error': _('Kein Feld für die automatische '
                                                     'Befüllung zugeordnet.')}, status=400)

    query = (request.GET.get('q') or '').strip()
    if not query:
        return JsonResponse({'ok': False, 'error': _('Kein Suchbegriff übergeben.')}, status=400)

    data = provider.fetch(query)  # {attribute: value}
    fields, covers = _map_lookup_data(collection, data)

    return JsonResponse({
        'ok': True,
        'found': bool(data),
        'provider': provider.label,
        'fields': fields,
        'covers': covers,
        'duplicate': _find_duplicate(collection, provider, query,
                                     exclude=request.GET.get('exclude')),
    })


def _has_lookup_mapping(collection) -> bool:
    """True when at least one field is mapped to a lookup attribute."""
    return any((fd.config or {}).get('lookup_attribute') for fd in collection.fields.all())


def _map_lookup_data(collection, data: dict) -> tuple[dict, dict]:
    """Translate provider attributes into this collection's field keys.

    File/image fields can't be filled by JS — cover URLs are surfaced
    separately so the UI can offer a preview instead of silently dropping them.
    """
    fields, covers = {}, {}
    for fd in collection.fields.all():
        attribute = (fd.config or {}).get('lookup_attribute')
        if not attribute or attribute not in data:
            continue
        if attribute == 'cover_url' and fd.field_type in ('image', 'file'):
            covers[fd.key] = data[attribute]
        else:
            fields[fd.key] = data[attribute]
    return fields, covers


@login_required
def item_search(request, pk):
    """AJAX: free-text search (title, author, keywords) in the collection's
    external database. Returns candidate records; the client links one of them
    to the item by filling the mapped fields (incl. ISBN and cover).
    """
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if not _lookup_allowed(request):
        return _lookup_throttled()
    provider = lookup_providers.provider_for(collection)
    if provider.search is None or not _has_lookup_mapping(collection):
        return JsonResponse({'ok': False, 'error': _('Kein Feld für die automatische '
                                                     'Befüllung zugeordnet.')}, status=400)
    query = (request.GET.get('q') or '').strip()
    if not query:
        return JsonResponse({'ok': False, 'error': _('Kein Suchbegriff übergeben.')}, status=400)

    results = []
    for data in provider.search(query)[:10]:
        fields, covers = _map_lookup_data(collection, data)
        if not fields and not covers:
            continue
        label = ' · '.join(str(data[key]) for key
                           in ('title', 'authors', 'artist', 'platform', 'year')
                           if data.get(key))
        results.append({'label': label, 'cover': data.get('cover_url') or '',
                        'fields': fields, 'covers': covers})
    return JsonResponse({'ok': True, 'provider': provider.label, 'results': results})


def _find_duplicate(collection, provider, query, exclude=None):
    """Warn before a second copy is created: does an item of this collection
    already carry the scanned code in the field mapped to ISBN/EAN?
    """
    query_key, _attribute = _query_key_for(collection, provider)
    if not query_key:
        return None
    normalised = lookup_providers._digits(query) or query
    for item_id, values in collection.items.values_list('id', 'values'):
        if exclude and str(item_id) == exclude:
            continue
        value = (values or {}).get(query_key)
        if isinstance(value, str) and (lookup_providers._digits(value) or value) == normalised:
            return {
                'url': reverse('item_detail', args=[collection.pk, item_id]),
                'name': (values or {}).get('name') or str(item_id)[:8],
            }
    return None


# --- Saved views (named filter/sort states) --------------------------------------

_SAVED_VIEW_DROP_PARAMS = ('page',)


@login_required
@require_POST
def saved_view_create(request, pk):
    """Save the current filter/sort querystring under a name."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    name = (request.POST.get('name') or '').strip()[:120]
    raw = (request.POST.get('querystring') or '')[:2000]
    params = QueryDict(raw, mutable=True)
    for param in _SAVED_VIEW_DROP_PARAMS:
        params.pop(param, None)
    if not name:
        messages.error(request, _('Bitte einen Namen für die Ansicht angeben.'))
    else:
        _view, created = SavedView.objects.update_or_create(
            collection=collection, name=name,
            defaults={'querystring': params.urlencode(), 'created_by': request.user},
        )
        messages.success(request, _('Ansicht „%(name)s“ gespeichert.') % {'name': name}
                         if created else
                         _('Ansicht „%(name)s“ aktualisiert.') % {'name': name})
    target = reverse('collection_detail', args=[collection.pk])
    return redirect(f'{target}?{params.urlencode()}' if params else target)


@login_required
@require_POST
def saved_view_delete(request, pk, view_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    view = get_object_or_404(SavedView, pk=view_pk, collection=collection)
    name = view.name
    view.delete()
    messages.success(request, _('Ansicht „%(name)s“ gelöscht.') % {'name': name})
    return redirect('collection_detail', pk=collection.pk)


# --- Per-item photo gallery ------------------------------------------------------

_GALLERY_MAX_PHOTOS = 20


@login_required
@require_POST
def item_photo_add(request, pk, item_pk):
    """Attach additional photos to an item (next to the regular image field)."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    uploads = request.FILES.getlist('photos')
    existing = item.assets.filter(field_key=GALLERY_KEY).count()

    from django import forms as django_forms
    from .runtime_settings import allowed_upload_extensions, get_setting
    max_mb = get_setting('upload_max_mb')
    allowed = allowed_upload_extensions()

    added = 0
    for upload in uploads:
        if existing + added >= _GALLERY_MAX_PHOTOS:
            messages.warning(request, _('Maximal %(count)s Fotos pro Gegenstand.')
                             % {'count': _GALLERY_MAX_PHOTOS})
            break
        name = upload.name or ''
        extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        if upload.size > max_mb * 1024 * 1024:
            messages.error(request, _('„%(name)s“ ist zu groß (maximal %(mb)s MB).')
                           % {'name': name, 'mb': max_mb})
            continue
        if allowed and extension not in allowed:
            messages.error(request, _('Dateityp „%(ext)s“ ist nicht erlaubt.')
                           % {'ext': extension or '?'})
            continue
        try:  # real image check (same validation the image form field uses)
            django_forms.ImageField().clean(upload)
        except django_forms.ValidationError:
            messages.error(request, _('„%(name)s“ ist kein gültiges Bild.') % {'name': name})
            continue
        ItemAsset.objects.create(item=item, field_key=GALLERY_KEY,
                                 file=upload, original_name=name[:255])
        added += 1
    if added:
        messages.success(request, _('%(count)s Foto(s) hinzugefügt.') % {'count': added})
    elif not uploads:
        messages.error(request, _('Keine Dateien ausgewählt.'))
    return redirect('item_detail', pk=collection.pk, item_pk=item.pk)


@login_required
@require_POST
def item_photo_delete(request, pk, item_pk, asset_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    asset = get_object_or_404(ItemAsset, pk=asset_pk, item=item, field_key=GALLERY_KEY)
    asset.file.delete(save=False)
    asset.delete()
    messages.success(request, _('Foto entfernt.'))
    return redirect('item_detail', pk=collection.pk, item_pk=item.pk)


# --- Item CRUD (dynamic forms) -------------------------------------------------

@login_required
def item_create(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    if request.method == 'POST':
        form = DynamicItemForm(request.POST, request.FILES, collection=collection)
        if form.is_valid():
            item = form.save(user=request.user)
            # "Speichern & nächster": stay in fast capture mode for an inventory.
            # Pre-fill the just-used "Art" so a batch of the same kind is quick.
            if 'save_and_new' in request.POST:
                messages.success(request, _('Gegenstand hinzugefügt. Weiter mit dem nächsten.'))
                url = reverse('item_create', args=[collection.pk])
                if item.item_type_id:
                    url += f'?item_type={item.item_type_id}'
                return redirect(url)
            messages.success(request, _('Gegenstand hinzugefügt.'))
            return redirect('collection_detail', pk=collection.pk)
    else:
        initial = {}
        preset_type = request.GET.get('item_type')
        if preset_type:
            initial['__item_type'] = preset_type
        form = DynamicItemForm(collection=collection, initial=initial or None)
    return render(request, 'collections/item_form.html',
                  {'form': form, 'collection': collection, 'title': _('Neuer Gegenstand'),
                   'save_and_new': True,
                   **_lookup_context(collection, form)})


@login_required
def item_edit(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    if request.method == 'POST':
        form = DynamicItemForm(request.POST, request.FILES, collection=collection, instance=item)
        if form.is_valid():
            form.save(user=request.user)
            messages.success(request, _('Gegenstand aktualisiert.'))
            return redirect('item_detail', pk=collection.pk, item_pk=item.pk)
    else:
        form = DynamicItemForm(collection=collection, instance=item)
    return render(request, 'collections/item_form.html',
                  {'form': form, 'collection': collection, 'title': _('Gegenstand bearbeiten'),
                   'lookup_exclude': item.pk, **_lookup_context(collection, form)})


@login_required
@require_POST
def item_inline_update(request, pk, item_pk):
    """AJAX: edit a single field of one item directly from the items table.

    Validates/coerces the posted value through the same per-type form field the
    full item form uses (``build_form_field``) and writes it into the item's
    JSON ``values``. File/image and multi-select fields are excluded (they need
    the full form). Returns the re-rendered display value for the cell.
    """
    from django import forms as dj_forms
    from .dynamic_forms import build_form_field, _to_jsonable
    from .rendering import render_cell

    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    key = request.POST.get('field_key') or ''
    fd = next((f for f in collection.fields.all() if f.key == key), None)
    if fd is None:
        return JsonResponse({'ok': False, 'error': _('Unbekanntes Feld.')}, status=400)
    if fd.field_type in INLINE_NONEDITABLE_TYPES:
        return JsonResponse({'ok': False, 'error': _('Dieses Feld kann nur im Formular '
                                                     'bearbeitet werden.')}, status=400)

    field = build_form_field(fd, required=False)
    raw = request.POST.get('value', '')
    try:
        cleaned = field.clean(raw)
    except dj_forms.ValidationError as exc:
        return JsonResponse({'ok': False, 'error': ' '.join(exc.messages)}, status=400)

    values = dict(item.values or {})
    if cleaned in (None, '', []):
        values.pop(key, None)
    else:
        values[key] = _to_jsonable(cleaned)
    item.values = values
    item.save(update_fields=['values', 'updated_at'])

    cell = render_cell(fd, values.get(key))
    return JsonResponse({'ok': True, 'display': cell.value or '–',
                         'empty': cell.kind == 'empty'})


# --- Loans ---------------------------------------------------------------------

@login_required
@require_POST
def item_lend(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    borrower = (request.POST.get('borrower') or '').strip()
    due_at = None
    try:
        due_at = date.fromisoformat((request.POST.get('due_at') or '').strip())
    except ValueError:
        pass
    if not borrower:
        messages.error(request, _('Bitte angeben, an wen verliehen wird.'))
    elif item.active_loan:
        messages.error(request, _('Dieser Gegenstand ist bereits verliehen.'))
    else:
        loan = Loan.objects.create(
            item=item, borrower=borrower[:120], due_at=due_at,
            borrower_contact=(request.POST.get('borrower_contact') or '').strip()[:300],
            note=(request.POST.get('note') or '').strip()[:255],
            created_by=request.user)
        # "Leihvertrag erstellen" ticked → jump straight to the printable PDF.
        if request.POST.get('make_agreement'):
            messages.success(request, _('Als verliehen markiert. Leihvertrag wird geöffnet.'))
            return redirect('item_loan_agreement', pk=collection.pk, item_pk=item.pk,
                            loan_pk=loan.pk)
        messages.success(request, _('Als verliehen markiert.'))
    return redirect('item_detail', pk=collection.pk, item_pk=item.pk)


@login_required
def item_loan_agreement(request, pk, item_pk, loan_pk):
    """Printable loan agreement (Leihvertrag) PDF for one loan."""
    collection = _get_collection_for(request.user, pk)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    loan = get_object_or_404(Loan, pk=loan_pk, item=item)
    from . import contracts
    pdf = contracts.build_loan_agreement_pdf(collection, item, loan)
    disposition = 'attachment' if request.GET.get('download') else 'inline'
    filename = f'leihvertrag-{codes.item_short_code(item)}.pdf'
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return response


@login_required
@require_POST
def item_return(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    loan = item.active_loan
    if loan:
        loan.returned_at = timezone.localdate()
        loan.save(update_fields=['returned_at'])
        messages.success(request, _('Rückgabe vermerkt.'))
    return redirect('item_detail', pk=collection.pk, item_pk=item.pk)


@login_required
def collection_loans(request, pk):
    """Overview of open loans (plus recent history) of one collection."""
    collection = _get_collection_for(request.user, pk)
    loans = (Loan.objects.filter(item__collection=collection, item__deleted_at__isnull=True)
             .select_related('item', 'item__item_type'))
    permission = collection.user_permission(request.user)
    return render(request, 'collections/loans.html', {
        'collection': collection,
        'open_loans': [loan for loan in loans if loan.returned_at is None],
        'closed_loans': [loan for loan in loans if loan.returned_at is not None][:50],
        'can_edit': permission in ('owner', 'edit'),
    })


# --- Import ----------------------------------------------------------------------

@login_required
def collection_import(request, pk):
    """Upload an .xlsx/.csv (headers = field labels, as produced by the export)
    and create one item per row."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    result = None
    if request.method == 'POST':
        upload = request.FILES.get('file')
        max_mb = get_setting('upload_max_mb')
        if not upload or not upload.name.lower().endswith(('.xlsx', '.csv')):
            messages.error(request, _('Bitte eine .xlsx- oder .csv-Datei auswählen.'))
        elif upload.size > max_mb * 1024 * 1024:
            messages.error(request, _('Datei ist zu groß (maximal %(mb)s MB).') % {'mb': max_mb})
        else:
            try:
                rows = imports.read_rows(upload)
            except Exception:
                rows = []
            if len(rows) < 2:
                messages.error(request, _('Die Datei enthält keine Datenzeilen oder konnte nicht gelesen werden.'))
            else:
                result = imports.import_table(collection, rows, user=request.user)
                messages.success(request, _('%(count)s Gegenstände importiert.')
                                 % {'count': result['created']})
    return render(request, 'collections/import.html',
                  {'collection': collection, 'result': result,
                   'fields': collection.fields.all()})


@login_required
def item_find(request, pk):
    """Scan-to-find: resolve a scanned code to an item of this collection.

    Accepts (in this order) an item-QR payload (the item detail URL), the label
    barcode (``item_short_code`` = first 12 hex chars of the id) or any stored
    string value such as an ISBN (digit-normalised so 978-… matches 978…).
    """
    collection = _get_collection_for(request.user, pk)
    code = (request.GET.get('code') or '').strip()
    if not code:
        messages.error(request, _('Kein Code übergeben.'))
        return redirect('collection_detail', pk=collection.pk)

    match = re.search(r'/items/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                      code.lower())
    if match and collection.items.filter(pk=match.group(1)).exists():
        return redirect('item_detail', pk=collection.pk, item_pk=match.group(1))

    if re.fullmatch(r'[0-9a-f]{12}', code.lower()):
        for item_id in collection.items.values_list('id', flat=True):
            if item_id.hex[:12] == code.lower():
                return redirect('item_detail', pk=collection.pk, item_pk=item_id)

    code_digits = lookup_providers._digits(code)
    matches = []
    for item_id, values in collection.items.values_list('id', 'values'):
        for value in (values or {}).values():
            if isinstance(value, str) and value and (
                    value == code or (code_digits and lookup_providers._digits(value) == code_digits)):
                matches.append(item_id)
                break
    if len(matches) == 1:
        return redirect('item_detail', pk=collection.pk, item_pk=matches[0])
    if matches:
        messages.info(request, _('Mehrere Treffer für „%(code)s“ – Liste wurde gefiltert.') % {'code': code})
        return redirect(reverse('collection_detail', args=[collection.pk]) + '?' + urlencode({'q': code}))
    messages.warning(request, _('Kein Gegenstand mit Code „%(code)s“ gefunden.') % {'code': code})
    return redirect('collection_detail', pk=collection.pk)


@login_required
def item_detail(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk)
    item = get_object_or_404(Item.objects.select_related('item_type'), pk=item_pk, collection=collection)
    fields = list(collection.fields.all())
    pairs = list(zip(fields, item_row(item, fields)))
    permission = collection.user_permission(request.user)
    # Leaf through the collection in list order (newest first): "previous" is
    # the next-newer item, "next" the next-older one.
    newer = (collection.items.filter(created_at__gt=item.created_at)
             .order_by('created_at').first())
    older = (collection.items.filter(created_at__lt=item.created_at)
             .order_by('-created_at').first())
    return render(request, 'collections/item_detail.html', {
        'collection': collection, 'item': item, 'pairs': pairs,
        'can_edit': permission in ('owner', 'edit'),
        'loan': item.active_loan,
        'past_loans': item.loans.filter(returned_at__isnull=False)[:10],
        'newer_item': newer,
        'older_item': older,
        'today': timezone.localdate(),
        'gallery': item.assets.filter(field_key=GALLERY_KEY).order_by('uploaded_at'),
    })


@login_required
@require_POST
def items_bulk(request, pk):
    """Bulk actions on selected items: delete them or (re)assign their "Art"."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    ids = []
    for raw in request.POST.getlist('items'):
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    items = collection.items.filter(pk__in=ids)
    count = items.count()
    action = request.POST.get('action')

    if not count:
        messages.info(request, _('Keine Gegenstände ausgewählt.'))
    elif action == 'delete':
        items.update(deleted_at=timezone.now())
        messages.success(request, _('%(count)s Gegenstände in den Papierkorb verschoben.')
                         % {'count': count})
    elif action == 'set_type':
        type_id = (request.POST.get('item_type') or '').strip()
        item_type = (get_object_or_404(ItemType, pk=type_id, collection=collection)
                     if type_id else None)
        items.update(item_type=item_type)
        if item_type:
            messages.success(request, _('Art „%(type)s“ für %(count)s Gegenstände gesetzt.')
                             % {'type': item_type.name, 'count': count})
        else:
            messages.success(request, _('Art bei %(count)s Gegenständen entfernt.') % {'count': count})
    else:
        messages.error(request, _('Unbekannte Aktion.'))
    return redirect('collection_detail', pk=collection.pk)


@login_required
@require_POST
def item_duplicate(request, pk, item_pk):
    """Create a copy of an item (values, type and uploaded files) and jump into
    editing the copy — the quick way to catalogue several similar objects."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)

    file_keys = {fd.key for fd in collection.fields.all() if fd.field_type in ('image', 'file')}
    values = {k: v for k, v in (item.values or {}).items() if k not in file_keys}
    copy = Item.objects.create(collection=collection, item_type=item.item_type,
                               values=values, created_by=request.user)

    copied_assets = False
    for asset in item.assets.all():
        try:
            with asset.file.open('rb') as fh:
                content = fh.read()
        except (OSError, ValueError):
            continue  # source file missing on disk: skip, keep the rest
        new_asset = ItemAsset.objects.create(
            item=copy, field_key=asset.field_key,
            file=ContentFile(content, name=os.path.basename(asset.file.name)),
            original_name=asset.original_name,
        )
        # Gallery photos have no field/value entry — only real file fields
        # store an asset reference in the JSON values.
        if asset.field_key != GALLERY_KEY:
            copy.values[asset.field_key] = {'asset_id': str(new_asset.id),
                                            'name': new_asset.original_name,
                                            'url': new_asset.file.url}
        copied_assets = True
    if copied_assets:
        copy.save(update_fields=['values', 'updated_at'])

    messages.success(request, _('Gegenstand dupliziert — du bearbeitest jetzt die Kopie.'))
    return redirect('item_edit', pk=collection.pk, item_pk=copy.pk)


@login_required
def item_delete(request, pk, item_pk):
    """Move an item to the collection's trash (soft delete, restorable)."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    if request.method == 'POST':
        item.soft_delete()
        messages.success(request, _('Gegenstand in den Papierkorb verschoben.'))
        return redirect('collection_detail', pk=collection.pk)
    return render(request, 'collections/confirm_delete.html', {
        'collection': collection, 'object_label': _('Gegenstand „%(item)s“') % {'item': item},
        'warning': _('Der Gegenstand wandert in den Papierkorb und wird nach Ablauf der '
                     'Aufbewahrungsfrist endgültig gelöscht.'),
    })


# --- Trash (soft-deleted items) --------------------------------------------------

def _purge_expired_trash(collection) -> int:
    """Really delete trashed items older than the configured retention."""
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=get_setting('trash_retention_days'))
    expired = Item.all_objects.filter(collection=collection, deleted_at__lt=cutoff)
    count = 0
    for item in expired:
        item.purge()
        count += 1
    return count


@login_required
def collection_trash(request, pk):
    """List the collection's trashed items with restore / purge actions."""
    collection = _get_collection_for(request.user, pk, need_edit=True)
    _purge_expired_trash(collection)  # opportunistic retention cleanup
    items = list(Item.all_objects.filter(collection=collection, deleted_at__isnull=False)
                 .select_related('item_type').order_by('-deleted_at'))
    return render(request, 'collections/trash.html', {
        'collection': collection,
        'items': items,
        'retention_days': get_setting('trash_retention_days'),
    })


@login_required
@require_POST
def item_restore(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item.all_objects, pk=item_pk, collection=collection,
                             deleted_at__isnull=False)
    item.restore()
    messages.success(request, _('Gegenstand „%(item)s“ wiederhergestellt.') % {'item': item})
    return redirect('collection_trash', pk=collection.pk)


@login_required
@require_POST
def item_purge(request, pk, item_pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    item = get_object_or_404(Item.all_objects, pk=item_pk, collection=collection,
                             deleted_at__isnull=False)
    name = str(item)
    item.purge()
    messages.success(request, _('Gegenstand „%(item)s“ endgültig gelöscht.') % {'item': name})
    return redirect('collection_trash', pk=collection.pk)


@login_required
@require_POST
def trash_empty(request, pk):
    collection = _get_collection_for(request.user, pk, need_edit=True)
    items = list(Item.all_objects.filter(collection=collection, deleted_at__isnull=False))
    for item in items:
        item.purge()
    messages.success(request, _('Papierkorb geleert (%(count)s Gegenstände).')
                     % {'count': len(items)})
    return redirect('collection_trash', pk=collection.pk)


# --- Price comparison (link-out search on external platforms) -------------------

def _live_offers(request, query):
    """Real inline offers for the price search — only when the ``live_offers_enabled``
    runtime setting is on and at least one provider is configured. Cached (30 min)
    and per-user throttled, because unlike the link-out cards this makes outbound
    requests. Returns a list (possibly empty) when active, else ``None`` (feature
    off / no provider) so the template can tell the two apart."""
    if not get_setting('live_offers_enabled') or not query.has_query():
        return None
    from .offer_providers import active_providers, fetch_offers
    if not active_providers(query):
        return None
    from accounts.throttling import allow
    if not allow('offers', str(request.user.pk), max_requests=20, window_seconds=60):
        return None

    import hashlib
    from django.core.cache import cache
    digest = hashlib.md5(f'{query.best_text}|{query.kind}'.lower().encode()).hexdigest()
    cache_key = f'offers:{digest}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    offers = fetch_offers(query)
    cache.set(cache_key, offers, 30 * 60)
    return offers


@login_required
def price_search_page(request):
    """Multi-platform shopping/price search.

    Two layers: (1) pre-filled deep *links* into many platforms (no server-side
    contact — see price_search.py); (2) optional live inline offers fetched from
    providers with an official API (see offer_providers.py), off by default."""
    form = price_search.PriceSearchForm(request.GET or None)
    query = form.to_query()
    return render(request, 'collections/price_search.html', {
        'form': form,
        'query': query,
        'links': price_search.build_links(query),
        'offers': _live_offers(request, query),
        'live_offers_enabled': get_setting('live_offers_enabled'),
    })


@login_required
def item_price_search(request, pk, item_pk):
    """Open the price search pre-filled with an item's code/title/creator."""
    collection = _get_collection_for(request.user, pk)  # viewing suffices
    item = get_object_or_404(Item, pk=item_pk, collection=collection)
    query = price_search.query_for_item(collection, item)
    params = {'q': query.q, 'code': query.code, 'kind': query.kind}
    return redirect(reverse('price_search') + '?' + urlencode({k: v for k, v in params.items() if v}))


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


# --- Full backup (ZIP: Excel + JSON + media) -------------------------------------

@login_required
def collection_backup(request, pk):
    """Download a complete backup of the collection as one ZIP file:
    the Excel table, a machine-readable JSON dump (incl. trash) and every
    uploaded image/file. Independent of active filters."""
    collection = _get_collection_for(request.user, pk)
    from accounts.throttling import allow
    if not allow('backup', str(request.user.pk), max_requests=10, window_seconds=3600):
        return HttpResponse(_('Zu viele Sicherungs-Anfragen. Bitte versuche es später erneut.'),
                            status=429, content_type='text/plain; charset=utf-8')
    buffer = export.build_backup_zip(collection, request.build_absolute_uri)
    filename = (f"sicherung-{slugify(collection.name) or 'sammlung'}-"
                f"{timezone.localdate().isoformat()}.zip")
    return FileResponse(buffer, as_attachment=True, filename=filename,
                        content_type='application/zip')


# --- Restore from backup ZIP -----------------------------------------------------

@login_required
def collection_restore(request):
    """Recreate a collection from a backup ZIP (see collection_backup) as a
    NEW collection owned by the current user — structure, contents, files,
    loan history and saved views; shares are deliberately not restored."""
    if request.method == 'POST':
        from accounts.throttling import allow
        if not allow('restore', str(request.user.pk), max_requests=5, window_seconds=3600):
            return HttpResponse(_('Zu viele Wiederherstellungen. Bitte versuche es später erneut.'),
                                status=429, content_type='text/plain; charset=utf-8')
        upload = request.FILES.get('file')
        if not upload or not upload.name.lower().endswith('.zip'):
            messages.error(request, _('Bitte eine .zip-Sicherungsdatei auswählen.'))
        elif upload.size > restore.MAX_TOTAL_BYTES:
            messages.error(request, _('Die Datei ist zu groß.'))
        else:
            try:
                collection, stats = restore.restore_backup(upload, request.user)
            except restore.RestoreError as exc:
                messages.error(request, str(exc))
            else:
                for warning in stats['warnings'][:10]:
                    messages.warning(request, warning)
                messages.success(
                    request,
                    _('Sammlung „%(name)s“ wiederhergestellt: %(items)s Gegenstände, '
                      '%(files)s Dateien.')
                    % {'name': collection.name, 'items': stats['items'],
                       'files': stats['files']})
                return redirect('collection_detail', pk=collection.pk)
    return render(request, 'collections/restore.html')


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
