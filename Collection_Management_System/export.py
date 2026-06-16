"""Dynamic Excel (.xlsx) export of a collection.

Columns are built from the collection's FieldDefinitions (so the export is as
dynamic as the data model), and the rows are whatever items are passed in — the
view feeds it the *filtered* queryset, so "export" honours the active filters.
"""

from __future__ import annotations

import re
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .codes import item_short_code
from .models import FieldType

_INVALID_SHEET = re.compile(r'[\\/?*\[\]:]')
NUMERIC_TYPES = {FieldType.NUMBER, FieldType.YEAR, FieldType.DECIMAL, FieldType.PRICE}


def _cell_value(field, raw, build_uri):
    """Convert a stored value into something Excel should display."""
    if raw in (None, '', [], {}):
        return ''
    t = field.field_type
    if t in (FieldType.IMAGE, FieldType.FILE) and isinstance(raw, dict):
        url = raw.get('url', '')
        return build_uri(url) if url else raw.get('name', '')
    if t == FieldType.BOOLEAN:
        return 'Ja' if raw else 'Nein'
    if t == FieldType.MULTICHOICE and isinstance(raw, list):
        return ', '.join(str(v) for v in raw)
    if t in NUMERIC_TYPES:
        try:
            return float(raw) if t in (FieldType.DECIMAL, FieldType.PRICE) else int(raw)
        except (TypeError, ValueError):
            return str(raw)
    return str(raw)


def build_workbook(collection, items, fields, build_uri) -> bytes:
    """Return xlsx bytes: one column per field, one row per item."""
    wb = Workbook()
    ws = wb.active
    ws.title = (_INVALID_SHEET.sub('', collection.name) or 'Export')[:31]

    headers = ['ID', 'Art'] + [f.label for f in fields]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = 'A2'

    for item in items:
        values = item.values or {}
        row = [item_short_code(item), str(item.item_type) if item.item_type else '']
        row += [_cell_value(f, values.get(f.key), build_uri) for f in fields]
        ws.append(row)

    # Rough auto-width based on header/content length.
    for idx, header in enumerate(headers, start=1):
        width = max(len(str(header)), 12)
        for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx, values_only=True):
            width = max(width, len(str(row[0])) if row[0] is not None else 0)
        ws.column_dimensions[get_column_letter(idx)].width = min(width + 2, 60)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
