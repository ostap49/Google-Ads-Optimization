"""Shared helpers for tool implementations."""
from __future__ import annotations

from typing import Any

from core import accounts
from core.config import get_settings
from core.dates import DateRange, resolve as resolve_dates
from core.fmt import from_micros
from gads.client import Gads, get_gads


def ctx(customer_id: str, date_range: str | None = None, gads: Gads | None = None) -> tuple[Gads, str, dict[str, Any], DateRange | None]:
    """Resolve account + its metadata + date range in the account time zone."""
    g = gads or get_gads()
    cid = accounts.resolve(customer_id)
    info = g.account_info(cid)
    dr = resolve_dates(date_range, info.get("time_zone")) if date_range is not None else None
    return g, cid, info, dr


def money_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add currency-unit siblings for every *_micros field (metrics.cost_micros -> metrics.cost)."""
    out = dict(row)
    for k, v in row.items():
        if k.endswith("_micros") and isinstance(v, (int, float)):
            out[k[: -len("_micros")]] = from_micros(v)
    return out


def account_header(cid: str, info: dict[str, Any], dr: DateRange | None = None) -> dict[str, Any]:
    h = {"customer_id": cid, "account": info.get("name"), "currency": info.get("currency")}
    alias = accounts.alias_for(cid)
    if alias:
        h["alias"] = alias
    if dr:
        h["period"] = dr.as_dict()
    return h


def limit_default(limit: int | None) -> int:
    return int(limit or get_settings().default_limit)


def q_in(values: list[str] | tuple[str, ...]) -> str:
    """('A','B') literal for GAQL IN clauses."""
    return "(" + ", ".join(f"'{v}'" if not str(v).isdigit() else str(v) for v in values) + ")"


def esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("'", "\\'")
