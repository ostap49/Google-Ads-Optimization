"""Shopping / product-level analysis (Standard Shopping + PMax retail)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.fmt import compact, from_micros, r
from gads.queries import pmax as Q
from tools._registry import tool
from tools.common import account_header, agg, cost_threshold, ctx, limit_default, safe


@tool(title="Shopping product performance")
def get_shopping_products(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: str | None = None,
    channel: str | None = None,
    flag: str | None = None,
    waste_threshold: float | None = None,
    include_zero_impression: bool = True,
    limit: int | None = None,
    sort_by: str = "cost",
) -> dict[str, Any]:
    """Product-level spend / conversions / ROAS across Shopping and PMax (channel: SHOPPING | PERFORMANCE_MAX | None=both).

    Flags: spend_no_conv (cost >= threshold, default 2x target CPA, and 0 conversions), low_roas (< target ROAS),
    zero_impressions (eligible product in shopping_product with no impressions in the period), not_eligible.
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    from memory.brief import targets
    t = targets(cid)
    errors: dict[str, str] = {}
    rows = g.search(cid, Q.product_performance(dr, campaign_id, channel))
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}
    for p in rows:
        k = str(p.get("segments.product_item_id") or "(unknown)")
        by_item[k].append(p)
        meta.setdefault(k, {"item_id": k, "title": p.get("segments.product_title"),
                            "brand": p.get("segments.product_brand"), "product_type": p.get("segments.product_type_l1"),
                            "custom_label_0": p.get("segments.product_custom_attribute0"), "campaigns": set()})
        meta[k]["campaigns"].add(p["campaign.name"])
    total = agg(rows)
    threshold, basis = (waste_threshold, "explicit") if waste_threshold else cost_threshold(cid, total["cpa"])
    items = []
    for k, v in by_item.items():
        a = agg(v)
        flags = []
        if (a["cost"] or 0) >= threshold and not a["conversions"]:
            flags.append("spend_no_conv")
        if t.get("target_roas") and a["roas"] is not None and a["conversions"] and a["roas"] < float(t["target_roas"]) * 0.7:
            flags.append("low_roas")
        items.append({**meta[k], "campaigns": sorted(meta[k]["campaigns"]), **a, "flags": flags})

    zero_imp, not_eligible = [], []
    if include_zero_impression:
        catalog = safe("shopping_product", lambda: g.search(cid, Q.SHOPPING_PRODUCTS), errors) or []
        with_imps = {k for k, v in by_item.items() if agg(v)["impressions"]}
        seen = set()
        for p in catalog:
            item_id = str(p.get("shopping_product.item_id"))
            if item_id in seen:
                continue
            seen.add(item_id)
            base = {"item_id": item_id, "title": p.get("shopping_product.title"), "brand": p.get("shopping_product.brand"),
                    "status": p.get("shopping_product.status"), "availability": p.get("shopping_product.availability"),
                    "price": from_micros(p.get("shopping_product.price_micros"))}
            if p.get("shopping_product.status") == "NOT_ELIGIBLE":
                issues = p.get("shopping_product.issues") or []
                base["issues"] = [i.get("description") or i.get("error_code") for i in issues if isinstance(i, dict)][:3]
                not_eligible.append(base)
            elif item_id not in with_imps:
                zero_imp.append(base)
    out_items = [x for x in items if not flag or flag in x["flags"]]
    out = compact(out_items, limit=limit_default(limit), sort_by=sort_by,
                  totals_fields=("cost", "clicks", "impressions", "conversions", "conv_value"))
    out.update(account_header(cid, info, dr))
    out["flag_summary"] = {
        "spend_no_conv": {"count": sum(1 for x in items if "spend_no_conv" in x["flags"]),
                          "cost": r(sum(x["cost"] or 0 for x in items if "spend_no_conv" in x["flags"]))},
        "low_roas": sum(1 for x in items if "low_roas" in x["flags"]),
        "zero_impressions": len(zero_imp),
        "not_eligible": len(not_eligible),
    }
    out["waste_threshold"] = {"amount": r(threshold), "basis": basis}
    if include_zero_impression and flag in (None, "zero_impressions"):
        out["zero_impression_products"] = compact(zero_imp, limit=limit_default(limit))
    if include_zero_impression and flag in (None, "not_eligible"):
        out["not_eligible_products"] = compact(not_eligible, limit=limit_default(limit))
    if errors:
        out["section_errors"] = errors
    return out
