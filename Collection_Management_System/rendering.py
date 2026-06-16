"""Turn stored JSON values into display-ready cells for templates."""

from __future__ import annotations

from dataclasses import dataclass

from .models import FieldType


@dataclass
class Cell:
    kind: str   # 'text' | 'image' | 'file' | 'bool' | 'url' | 'empty'
    value: str = ''
    url: str = ''


def render_cell(field, raw) -> Cell:
    if raw in (None, '', [], {}):
        return Cell('empty', '–')

    t = field.field_type
    if t == FieldType.IMAGE and isinstance(raw, dict):
        return Cell('image', raw.get('name', ''), raw.get('url', ''))
    if t == FieldType.FILE and isinstance(raw, dict):
        return Cell('file', raw.get('name', 'Datei'), raw.get('url', ''))
    if t == FieldType.BOOLEAN:
        return Cell('bool', 'Ja' if raw else 'Nein')
    if t == FieldType.PRICE:
        currency = (field.config or {}).get('currency', 'EUR')
        try:
            return Cell('text', f'{float(raw):.2f} {currency}')
        except (TypeError, ValueError):
            return Cell('text', f'{raw} {currency}')
    if t == FieldType.URL:
        return Cell('url', str(raw), str(raw))
    if t == FieldType.MULTICHOICE and isinstance(raw, list):
        return Cell('text', ', '.join(str(v) for v in raw))
    return Cell('text', str(raw))


def item_row(item, fields) -> list[Cell]:
    """List of display cells for an item, one per field (column order)."""
    return [render_cell(fd, (item.values or {}).get(fd.key)) for fd in fields]
