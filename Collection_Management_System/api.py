"""Token-authenticated JSON API (v1) — see API.md for examples.

Deliberately hand-rolled (no DRF): the schema is dynamic (FieldDefinition +
JSON values), so items are exchanged as plain ``{"values": {field_key: value}}``
mappings and validated through the same ``DynamicItemForm`` the web UI uses —
one validation path for both worlds, no serializer layer to keep in sync.

Access control:
  * master switch: runtime setting ``api_enabled`` (off by default)
  * authentication: ``Authorization: Bearer <token>`` (or ``X-Api-Key``);
    tokens are created on the profile page (``accounts.ApiToken``)
  * authorization: the same row-level rules as the UI
    (``Collection.user_permission`` — owner/edit/view)

Token requests carry no session, so CSRF does not apply (``csrf_exempt``).
"""

from __future__ import annotations

import json
from functools import wraps

from django.core.paginator import Paginator
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import gettext as _

from accounts.models import ApiToken

from .dynamic_forms import ITEM_TYPE_KEY, DynamicItemForm
from .filters import ItemFilterForm
from .models import Collection, Item
from .runtime_settings import get_setting
from .services import collections_for_user


def _error(message: str, status: int, **extra) -> JsonResponse:
    return JsonResponse({'error': message, **extra}, status=status)


def api_view(func):
    """Gate an API endpoint: master switch, token auth, JSON errors."""

    @csrf_exempt
    @wraps(func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not get_setting('api_enabled'):
            return _error(_('Die API ist deaktiviert.'), 403)
        auth = request.headers.get('Authorization', '')
        key = (auth[len('Bearer '):].strip() if auth.startswith('Bearer ')
               else request.headers.get('X-Api-Key', '').strip())
        if not key:
            return _error(_('API-Token fehlt (Authorization: Bearer <token>).'), 401)
        token = (ApiToken.objects.select_related('user')
                 .filter(key=key, user__is_active=True).first())
        if token is None:
            return _error(_('Ungültiger API-Token.'), 401)
        token.touch()
        request.user = token.user
        return func(request, *args, **kwargs)

    return wrapper


def _get_collection(request, pk, *, need_edit=False) -> tuple[Collection | None, JsonResponse | None]:
    collection = Collection.objects.filter(pk=pk).first()
    if collection is None:
        return None, _error(_('Sammlung nicht gefunden.'), 404)
    permission = collection.user_permission(request.user)
    if permission is None:
        return None, _error(_('Kein Zugriff auf diese Sammlung.'), 403)
    if need_edit and permission not in ('owner', 'edit'):
        return None, _error(_('Keine Bearbeitungsrechte für diese Sammlung.'), 403)
    return collection, None


def _json_body(request) -> tuple[dict | None, JsonResponse | None]:
    try:
        payload = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return None, _error(_('Ungültiges JSON im Request-Body.'), 400)
    if not isinstance(payload, dict):
        return None, _error(_('Der Request-Body muss ein JSON-Objekt sein.'), 400)
    return payload, None


def _item_data(item: Item) -> dict:
    return {
        'id': str(item.pk),
        'item_type': ({'id': item.item_type.pk, 'name': item.item_type.name}
                      if item.item_type else None),
        'values': item.values,
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
    }


def _save_item(request, collection, payload, instance=None, merge=False):
    """Create/update an item through DynamicItemForm (single validation path)."""
    values = payload.get('values') or {}
    if not isinstance(values, dict):
        return _error(_('„values“ muss ein Objekt {feld_key: wert} sein.'), 400)
    if merge and instance is not None:
        values = {**(instance.values or {}), **values}
    data = dict(values)
    if collection.item_types.exists():
        if 'item_type' in payload:
            data[ITEM_TYPE_KEY] = payload.get('item_type') or ''
        elif instance is not None:
            data[ITEM_TYPE_KEY] = instance.item_type_id or ''
    form = DynamicItemForm(data, collection=collection, instance=instance)
    if not form.is_valid():
        return _error(_('Validierung fehlgeschlagen.'), 400, fields=form.errors)
    item = form.save(user=request.user)
    return JsonResponse(_item_data(item), status=201 if instance is None else 200)


@api_view
def api_collections(request):
    """GET /api/collections/ — all collections the token's user may access."""
    if request.method != 'GET':
        return _error(_('Methode nicht erlaubt.'), 405)
    results = []
    for collection in collections_for_user(request.user):
        results.append({
            'id': str(collection.pk),
            'name': collection.name,
            'description': collection.description,
            'permission': collection.user_permission(request.user),
            'item_count': collection.items.count(),
        })
    return JsonResponse({'results': results})


@api_view
def api_collection_detail(request, pk):
    """GET /api/collections/<id>/ — schema: fields and item types."""
    if request.method != 'GET':
        return _error(_('Methode nicht erlaubt.'), 405)
    collection, error = _get_collection(request, pk)
    if error:
        return error
    return JsonResponse({
        'id': str(collection.pk),
        'name': collection.name,
        'description': collection.description,
        'permission': collection.user_permission(request.user),
        'fields': [
            {'key': f.key, 'label': f.label, 'type': f.field_type,
             'required': f.required, 'config': f.config}
            for f in collection.fields.all()
        ],
        'item_types': [
            {'id': t.pk, 'name': t.name} for t in collection.item_types.all()
        ],
    })


@api_view
def api_items(request, pk):
    """GET (list, supports the UI's filter params + ?page=) / POST (create)."""
    collection, error = _get_collection(request, pk, need_edit=(request.method == 'POST'))
    if error:
        return error

    if request.method == 'GET':
        filter_form = ItemFilterForm(request.GET or None, collection=collection)
        items = filter_form.apply(collection.items.select_related('item_type'))
        paginator = Paginator(items, get_setting('items_per_page'))
        page = paginator.get_page(request.GET.get('page'))
        return JsonResponse({
            'count': paginator.count,
            'page': page.number,
            'pages': paginator.num_pages,
            'results': [_item_data(item) for item in page],
        })

    if request.method == 'POST':
        payload, error = _json_body(request)
        if error:
            return error
        return _save_item(request, collection, payload)

    return _error(_('Methode nicht erlaubt.'), 405)


@api_view
def api_item(request, pk, item_pk):
    """GET / PUT (replace values) / PATCH (merge values) / DELETE (to trash)."""
    need_edit = request.method != 'GET'
    collection, error = _get_collection(request, pk, need_edit=need_edit)
    if error:
        return error
    item = Item.objects.filter(pk=item_pk, collection=collection).first()
    if item is None:
        return _error(_('Gegenstand nicht gefunden.'), 404)

    if request.method == 'GET':
        return JsonResponse(_item_data(item))
    if request.method in ('PUT', 'PATCH'):
        payload, error = _json_body(request)
        if error:
            return error
        return _save_item(request, collection, payload, instance=item,
                          merge=(request.method == 'PATCH'))
    if request.method == 'DELETE':
        item.soft_delete()
        return JsonResponse({'ok': True, 'trashed': str(item.pk)})
    return _error(_('Methode nicht erlaubt.'), 405)
