"""Restore a collection from a backup ZIP (counterpart to export.build_backup_zip).

Reads ``sammlung.json`` plus the ``medien/…`` members of a backup archive and
recreates the collection — structure (fields incl. configuration, item types,
saved views), contents (items incl. trash state and loan history) and every
uploaded file — as a NEW collection owned by the importing user.

Deliberately NOT restored: shares. Access grants are personal to the original
owner; the restoring user re-shares explicitly if wanted.

Safety: the archive is never extracted to disk (no zip-slip); member sizes and
counts are capped before reading (zip-bomb guard); every field/value is
validated against the schema before it is written; everything runs in one
transaction, so a broken archive leaves nothing behind.
"""

from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from datetime import date, datetime

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext as _

from .lookup_providers import VALID_ATTRIBUTES, VALID_MEDIA_KINDS
from .models import (GALLERY_KEY, Collection, FieldDefinition, FieldType, Item,
                     ItemAsset, ItemType, Loan, SavedView)

# Zip-bomb guards.
MAX_MEMBERS = 5000
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_MEMBER_BYTES = 200 * 1024 * 1024  # single files are read into memory
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

FILE_FIELD_TYPES = {FieldType.IMAGE, FieldType.FILE}


class RestoreError(Exception):
    """Raised with a user-readable message when the archive is unusable."""


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise RestoreError(_('Das Archiv enthält zu viele Dateien.'))
    total = 0
    for info in infos:
        if info.file_size > MAX_MEMBER_BYTES:
            raise RestoreError(_('Eine Datei im Archiv ist zu groß.'))
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise RestoreError(_('Das Archiv ist entpackt zu groß.'))


def _load_manifest(archive: zipfile.ZipFile) -> dict:
    try:
        info = archive.getinfo('sammlung.json')
    except KeyError:
        raise RestoreError(_('Keine gültige Sicherung: „sammlung.json“ fehlt im Archiv.'))
    if info.file_size > MAX_JSON_BYTES:
        raise RestoreError(_('Die Sicherungsdaten sind zu groß.'))
    try:
        data = json.loads(archive.read('sammlung.json').decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise RestoreError(_('„sammlung.json“ ist beschädigt oder kein gültiges JSON.'))
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise RestoreError(_('„sammlung.json“ hat nicht das erwartete Format.'))
    return data


def _parse_datetime(raw):
    try:
        return datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _parse_date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _clean_config(field_type: str, config) -> dict:
    """Keep only understood, well-typed configuration entries."""
    if not isinstance(config, dict):
        return {}
    cleaned: dict = {}
    choices = config.get('choices')
    if isinstance(choices, list):
        cleaned['choices'] = [str(c)[:200] for c in choices[:200]]
    attribute = config.get('lookup_attribute')
    if isinstance(attribute, str) and attribute in VALID_ATTRIBUTES:
        cleaned['lookup_attribute'] = attribute
    currency = config.get('currency')
    if isinstance(currency, str) and 0 < len(currency) <= 3:
        cleaned['currency'] = currency
    return cleaned


_KEY_RE = re.compile(r'^[-a-zA-Z0-9_]+$')


def _restore_fields(collection: Collection, raw_fields) -> dict[str, FieldDefinition]:
    fields: dict[str, FieldDefinition] = {}
    valid_types = set(FieldType.values)
    for order, entry in enumerate(raw_fields or []):
        if not isinstance(entry, dict):
            continue
        # Keep the original key verbatim when it is a valid slug (values are
        # keyed by it!); otherwise sanitise. Leading '_' stays reserved.
        raw_key = str(entry.get('key') or '')[:80]
        key = (raw_key if _KEY_RE.match(raw_key) else slugify(raw_key)[:80]).lstrip('_')
        field_type = entry.get('type')
        if not key or key in fields or field_type not in valid_types:
            continue
        fields[key] = FieldDefinition.objects.create(
            collection=collection, key=key,
            label=str(entry.get('label') or key)[:150],
            field_type=field_type,
            required=bool(entry.get('required')),
            order=order,
            config=_clean_config(field_type, entry.get('config')),
        )
    return fields


def _media_member(archive: zipfile.ZipFile, names: set[str], old_item_id: str,
                  stored_path: str) -> str | None:
    """Find the archive member for one file reference (see build_backup_zip)."""
    try:
        short = uuid.UUID(str(old_item_id)).hex[:12]
    except (ValueError, AttributeError, TypeError):
        return None
    base = os.path.basename(str(stored_path) or '')
    if not base:
        return None
    exact = f'medien/{short}/{base}'
    if exact in names:
        return exact
    # Collision-renamed variant ('<assethex8>-<base>').
    suffix = f'-{base}'
    for name in names:
        if name.startswith(f'medien/{short}/') and name.endswith(suffix):
            return name
    return None


def restore_backup(uploaded_file, owner) -> tuple[Collection, dict]:
    """Create a new collection for ``owner`` from a backup ZIP.

    Returns ``(collection, stats)`` with ``stats = {'items': n, 'files': n,
    'warnings': [...]}``. Raises :class:`RestoreError` for unusable archives.
    """
    try:
        archive = zipfile.ZipFile(uploaded_file)
    except (zipfile.BadZipFile, OSError):
        raise RestoreError(_('Die Datei ist kein gültiges ZIP-Archiv.'))
    with archive:
        _validate_archive(archive)
        data = _load_manifest(archive)
        member_names = set(archive.namelist())
        warnings: list[str] = []

        with transaction.atomic():
            kind = data.get('media_kind')
            collection = Collection.objects.create(
                owner=owner,
                name=str(data.get('name') or _('Wiederhergestellte Sammlung'))[:200],
                description=str(data.get('description') or ''),
                lookup_provider=kind if kind in VALID_MEDIA_KINDS else '',
            )
            fields = _restore_fields(collection, data.get('fields'))

            types: dict[str, ItemType] = {}
            for order, entry in enumerate(data.get('item_types') or []):
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '').strip()[:120]
                if name and name not in types:
                    types[name] = ItemType.objects.create(
                        collection=collection, name=name,
                        description=str(entry.get('description') or ''), order=order)

            # Per-"Art" required-field mapping ('required_for' on each field).
            for entry in data.get('fields') or []:
                if not isinstance(entry, dict):
                    continue
                field = fields.get(str(entry.get('key') or ''))
                wanted = entry.get('required_for')
                if field and isinstance(wanted, list):
                    matching = [types[str(n)] for n in wanted if str(n) in types]
                    if matching:
                        field.required_for_types.set(matching)

            for entry in data.get('saved_views') or []:
                if isinstance(entry, dict) and str(entry.get('name') or '').strip():
                    SavedView.objects.get_or_create(
                        collection=collection,
                        name=str(entry['name']).strip()[:120],
                        defaults={'querystring': str(entry.get('querystring') or '')[:2000],
                                  'created_by': owner})

            item_count = file_count = 0
            for entry in data.get('items') or []:
                if not isinstance(entry, dict):
                    continue
                raw_values = entry.get('values')
                raw_values = raw_values if isinstance(raw_values, dict) else {}
                # Keep only values whose field exists; file fields are rebuilt
                # from the restored assets below.
                values = {
                    key: value for key, value in raw_values.items()
                    if key in fields and fields[key].field_type not in FILE_FIELD_TYPES
                }
                item = Item.objects.create(
                    collection=collection,
                    item_type=types.get(str(entry.get('type') or '')),
                    values=values,
                    created_by=owner,
                    deleted_at=_parse_datetime(entry.get('deleted_at')),
                )
                item_count += 1
                values_changed = False

                for ref in entry.get('files') or []:
                    if not isinstance(ref, dict):
                        continue
                    field_key = str(ref.get('field') or '')
                    is_gallery = field_key == GALLERY_KEY
                    if not is_gallery and (
                            field_key not in fields
                            or fields[field_key].field_type not in FILE_FIELD_TYPES):
                        continue
                    member = _media_member(archive, member_names,
                                           entry.get('id'), ref.get('path'))
                    if not member:
                        warnings.append(_('Datei „%(name)s“ fehlt im Archiv.')
                                        % {'name': ref.get('name') or ref.get('path')})
                        continue
                    base = os.path.basename(member)
                    asset = ItemAsset.objects.create(
                        item=item, field_key=field_key,
                        file=ContentFile(archive.read(member), name=base),
                        original_name=str(ref.get('name') or base)[:255],
                    )
                    file_count += 1
                    if not is_gallery:
                        item.values[field_key] = {'asset_id': str(asset.id),
                                                  'name': asset.original_name,
                                                  'url': asset.file.url}
                        values_changed = True
                if values_changed:
                    item.save(update_fields=['values', 'updated_at'])

                for loan in entry.get('loans') or []:
                    if not isinstance(loan, dict):
                        continue
                    borrower = str(loan.get('borrower') or '').strip()[:120]
                    lent_at = _parse_date(loan.get('lent_at'))
                    if not borrower or not lent_at:
                        continue
                    Loan.objects.create(
                        item=item, borrower=borrower, lent_at=lent_at,
                        due_at=_parse_date(loan.get('due_at')),
                        returned_at=_parse_date(loan.get('returned_at')),
                        note=str(loan.get('note') or '')[:255],
                        created_by=owner,
                    )

    return collection, {'items': item_count, 'files': file_count, 'warnings': warnings}
