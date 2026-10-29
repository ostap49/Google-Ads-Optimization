"""Performance Max insights."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from core.fmt import compact, from_micros, r
from gads.queries import performance as PQ
from gads.queries import pmax as Q
from tools._registry import tool
from tools.common import account_header, agg, ctx, limit_default, m, safe

MIN_ASSETS = {  # Google's minimums per asset group (Online sales / leads with assets)
    "HEADLINE": 3, "LONG_HEADLINE": 1, "DESCRIPTION": 2, "BUSINESS_NAME": 1,
    "MARKETING_IMAGE": 1, "SQUARE_MARKETING_IMAGE": 1, "LOGO": 1,
}
RECOMMENDED_ASSETS = {"HEADLINE": 15, "LONG_HEADLINE": 5, "DESCRIPTION": 5, "MARKETING_IMAGE": 5,
                      "SQUARE_MARKETING_IMAGE": 5, "PORTRAIT_MARKETING_IMAGE": 1, "YOUTUBE_VIDEO": 1}


@tool(title="Performance Max insights")
def pmax_insights(customer_id: str, campaign_id: str | None = None, date_range: str = "LAST_30_DAYS",
                  limit: int | None = None) -> dict[str, Any]:
    """PMax deep-dive: campaigns, asset groups (ad strength, metrics), asset coverage vs minimum/recommended
    counts per field type, asset-level metrics where the API exposes them, search-term insight categories,
    product performance (shopping_performance_view), listing-group structure, placements
    (performance_max_placement_view, impressions only) and channel breakdown (segments.ad_network_type).
    Each section degrades gracefully: incompatibilities are reported in section_errors.
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    lim = limit_default(limit)
    errors: dict[str, str] = {}
    out: dict[str, Any] = account_header(cid, info, dr)

    camps = g.search(cid, PQ.campaigns(dr, channel_types=["PERFORMANCE_MAX"],
                                       campaign_ids=[campaign_id] if campaign_id else None))
    out["campaigns"] = sorted(
        ({"campaign_id": str(x["campaign.id"]), "campaign": x["campaign.name"], "status": x["campaign.status"],
          "bidding": x["campaign.bidding_strategy_type"], "daily_budget": from_micros(x.get("campaign_budget.amount_micros")),
          **m(x)} for x in camps), key=lambda x: -(x["cost"] or 0))
    if not camps:
        out["note"] = "No Performance Max campaigns with data in this period."
        return out

    ags = safe("asset_groups", lambda: g.search(cid, Q.asset_groups(dr, campaign_id)), errors) or []
    out["asset_groups"] = compact(
        [{"campaign_id": str(x["campaign.id"]), "campaign": x["campaign.name"], "asset_group_id": str(x["asset_group.id"]),
          "asset_group": x["asset_group.name"], "status": x["asset_group.status"],
          "primary_status": x.get("asset_group.primary_status"), "ad_strength": x.get("asset_group.ad_strength"),
          **m(x)} for x in ags], limit=lim, sort_by="cost")

    # assets: try with metrics first (newer API versions), fall back to inventory only
    asset_rows = safe("asset_metrics", lambda: g.search(cid, Q.asset_group_assets(campaign_id, dr)), errors)
    with_metrics = asset_rows is not None
    if asset_rows is None:
        asset_rows = safe("assets", lambda: g.search(cid, Q.asset_group_assets(campaign_id)), errors) or []
    coverage: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    assets = []
    for a in asset_rows:
        agid = str(a["asset_group.id"])
        names[agid] = a["asset_group.name"]
        if a.get("asset_group_asset.status") == "ENABLED":
            coverage[agid][a["asset_group_asset.field_type"]] += 1
        item = {"asset_group": a["asset_group.name"], "field_type": a["asset_group_asset.field_type"],
                "asset_id": str(a["asset.id"]), "type": a.get("asset.type"),
                "text": a.get("asset.text_asset.text") or a.get("asset.name") or a.get("asset.youtube_video_asset.youtube_video_id"),
                "status": a.get("asset_group_asset.status"), "primary_status": a.get("asset_group_asset.primary_status"),
                "source": a.get("asset_group_asset.source")}
        if with_metrics:
            item.update(m(a))
        assets.append(item)
    gaps = []
    for agid, cnt in coverage.items():
        missing = {f: n - cnt.get(f, 0) for f, n in MIN_ASSETS.items() if cnt.get(f, 0) < n}
        below_rec = {f: f"{cnt.get(f, 0)}/{n}" for f, n in RECOMMENDED_ASSETS.items() if cnt.get(f, 0) < n}
        gaps.append({"asset_group_id": agid, "asset_group": names.get(agid), "counts": dict(cnt),
                     "missing_minimum": missing, "below_recommended": below_rec,
                     "auto_generated_assets": sum(1 for x in assets if x["asset_group"] == names.get(agid)
                                                  and x["source"] == "AUTOMATIC")})
    out["asset_coverage"] = gaps
    out["assets"] = compact(assets, limit=lim, sort_by="cost" if with_metrics else None)
    out["asset_metrics_available"] = with_metrics

    insights = []
    for c in out["campaigns"][:5]:
        rows = safe(f"search_insights_{c['campaign_id']}", lambda c=c: g.search(cid, Q.search_term_insights(dr, c["campaign_id"])), errors) or []
        for x in rows:
            insights.append({"campaign": c["campaign"], "category": x.get("campaign_search_term_insight.category_label") or "(uncategorized)",
                             "impressions": x.get("metrics.impressions"), "clicks": x.get("metrics.clicks"),
                             "conversions": r(x.get("metrics.conversions")), "conv_value": r(x.get("metrics.conversions_value"))})
    out["search_term_insights"] = compact(insights, limit=lim, sort_by="clicks")

    prods = safe("products", lambda: g.search(cid, Q.product_performance(dr, campaign_id)), errors) or []
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}
    for p in prods:
        k = p.get("segments.product_item_id") or "(unknown)"
        by_item[k].append(p)
        meta[k] = {"item_id": k, "title": p.get("segments.product_title"), "brand": p.get("segments.product_brand")}
    out["products"] = compact([{**meta[k], **agg(v)} for k, v in by_item.items()], limit=lim, sort_by="cost",
                              totals_fields=("cost", "conversions", "conv_value"))
    out["products"]["zero_conv_products_with_spend"] = sum(1 for v in by_item.values() if agg(v)["cost"] and not agg(v)["conversions"])

    lgs = safe("listing_groups", lambda: g.search(cid, Q.listing_groups(campaign_id)), errors) or []
    lg_summary: dict[str, Counter] = defaultdict(Counter)
    for x in lgs:
        lg_summary[x["asset_group.name"]][x.get("asset_group_listing_group_filter.type") or "?"] += 1
    out["listing_groups"] = {k: dict(v) for k, v in lg_summary.items()}

    pl = safe("placements", lambda: g.search(cid, Q.placements(dr, campaign_id)), errors) or []
    out["placements"] = compact(
        [{"placement": x.get("performance_max_placement_view.placement"),
          "display_name": x.get("performance_max_placement_view.display_name"),
          "type": x.get("performance_max_placement_view.placement_type"),
          "url": x.get("performance_max_placement_view.target_url"),
          "impressions": x.get("metrics.impressions")} for x in pl], limit=lim, sort_by="impressions")

    ch = safe("channel_breakdown", lambda: g.search(cid, Q.channel_breakdown(dr, campaign_id)), errors) or []
    by_net: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in ch:
        by_net[x.get("segments.ad_network_type") or "UNKNOWN"].append(x)
    tot_cost = sum(agg(v)["cost"] or 0 for v in by_net.values()) or 0
    out["channel_breakdown"] = sorted(
        ({"network": k, **agg(v), "cost_share_pct": r((agg(v)["cost"] or 0) / tot_cost * 100, 1) if tot_cost else None}
         for k, v in by_net.items()), key=lambda x: -(x["cost"] or 0))
    if errors:
        out["section_errors"] = errors
    return out


REUSABLE_FIELDS = ("HEADLINE", "LONG_HEADLINE", "DESCRIPTION", "BUSINESS_NAME", "MARKETING_IMAGE",
                   "SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE", "LOGO", "LANDSCAPE_LOGO", "YOUTUBE_VIDEO")


@tool(title="PMax assets for reuse")
def get_pmax_reusable_assets(customer_id: str, campaign_id: str, asset_group_id: str | None = None) -> dict[str, Any]:
    """Assets linked to a PMax campaign's asset groups, de-duplicated per (asset, field_type), in the exact
    shape propose_create_pmax_campaign accepts as asset_group.existing_assets — so a new PMax can reuse the
    same headlines / images / logo / business name without re-uploading. Removed links are skipped."""
    g, cid, info, _ = ctx(customer_id)
    cond = f"campaign.id = {int(campaign_id)}"
    if asset_group_id:
        cond += f" AND asset_group.id = {int(asset_group_id)}"
    rows = g.search(cid, f"""
        SELECT asset_group.id, asset_group.name, asset_group_asset.field_type, asset_group_asset.status,
               asset.resource_name, asset.type, asset.text_asset.text, asset.image_asset.full_size.width_pixels,
               asset.image_asset.full_size.height_pixels, asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE {cond} AND asset_group_asset.status != 'REMOVED'""")
    seen: set[tuple[str, str]] = set()
    existing: list[dict[str, str]] = []
    preview: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        ft = str(row.get("asset_group_asset.field_type") or "")
        rn = str(row.get("asset.resource_name") or "")
        if ft not in REUSABLE_FIELDS or not rn or (rn, ft) in seen:
            continue
        seen.add((rn, ft))
        existing.append({"asset": rn, "field_type": ft})
        label = (row.get("asset.text_asset.text")
                 or row.get("asset.youtube_video_asset.youtube_video_id")
                 or (f"{row.get('asset.image_asset.full_size.width_pixels')}x{row.get('asset.image_asset.full_size.height_pixels')}"
                     if row.get("asset.image_asset.full_size.width_pixels") else rn.rsplit("/", 1)[-1]))
        preview[ft].append(str(label))
    counts = Counter(e["field_type"] for e in existing)
    return {**account_header(cid, info), "campaign_id": str(campaign_id), "counts": dict(counts),
            "preview": dict(preview), "existing_assets": existing,
            "note": "PMax max per asset group: 15 headlines, 5 long headlines, 5 descriptions, 20 images, 5 logos, 5 videos — trim before reuse."}
