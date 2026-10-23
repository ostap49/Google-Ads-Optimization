"""Account / campaign / ad group / keyword performance."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.fmt import compact, from_micros, r
from gads.queries import performance as Q
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, agg, ctx, deltas, is_metrics, limit_default, m


def _vs_target(k: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if t.get("target_cpa") and k.get("cpa") is not None:
        out["cpa_vs_target_pct"] = r((k["cpa"] / float(t["target_cpa"]) - 1) * 100, 1)
    if t.get("target_roas") and k.get("roas") is not None:
        out["roas_vs_target_pct"] = r((k["roas"] / float(t["target_roas"]) - 1) * 100, 1)
    return out


@tool(title="Account overview")
def get_account_overview(customer_id: str, date_range: str = "LAST_30_DAYS", compare: bool = True) -> dict[str, Any]:
    """Spend, conversions, conv value, CPA, ROAS, CTR, CPC, search impression share — vs previous period
    and vs brief targets, with a channel-type breakdown.

    date_range: preset (LAST_7_DAYS, LAST_30_DAYS, THIS_MONTH, LAST_MONTH, ...) or "YYYY-MM-DD..YYYY-MM-DD".
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    cur_rows = g.search(cid, Q.account_totals(dr))
    cur = {**agg(cur_rows), **(is_metrics(cur_rows[0]) if cur_rows else {})}
    cur["all_conversions"] = r(sum(x.get("metrics.all_conversions") or 0 for x in cur_rows))
    out = account_header(cid, info, dr)
    out["kpis"] = cur
    out["vs_target"] = _vs_target(cur, t)
    out["targets"] = {k: t[k] for k in ("target_cpa", "target_roas", "monthly_budget", "kpi")}
    if compare:
        pdr = dr.previous()
        prev_rows = g.search(cid, Q.account_totals(pdr))
        prev = {**agg(prev_rows), **(is_metrics(prev_rows[0]) if prev_rows else {})}
        out["previous"] = {"period": pdr.as_dict(), "kpis": prev}
        out["delta_pct"] = deltas(cur, prev)
        out["delta_pct"]["search_is_pts"] = (
            r(cur["search_is"] - prev["search_is"], 1) if cur.get("search_is") is not None and prev.get("search_is") is not None else None
        )
    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in g.search(cid, Q.campaigns(dr)):
        by_channel[row["campaign.advertising_channel_type"]].append(row)
    out["by_channel"] = sorted(
        ({"channel": ch, "campaigns": len({x["campaign.id"] for x in rows}), **agg(rows)} for ch, rows in by_channel.items()),
        key=lambda x: -(x["cost"] or 0),
    )
    if not t["brief_exists"]:
        out["hint"] = "No account brief yet — set target_cpa/target_roas with update_account_brief for target-aware analysis."
    return out


@tool(title="Campaign performance")
def get_campaign_performance(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    segment: str | None = None,
    channel_types: list[str] | None = None,
    campaign_ids: list[str] | None = None,
    compare: bool = True,
    include_paused: bool = True,
    limit: int | None = None,
    sort_by: str = "cost",
) -> dict[str, Any]:
    """Campaign metrics with optional segment (date | device | network | day_of_week | hour) and
    period-over-period deltas (%) per campaign (+segment value).

    channel_types: e.g. ["SEARCH", "PERFORMANCE_MAX", "DEMAND_GEN", "DISPLAY", "SHOPPING", "VIDEO"].
    sort_by: any returned KPI (cost, conversions, cpa, roas, ctr, clicks, conv_value, search_is ...).
    """
    if segment and segment not in Q.SEGMENTS:
        raise ValueError(f"segment must be one of {sorted(Q.SEGMENTS)}")
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    seg_field = Q.SEGMENTS.get(segment) if segment else None

    def build(rows: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
        out: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row["campaign.id"], row.get(seg_field) if seg_field else None)
            item = {
                "campaign_id": str(row["campaign.id"]),
                "campaign": row["campaign.name"],
                "status": row["campaign.status"],
                "primary_status": row.get("campaign.primary_status"),
                "channel": row["campaign.advertising_channel_type"],
                "bidding": row["campaign.bidding_strategy_type"],
                "daily_budget": from_micros(row.get("campaign_budget.amount_micros")),
            }
            if seg_field:
                item[segment] = row.get(seg_field)
            item.update(m(row))
            if segment in Q.IS_COMPATIBLE:
                item.update(is_metrics(row))
            out[key] = item
        return out

    cur = build(g.search(cid, Q.campaigns(dr, segment, channel_types, campaign_ids, include_paused)))
    prev: dict[tuple, dict[str, Any]] = {}
    if compare and segment != "date":
        pdr = dr.previous()
        prev = build(g.search(cid, Q.campaigns(pdr, segment, channel_types, campaign_ids, include_paused)))
    rows = []
    for key, item in cur.items():
        if compare and segment != "date":
            item["delta_pct"] = {k: v for k, v in deltas(item, prev.get(key)).items() if k in ("cost", "conversions", "cpa", "roas", "ctr", "conv_value")}
        item.update(_vs_target(item, t))
        rows.append(item)
    out = compact(rows, limit=limit_default(limit) if segment is None else max(limit_default(limit), 100),
                  sort_by=sort_by if segment != "date" else None,
                  totals_fields=("cost", "clicks", "impressions", "conversions", "conv_value"))
    if segment == "date":
        out["rows"].sort(key=lambda x: (x["date"], x["campaign"]))
    tot = out["totals"]
    tot["cpa"] = r(tot["cost"] / tot["conversions"]) if tot["conversions"] else None
    tot["roas"] = r(tot["conv_value"] / tot["cost"]) if tot["cost"] else None
    out.update(account_header(cid, info, dr))
    if compare and segment != "date":
        out["compared_to"] = dr.previous().as_dict()
    return out


@tool(title="Ad group & keyword performance (with Quality Score)")
def get_adgroup_and_keyword_performance(
    customer_id: str,
    level: str = "keyword",
    date_range: str = "LAST_30_DAYS",
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    min_cost: float = 0.0,
    limit: int | None = None,
    sort_by: str = "cost",
) -> dict[str, Any]:
    """level="keyword": keyword_view with Quality Score + components (expected CTR, ad relevance,
    landing page experience), bids, match types, flags. level="ad_group": ad group metrics.
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    rows: list[dict[str, Any]] = []
    if level == "ad_group":
        for row in g.search(cid, Q.ad_groups(dr, campaign_id)):
            item = {"campaign_id": str(row["campaign.id"]), "campaign": row["campaign.name"],
                    "ad_group_id": str(row["ad_group.id"]), "ad_group": row["ad_group.name"],
                    "status": row["ad_group.status"], "type": row["ad_group.type"],
                    "cpc_bid": from_micros(row.get("ad_group.cpc_bid_micros")), **m(row)}
            if isinstance(row.get("metrics.search_impression_share"), (int, float)):
                item["search_is"] = r(row["metrics.search_impression_share"] * 100, 1)
            if (item["cost"] or 0) >= min_cost:
                rows.append(item)
    elif level == "keyword":
        qs_dist: dict[str, int] = defaultdict(int)
        for row in g.search(cid, Q.keywords(dr, campaign_id, ad_group_id)):
            k = m(row)
            qs = row.get("ad_group_criterion.quality_info.quality_score") or None
            comps = {
                "expected_ctr": row.get("ad_group_criterion.quality_info.search_predicted_ctr"),
                "ad_relevance": row.get("ad_group_criterion.quality_info.creative_quality_score"),
                "landing_page": row.get("ad_group_criterion.quality_info.post_click_quality_score"),
            }
            qs_dist[str(qs) if qs else "none"] += 1
            flags = []
            if qs and qs <= 4:
                flags.append("low_quality_score")
            flags += [f"{name}_below_average" for name, v in comps.items() if v == "BELOW_AVERAGE"]
            if (k["cost"] or 0) > 0 and not k["conversions"] and t.get("target_cpa") and k["cost"] >= 2 * float(t["target_cpa"]):
                flags.append("zero_conv_spender")
            text = (row.get("ad_group_criterion.keyword.text") or "").lower()
            if any(c in text for c in t["competitor_terms"]):
                flags.append("competitor_term")
            item = {
                "campaign_id": str(row["campaign.id"]), "campaign": row["campaign.name"],
                "ad_group_id": str(row["ad_group.id"]), "ad_group": row["ad_group.name"],
                "criterion_id": str(row["ad_group_criterion.criterion_id"]),
                "keyword": row.get("ad_group_criterion.keyword.text"),
                "match_type": row.get("ad_group_criterion.keyword.match_type"),
                "status": row.get("ad_group_criterion.status"),
                "serving": row.get("ad_group_criterion.system_serving_status"),
                "cpc_bid": from_micros(row.get("ad_group_criterion.cpc_bid_micros")),
                "effective_cpc_bid": from_micros(row.get("ad_group_criterion.effective_cpc_bid_micros")),
                "quality_score": qs, **comps, **k, "flags": flags,
            }
            if isinstance(row.get("metrics.search_impression_share"), (int, float)):
                item["search_is"] = r(row["metrics.search_impression_share"] * 100, 1)
            if (item["cost"] or 0) >= min_cost:
                rows.append(item)
        extra_qs = {"quality_score_distribution": dict(sorted(qs_dist.items()))}
        cost_total = sum(x["cost"] or 0 for x in rows) or 0
        weighted = [(x["quality_score"], x["impressions"]) for x in rows if x["quality_score"]]
        imp = sum(w for _, w in weighted)
        extra_qs["impression_weighted_qs"] = r(sum(q * w for q, w in weighted) / imp, 2) if imp else None
        extra_qs["cost_on_low_qs_pct"] = r(
            sum(x["cost"] or 0 for x in rows if x["quality_score"] and x["quality_score"] <= 4) / cost_total * 100, 1
        ) if cost_total else None
    else:
        raise ValueError("level must be 'keyword' or 'ad_group'")
    out = compact(rows, limit=limit_default(limit), sort_by=sort_by,
                  totals_fields=("cost", "clicks", "impressions", "conversions", "conv_value"))
    out.update(account_header(cid, info, dr))
    if level == "keyword":
        out.update(extra_qs)
    return out
