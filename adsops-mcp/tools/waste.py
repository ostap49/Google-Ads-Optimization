"""Cross-campaign wasted-spend analysis."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.fmt import compact, r
from gads.queries import performance as PQ
from gads.queries import pmax as PMQ
from gads.queries import segments as SQ
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, agg, cost_threshold, ctx, limit_default, m, safe
from tools.search_terms import fetch_terms

DIMENSIONS = ("search_terms", "keywords", "placements", "products", "devices", "locations", "hours")


def geo_name_map(g, cid: str, resource_names: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    todo = [rn for rn in set(resource_names) if rn]
    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        for row in g.cached_search(cid, SQ.geo_names(chunk)):
            names[row["geo_target_constant.resource_name"]] = row.get("geo_target_constant.canonical_name") or row.get("geo_target_constant.name")
    return names


@tool(title="Analyze wasted spend")
def analyze_wasted_spend(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    dimensions: list[str] | None = None,
    threshold: float | None = None,
    cpa_multiplier: float | None = None,
    location_level: str = "region",
    include_inefficient_segments: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Find spend with no return across search terms, keywords, placements, products, devices, locations, hours.

    An item is waste when cost >= threshold and conversions == 0. Default threshold = 2x target CPA from the
    brief (else 2x account CPA for the period; never below thresholds.min_waste_spend). For devices/locations/
    hours with conversions, include_inefficient_segments also flags CPA > 2.5x target, counting the excess
    cost over target as waste. Output is ranked by $ wasted with a proposed action + the propose_* tool to use.
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    dims = [d for d in (dimensions or DIMENSIONS) if d in DIMENSIONS]
    errors: dict[str, str] = {}
    totals_rows = g.search(cid, PQ.account_totals(dr))
    acct = agg(totals_rows)
    thr, basis = (threshold, "explicit") if threshold else cost_threshold(cid, acct["cpa"], cpa_multiplier)
    target_cpa = float(t["target_cpa"]) if t.get("target_cpa") else acct["cpa"]
    items: list[dict[str, Any]] = []

    def add(dim: str, entity: str, campaign: str | None, k: dict[str, Any], action: str, tool_name: str | None,
            hint: dict[str, Any] | None = None, waste: float | None = None, kind: str = "zero_conv") -> None:
        items.append({"dimension": dim, "entity": entity, "campaign": campaign, "kind": kind,
                      "wasted": r(waste if waste is not None else k["cost"]), "cost": k["cost"], "clicks": k["clicks"],
                      "conversions": k["conversions"], "cpa": k.get("cpa"),
                      "action": action, "propose_tool": tool_name, "params_hint": hint or {}})

    def zero(k: dict[str, Any]) -> bool:
        return (k["cost"] or 0) >= thr and not k["conversions"]

    def inefficient(k: dict[str, Any]) -> float | None:
        if not include_inefficient_segments or not target_cpa or not k["conversions"] or k["cpa"] is None:
            return None
        if k["cpa"] > 2.5 * target_cpa and (k["cost"] or 0) >= thr:
            return k["cost"] - k["conversions"] * target_cpa
        return None

    if "search_terms" in dims:
        rows = safe("search_terms", lambda: fetch_terms(g, cid, dr, errors=errors), errors) or []
        for x in rows:
            if zero(x) and not any(b in (x["search_term"] or "").lower() for b in t["brand_terms"]):
                pmax = x["channel"] == "PERFORMANCE_MAX"
                add("search_terms", x["search_term"], x["campaign"], x,
                    "Add as negative (exact; phrase if the n-gram is junk)" + (" — PMax: use account-level negative list" if pmax else ""),
                    "propose_add_negative_keywords",
                    {"level": "shared_list" if pmax else "campaign", "campaign_id": x["campaign_id"],
                     "keywords": [{"text": x["search_term"], "match_type": "EXACT"}]})

    if "keywords" in dims:
        rows = safe("keywords", lambda: g.search(cid, PQ.keywords(dr)), errors) or []
        for x in rows:
            k = m(x)
            if zero(k) and x.get("ad_group_criterion.status") == "ENABLED":
                add("keywords", f"{x.get('ad_group_criterion.keyword.text')} [{x.get('ad_group_criterion.keyword.match_type')}]",
                    x["campaign.name"], k, "Pause keyword or cut bid 30-50%; check search terms it matches first",
                    "propose_set_status",
                    {"entity_type": "keyword", "ids": [f"{x['ad_group.id']}~{x['ad_group_criterion.criterion_id']}"], "status": "PAUSED"})

    if "placements" in dims:
        rows = safe("placements", lambda: g.search(cid, SQ.placements(dr)), errors) or []
        agg_pl: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for x in rows:
            agg_pl[(x.get("detail_placement_view.placement"), x.get("detail_placement_view.display_name"),
                    x.get("detail_placement_view.placement_type"))].append(x)
        for (pl, name, ptype), v in agg_pl.items():
            k = agg(v)
            if zero(k):
                camps = sorted({y["campaign.name"] for y in v})
                add("placements", f"{name or pl} ({ptype})", ", ".join(camps)[:120], k,
                    "Exclude placement (account-level exclusion list covers PMax too)", "propose_add_exclusions",
                    {"exclusion_type": "placement", "level": "account", "values": [pl]})

    if "products" in dims:
        rows = safe("products", lambda: g.search(cid, PMQ.product_performance(dr, None, None)), errors) or []
        by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        titles: dict[str, str] = {}
        for x in rows:
            key = str(x.get("segments.product_item_id"))
            by_item[key].append(x)
            titles[key] = x.get("segments.product_title") or key
        for key, v in by_item.items():
            k = agg(v)
            if zero(k):
                add("products", f"{titles[key]} ({key})", ", ".join(sorted({y['campaign.name'] for y in v}))[:120], k,
                    "Exclude from listing group / move to a low-priority 'zombie' asset group via custom label",
                    None, {"item_id": key})

    if "devices" in dims:
        rows = safe("devices", lambda: g.search(cid, SQ.devices(dr)), errors) or []
        for x in rows:
            k = m(x)
            dev = x.get("segments.device")
            ch = x.get("campaign.advertising_channel_type")
            hint = {"exclusion_type": "device", "campaign_id": str(x["campaign.id"]), "values": [dev]}
            can_adjust = ch not in ("PERFORMANCE_MAX", "DEMAND_GEN")
            if zero(k):
                add("devices", dev, x["campaign.name"], k,
                    "Device bid adjustment -50%..-100%" if can_adjust else "PMax/Demand Gen: no device bid adjustments — review creatives/landing page on this device",
                    "propose_add_exclusions" if can_adjust else None, {**hint, "bid_modifier": 0.5} if can_adjust else {})
            else:
                excess = inefficient(k)
                if excess:
                    add("devices", dev, x["campaign.name"], k,
                        f"CPA {k['cpa']} vs target {r(target_cpa)} — bid down this device", "propose_add_exclusions" if can_adjust else None,
                        {**hint, "bid_modifier": round(max(0.1, target_cpa / k['cpa']), 2)} if can_adjust else {},
                        waste=excess, kind="inefficient")

    if "locations" in dims:
        rows = safe("locations", lambda: g.search(cid, SQ.locations(dr, location_level)), errors) or []
        seg = SQ.GEO_SEGMENT[location_level]
        names = safe("geo_names", lambda: geo_name_map(g, cid, [x.get(seg) for x in rows]), errors) or {}
        by_loc: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for x in rows:
            by_loc[(x["campaign.id"], x["campaign.name"], x.get(seg))].append(x)
        for (camp_id, camp, loc), v in by_loc.items():
            k = agg(v)
            label = names.get(loc, loc or "(unknown)")
            interest = sum(1 for y in v if y.get("geographic_view.location_type") == "AREA_OF_INTEREST")
            hint = {"exclusion_type": "location", "campaign_id": str(camp_id), "values": [loc]}
            suffix = " (incl. 'area of interest' traffic — check presence-only setting)" if interest else ""
            if zero(k):
                add("locations", label, camp, k, "Exclude location or bid -50%" + suffix, "propose_add_exclusions", hint)
            else:
                excess = inefficient(k)
                if excess:
                    add("locations", label, camp, k, f"CPA {k['cpa']} vs target {r(target_cpa)} — location bid down" + suffix,
                        "propose_add_exclusions", hint, waste=excess, kind="inefficient")

    if "hours" in dims:
        rows = safe("hours", lambda: g.search(cid, SQ.hours(dr)), errors) or []
        for x in rows:
            k = m(x)
            hour = x.get("segments.hour")
            hint = {"exclusion_type": "ad_schedule", "campaign_id": str(x["campaign.id"]), "hours": [hour]}
            if zero(k):
                add("hours", f"{hour:02d}:00" if isinstance(hour, int) else str(hour), x["campaign.name"], k,
                    "Ad schedule: bid -50%..-90% for this hour (or exclude it from the schedule)", "propose_add_exclusions", hint)
            else:
                excess = inefficient(k)
                if excess:
                    add("hours", f"{hour:02d}:00" if isinstance(hour, int) else str(hour), x["campaign.name"], k,
                        f"CPA {k['cpa']} vs target {r(target_cpa)} — schedule bid down", "propose_add_exclusions",
                        hint, waste=excess, kind="inefficient")

    out = compact(items, limit=limit_default(limit), sort_by="wasted", totals_fields=("wasted",))
    by_dim: dict[str, dict[str, float]] = defaultdict(lambda: {"items": 0, "wasted": 0.0})
    for it in items:
        by_dim[it["dimension"]]["items"] += 1
        by_dim[it["dimension"]]["wasted"] = r(by_dim[it["dimension"]]["wasted"] + (it["wasted"] or 0))
    out.update(account_header(cid, info, dr))
    out["by_dimension"] = dict(sorted(by_dim.items(), key=lambda kv: -kv[1]["wasted"]))
    out["threshold"] = {"amount": r(thr), "basis": basis}
    out["account_cost"] = acct["cost"]
    out["note"] = ("Dimensions overlap (a wasted search term also shows up in its keyword, device, hour...). "
                   "Do not add up dimensions; search_terms and keywords are the most actionable.")
    if errors:
        out["section_errors"] = errors
    return out
