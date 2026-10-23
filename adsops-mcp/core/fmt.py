"""Output shaping: micros conversion, compact tables, deltas."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Sequence

MICROS = 1_000_000


def from_micros(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value) / MICROS, digits)


def to_micros(amount: float | int | None) -> int | None:
    """Currency units -> micros. Rounds to the nearest 10,000 micros (0.01), which every currency accepts."""
    if amount is None:
        return None
    cents = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(cents * 100) * 10_000


def div(a: Any, b: Any, digits: int = 4) -> float | None:
    try:
        if b in (None, 0) or a is None:
            return None
        return round(float(a) / float(b), digits)
    except (TypeError, ZeroDivisionError):
        return None


def pct_delta(cur: Any, prev: Any, digits: int = 1) -> float | None:
    if cur is None or prev in (None, 0):
        return None
    return round((float(cur) - float(prev)) / abs(float(prev)) * 100, digits)


def r(value: Any, digits: int = 2) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def clean_id(customer_id: str | int) -> str:
    return str(customer_id).replace("-", "").strip()


def fmt_id(customer_id: str | int) -> str:
    c = clean_id(customer_id)
    return f"{c[:3]}-{c[3:6]}-{c[6:]}" if len(c) == 10 else c


def kpis(cost: float, clicks: float, impressions: float, conversions: float, value: float) -> dict[str, Any]:
    """Derived KPIs from raw totals (cost/value already in currency units)."""
    return {
        "cost": r(cost),
        "impressions": int(impressions or 0),
        "clicks": int(clicks or 0),
        "conversions": r(conversions),
        "conv_value": r(value),
        "ctr": r(div(clicks, impressions, 6) * 100, 2) if impressions else None,
        "cpc": r(div(cost, clicks)),
        "cpa": r(div(cost, conversions)),
        "roas": r(div(value, cost)),
        "conv_rate": r(div(conversions, clicks, 6) * 100, 2) if clicks else None,
    }


def compact(
    rows: Sequence[dict[str, Any]],
    limit: int = 25,
    sort_by: str | None = None,
    descending: bool = True,
    totals_fields: Iterable[str] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Top-N rows + totals + truncated flag. Totals are computed over ALL rows, not just the returned ones."""
    rows = list(rows)
    if sort_by:
        rows.sort(key=_sort_key(sort_by), reverse=descending)
    totals = {f: r(sum((row.get(f) or 0) for row in rows)) for f in totals_fields}
    limit = max(1, min(int(limit or 25), 1000))
    out: dict[str, Any] = {
        "rows": rows[:limit],
        "row_count": len(rows),
        "returned": min(limit, len(rows)),
        "truncated": len(rows) > limit,
    }
    if totals:
        out["totals"] = totals
    if sort_by:
        out["sorted_by"] = sort_by
    if extra:
        out.update(extra)
    return out


def _sort_key(field: str) -> Callable[[dict[str, Any]], Any]:
    def key(row: dict[str, Any]) -> Any:
        v = row.get(field)
        if v is None:
            return (0, 0)
        if isinstance(v, (int, float)):
            return (1, v)
        return (1, str(v))
    return key
