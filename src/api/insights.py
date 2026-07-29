"""Period-over-period performance insights (read-only).

Each insight compares the current window (last N days) against the previous
window of the same length, live from the API — no local snapshots needed.
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from google.ads.googleads.client import GoogleAdsClient

logger = logging.getLogger(__name__)

MAX_ROWS = 100


def _windows(days: int) -> Tuple[str, str]:
    """Return GAQL BETWEEN clauses for (current, previous) windows."""
    today = date.today()
    cur_start = today - timedelta(days=days - 1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    cur = f"segments.date BETWEEN '{cur_start}' AND '{today}'"
    prev = f"segments.date BETWEEN '{prev_start}' AND '{prev_end}'"
    return cur, prev


def _search(client: GoogleAdsClient, customer_id: str, query: str):
    """Yield response batches.

    Generator on purpose: keeps `service` alive while the stream is being
    consumed, avoiding '499 Stream removed (Channel deallocated!)'.
    """
    service = client.get_service("GoogleAdsService")
    for batch in service.search_stream(
        customer_id=customer_id.replace("-", ""), query=query
    ):
        yield batch


def _metrics_of(m) -> Dict[str, float]:
    return {
        "clicks": m.clicks,
        "cost": m.cost_micros / 1_000_000,
        "conversions": m.conversions,
        "value": m.conversions_value,
    }


def _add(acc: Dict[str, float], m: Dict[str, float]) -> None:
    for k, v in m.items():
        acc[k] = acc.get(k, 0.0) + v


def _finalize(cur: Dict[str, float], prev: Dict[str, float]) -> Dict[str, Any]:
    """Combine current/previous sums into a display row fragment."""
    def cpa(d):
        return d["cost"] / d["conversions"] if d.get("conversions") else 0.0

    def roas(d):
        return d["value"] / d["cost"] if d.get("cost") else 0.0

    return {
        "clicks": cur.get("clicks", 0),
        "cost": round(cur.get("cost", 0.0), 2),
        "conversions": round(cur.get("conversions", 0.0), 1),
        "cpa": round(cpa(cur), 2),
        "roas": round(roas(cur), 2),
        "prev_clicks": prev.get("clicks", 0),
        "prev_cost": round(prev.get("cost", 0.0), 2),
        "prev_conversions": round(prev.get("conversions", 0.0), 1),
        "prev_cpa": round(cpa(prev), 2),
        "prev_roas": round(roas(prev), 2),
        "delta_cost": round(cur.get("cost", 0.0) - prev.get("cost", 0.0), 2),
        "delta_conversions": round(
            cur.get("conversions", 0.0) - prev.get("conversions", 0.0), 1
        ),
    }


# ── Geo performance ───────────────────────────────────────────────


def _resolve_geo_names(client, geo_ids: List[str]) -> Dict[str, str]:
    """Resolve geo target constant IDs to canonical names via GAQL."""
    names: Dict[str, str] = {}
    ids = [g for g in geo_ids if g.isdigit()]
    service = client.get_service("GoogleAdsService")
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        query = f"""
            SELECT geo_target_constant.id, geo_target_constant.canonical_name,
                   geo_target_constant.name
            FROM geo_target_constant
            WHERE geo_target_constant.id IN ({', '.join(chunk)})
        """
        try:
            # geo_target_constant queries run without a customer context issue
            # by using any accessible customer id — the caller passes one in
            # via functools.partial; here we reuse login customer.
            for batch in service.search_stream(
                customer_id=str(client.login_customer_id), query=query
            ):
                for r in batch.results:
                    gtc = r.geo_target_constant
                    names[str(gtc.id)] = gtc.canonical_name or gtc.name
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Geo name resolution failed: %s", exc)
            break
    return names


def geo_insight(client, customer_id: str, days: int) -> Dict[str, Any]:
    cur_clause, prev_clause = _windows(days)

    def fetch(clause):
        query = f"""
            SELECT campaign.name,
                   geographic_view.country_criterion_id,
                   segments.geo_target_region,
                   metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM geographic_view
            WHERE {clause}
              AND campaign.status = 'ENABLED'
        """
        agg: Dict[Tuple[str, str], Dict[str, float]] = {}
        for batch in _search(client, customer_id, query):
            for r in batch.results:
                region = r.segments.geo_target_region
                geo_id = (
                    region.split("/")[-1]
                    if region
                    else str(r.geographic_view.country_criterion_id)
                )
                key = (r.campaign.name, geo_id)
                agg.setdefault(key, {})
                _add(agg[key], _metrics_of(r.metrics))
        return agg

    cur_agg = fetch(cur_clause)
    prev_agg = fetch(prev_clause)

    geo_ids = sorted({gid for (_, gid) in set(cur_agg) | set(prev_agg)})
    names = _resolve_geo_names(client, geo_ids)

    rows = []
    for key in set(cur_agg) | set(prev_agg):
        campaign, geo_id = key
        row = {
            "geo": names.get(geo_id, geo_id),
            "campaign": campaign,
            **_finalize(cur_agg.get(key, {}), prev_agg.get(key, {})),
        }
        rows.append(row)

    converting = [r for r in rows if r["conversions"] > 0]
    best = sorted(converting, key=lambda r: (-r["conversions"], r["cpa"]))[:15]

    avg_cpa_values = [r["cpa"] for r in converting if r["cpa"] > 0]
    avg_cpa = sum(avg_cpa_values) / len(avg_cpa_values) if avg_cpa_values else 0.0
    worst = sorted(
        [
            r
            for r in rows
            if (r["cost"] >= 5 and r["conversions"] == 0)
            or (avg_cpa and r["cpa"] > 2 * avg_cpa)
        ],
        key=lambda r: -r["cost"],
    )[:15]

    return {
        "best": best,
        "worst": worst,
        "total_geos": len(rows),
        "avg_cpa": round(avg_cpa, 2),
    }


# ── Keyword movers ────────────────────────────────────────────────


def keyword_insight(client, customer_id: str, days: int) -> Dict[str, Any]:
    cur_clause, prev_clause = _windows(days)

    def fetch(clause):
        query = f"""
            SELECT campaign.name, ad_group.name,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM keyword_view
            WHERE {clause}
              AND campaign.status = 'ENABLED'
              AND ad_group.status = 'ENABLED'
              AND ad_group_criterion.status = 'ENABLED'
        """
        agg: Dict[Tuple[str, str, str], Dict[str, float]] = {}
        for batch in _search(client, customer_id, query):
            for r in batch.results:
                key = (
                    r.campaign.name,
                    r.ad_group.name,
                    r.ad_group_criterion.keyword.text,
                )
                agg.setdefault(key, {})
                _add(agg[key], _metrics_of(r.metrics))
        return agg

    cur_agg = fetch(cur_clause)
    prev_agg = fetch(prev_clause)

    rows = []
    for key in set(cur_agg) | set(prev_agg):
        campaign, ad_group, text = key
        fin = _finalize(cur_agg.get(key, {}), prev_agg.get(key, {}))
        if fin["cost"] == 0 and fin["prev_cost"] == 0:
            continue
        rows.append(
            {"keyword": text, "campaign": campaign, "ad_group": ad_group, **fin}
        )

    rows.sort(key=lambda r: -abs(r["delta_cost"]))
    return {"rows": rows[:MAX_ROWS], "total_rows": len(rows)}


# ── PMax products ─────────────────────────────────────────────────


def pmax_products_insight(client, customer_id: str, days: int) -> Dict[str, Any]:
    cur_clause, prev_clause = _windows(days)

    def fetch(clause):
        query = f"""
            SELECT campaign.name,
                   segments.product_item_id,
                   segments.product_title,
                   metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM shopping_performance_view
            WHERE {clause}
              AND campaign.advertising_channel_type = 'PERFORMANCE_MAX'
              AND campaign.status = 'ENABLED'
        """
        agg: Dict[Tuple[str, str], Dict[str, float]] = {}
        titles: Dict[str, str] = {}
        for batch in _search(client, customer_id, query):
            for r in batch.results:
                pid = r.segments.product_item_id
                key = (r.campaign.name, pid)
                titles[pid] = r.segments.product_title or pid
                agg.setdefault(key, {})
                _add(agg[key], _metrics_of(r.metrics))
        return agg, titles

    cur_agg, cur_titles = fetch(cur_clause)
    prev_agg, prev_titles = fetch(prev_clause)
    titles = {**prev_titles, **cur_titles}

    rows = []
    for key in set(cur_agg) | set(prev_agg):
        campaign, pid = key
        fin = _finalize(cur_agg.get(key, {}), prev_agg.get(key, {}))
        flag = fin["cost"] >= 5 and fin["conversions"] == 0
        rows.append(
            {
                "product": titles.get(pid, pid),
                "product_id": pid,
                "campaign": campaign,
                "_flag": flag,
                **fin,
            }
        )

    rows.sort(key=lambda r: -r["cost"])
    flagged = sum(1 for r in rows if r["_flag"])
    return {"rows": rows[:MAX_ROWS], "total_rows": len(rows), "flagged": flagged}


INSIGHTS = {
    "geo": geo_insight,
    "keywords": keyword_insight,
    "pmax-products": pmax_products_insight,
}
