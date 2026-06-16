"""Dynamic statistics computed from a collection's user-defined data.

Because every collection has a different, user-defined schema, the stats are
derived from the FieldDefinitions at runtime:
  * numeric fields (price/number/decimal/year)  -> sum, average, min, max
  * choice fields                               -> value distribution
  * item types ("Art")                          -> counts

Aggregation is done in Python (not in SQL) so it behaves the same on SQLite and
PostgreSQL and works directly on the JSON ``values`` mapping.
"""

from __future__ import annotations

from collections import Counter

from .models import FieldType

NUMERIC_TYPES = {FieldType.NUMBER, FieldType.YEAR, FieldType.DECIMAL, FieldType.PRICE}
PRICE_TYPES = {FieldType.PRICE, FieldType.DECIMAL}


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collection_stats(collection, items, fields) -> dict:
    items = list(items)
    fields = list(fields)

    numeric = []
    for fd in fields:
        if fd.field_type in NUMERIC_TYPES:
            nums = [n for it in items if (n := _to_number((it.values or {}).get(fd.key))) is not None]
            if nums:
                is_money = fd.field_type in PRICE_TYPES
                numeric.append({
                    'field': fd,
                    'count': len(nums),
                    'sum': sum(nums),
                    'avg': sum(nums) / len(nums),
                    'min': min(nums),
                    'max': max(nums),
                    'is_money': is_money,
                    'currency': (fd.config or {}).get('currency', 'EUR') if is_money else '',
                })

    distributions = []
    for fd in fields:
        if fd.field_type in (FieldType.CHOICE, FieldType.MULTICHOICE):
            counter: Counter = Counter()
            for it in items:
                value = (it.values or {}).get(fd.key)
                if fd.field_type == FieldType.MULTICHOICE and isinstance(value, list):
                    counter.update(value)
                elif value:
                    counter[value] += 1
            if counter:
                distributions.append({'field': fd, 'rows': counter.most_common()})

    type_counter: Counter = Counter()
    for it in items:
        type_counter[str(it.item_type) if it.item_type else 'Ohne Art'] += 1

    return {
        'total_items': len(items),
        'numeric': numeric,
        'distributions': distributions,
        'type_rows': type_counter.most_common(),
    }


def total_price_value(items, fields) -> float:
    """Sum of all price/decimal field values across the given items (overall value)."""
    price_keys = [f.key for f in fields if f.field_type in PRICE_TYPES]
    total = 0.0
    for it in items:
        values = it.values or {}
        for key in price_keys:
            n = _to_number(values.get(key))
            if n is not None:
                total += n
    return total
