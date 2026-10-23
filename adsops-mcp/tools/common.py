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


# ----------------------------------------------------------------------------- metrics

METRICS = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
           "metrics.conversions, metrics.conversions_value")
IS_METRICS = ("metrics.search_impression_share, metrics.search_budget_lost_impression_share, "
              "metrics.search_rank_lost_impression_share")


def m(row: dict[str, Any]) -> dict[str, Any]:
    """Raw metrics from a flat row -> KPI dict in currency units."""
    from core.fmt import kpis
    return kpis(
        from_micros(row.get("metrics.cost_micros") or 0, 4) or 0.0,
        row.get("metrics.clicks") or 0,
        row.get("metrics.impressions") or 0,
        row.get("metrics.conversions") or 0.0,
        row.get("metrics.conversions_value") or 0.0,
    )


def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum raw metric fields over rows and derive KPIs."""
    tot = {"metrics.cost_micros": 0, "metrics.impressions": 0, "metrics.clicks": 0,
           "metrics.conversions": 0.0, "metrics.conversions_value": 0.0}
    for r in rows:
        for k in tot:
            tot[k] += r.get(k) or 0
    return m(tot)


def is_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Impression share fields as % (None when not reported). Google reports <10% as 0.0999."""
    def pct(v: Any) -> Any:
        return round(float(v) * 100, 1) if isinstance(v, (int, float)) else None
    return {
        "search_is": pct(row.get("metrics.search_impression_share")),
        "lost_is_budget": pct(row.get("metrics.search_budget_lost_impression_share")),
        "lost_is_rank": pct(row.get("metrics.search_rank_lost_impression_share")),
    }


DELTA_FIELDS = ("cost", "clicks", "impressions", "conversions", "conv_value", "cpa", "roas", "ctr", "cpc", "conv_rate")


def deltas(cur: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    from core.fmt import pct_delta
    if not prev:
        return {}
    return {f: pct_delta(cur.get(f), prev.get(f)) for f in DELTA_FIELDS}


def safe(section: str, fn, errors: dict[str, str]):
    """Run one section of a multi-query tool; record API incompatibilities instead of failing the tool."""
    from gads.client import GadsError
    try:
        return fn()
    except GadsError as ex:
        errors[section] = str(ex)[:400]
        return None


def cost_threshold(cid: str, fallback_cpa: float | None, multiplier: float | None = None,
                   floor: float | None = None) -> tuple[float, str]:
    """Wasted-spend threshold: multiplier x target CPA (brief), else x account CPA, never below the floor."""
    from memory.brief import targets
    th = get_settings().thresholds()
    mult = multiplier if multiplier is not None else th["waste_cpa_multiplier"]
    floor = floor if floor is not None else th["min_waste_spend"]
    t = targets(cid)
    if t.get("target_cpa"):
        return max(floor, float(t["target_cpa"]) * mult), f"{mult}x target CPA from brief ({t['target_cpa']})"
    if fallback_cpa:
        return max(floor, float(fallback_cpa) * mult), f"{mult}x account CPA for the period ({fallback_cpa}) — set target_cpa in the brief"
    return floor, "min_waste_spend floor (no target CPA and no conversions)"
