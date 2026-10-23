"""Conversion tracking audit."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from core.dates import DateRange, today_in
from core.fmt import r
from gads.queries import conversions as Q
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, ctx, safe

PURCHASE_LIKE = {"PURCHASE", "STORE_SALE", "SUBSCRIBE_PAID"}
LEAD_LIKE = {"SUBMIT_LEAD_FORM", "PHONE_CALL_LEAD", "IMPORTED_LEAD", "BOOK_APPOINTMENT", "REQUEST_QUOTE",
             "CONTACT", "QUALIFIED_LEAD", "CONVERTED_LEAD", "SIGNUP", "MESSAGE_LEAD"}
ALWAYS_MICRO = {"PAGE_VIEW", "ENGAGEMENT", "OUTBOUND_CLICK", "GET_DIRECTIONS", "YOUTUBE_FOLLOW_ON_VIEWS", "DOWNLOAD"}
FUNNEL_MICRO = {"ADD_TO_CART", "BEGIN_CHECKOUT"}
DDA = "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"


def _sev(points: int) -> str:
    return "critical" if points >= 20 else "high" if points >= 10 else "medium" if points >= 5 else "low"


@tool(title="Audit conversion tracking")
def audit_conversion_tracking(customer_id: str, lookback_days: int = 90) -> dict[str, Any]:
    """List conversion actions (primary/secondary, counting, attribution model, last conversion date, 7d/30d volume,
    status) and flag: primary actions with no conversions in 7d, duplicate primary purchase actions, primary
    micro-conversions inflating Smart Bidding, missing conversion values, wrong counting type, non-DDA attribution,
    enhanced conversions off, customer-data terms not accepted, auto-tagging off.
    Consent-mode signals are not exposed by the Google Ads API — check them in Google Tag / GTM.
    Returns a 0-100 score and prioritized fixes."""
    g, cid, info, _ = ctx(customer_id)
    t = targets(cid)
    errors: dict[str, str] = {}
    today = today_in(info.get("time_zone"))
    dr = DateRange(today - timedelta(days=lookback_days), today - timedelta(days=1), f"LAST_{lookback_days}_DAYS")
    d7 = today - timedelta(days=7)
    d30 = today - timedelta(days=30)

    actions = g.search(cid, Q.CONVERSION_ACTIONS)
    daily = safe("daily", lambda: g.search(cid, Q.daily_by_action(dr)), errors) or []
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"last": None, "all_7d": 0.0, "all_30d": 0.0,
                                                             "conv_30d": 0.0, "value_30d": 0.0, "all_value_30d": 0.0})
    for row in daily:
        rn = row.get("segments.conversion_action") or ""
        aid = rn.split("/")[-1]
        day = row.get("segments.date")
        s = stats[aid]
        if (row.get("metrics.all_conversions") or 0) > 0 and (s["last"] is None or day > s["last"]):
            s["last"] = day
        if day and day >= d30.isoformat():
            s["all_30d"] += row.get("metrics.all_conversions") or 0
            s["conv_30d"] += row.get("metrics.conversions") or 0
            s["value_30d"] += row.get("metrics.conversions_value") or 0
            s["all_value_30d"] += row.get("metrics.all_conversions_value") or 0
        if day and day >= d7.isoformat():
            s["all_7d"] += row.get("metrics.all_conversions") or 0

    business = (t.get("business_type") or "").lower()
    ecommerce = business in {"ecommerce", "e-commerce", "shop", "retail"}
    rows, findings = [], []
    penalty = 0

    def flag(points: int, issue: str, fix: str, action: str | None = None) -> None:
        nonlocal penalty
        penalty += points
        findings.append({"severity": _sev(points), "issue": issue, "fix": fix, "conversion_action": action})

    enabled = [a for a in actions if a["conversion_action.status"] == "ENABLED"]
    primary = [a for a in enabled if a["conversion_action.primary_for_goal"]]
    has_purchase_primary = any(a["conversion_action.category"] in PURCHASE_LIKE for a in primary)

    for a in actions:
        aid = str(a["conversion_action.id"])
        s = stats.get(aid, {})
        cat = a["conversion_action.category"]
        is_primary = bool(a["conversion_action.primary_for_goal"])
        item = {
            "id": aid, "name": a["conversion_action.name"], "status": a["conversion_action.status"],
            "type": a["conversion_action.type"], "category": cat, "origin": a.get("conversion_action.origin"),
            "primary": is_primary, "in_conversions_column": a.get("conversion_action.include_in_conversions_metric"),
            "counting": a["conversion_action.counting_type"],
            "attribution": a.get("conversion_action.attribution_model_settings.attribution_model"),
            "default_value": a.get("conversion_action.value_settings.default_value"),
            "always_default_value": a.get("conversion_action.value_settings.always_use_default_value"),
            "click_lookback_days": a.get("conversion_action.click_through_lookback_window_days"),
            "last_conversion_date": s.get("last"), "all_conv_7d": r(s.get("all_7d", 0)),
            "all_conv_30d": r(s.get("all_30d", 0)), "value_30d": r(s.get("all_value_30d", 0)), "flags": [],
        }
        if item["status"] == "ENABLED":
            if is_primary and not s.get("all_7d"):
                item["flags"].append("no_conversions_7d")
                flag(15 if cat in PURCHASE_LIKE | LEAD_LIKE else 5,
                     f"Primary action '{item['name']}' recorded no conversions in 7 days (last: {item['last_conversion_date'] or 'never in ' + str(lookback_days) + 'd'})",
                     "Check the tag fires (Tag Assistant), consent banner, GTM triggers, recent site changes.", item["name"])
            micro = cat in ALWAYS_MICRO or (cat in FUNNEL_MICRO and (has_purchase_primary or ecommerce)) \
                or (ecommerce and cat in {"DEFAULT"})
            if is_primary and micro:
                item["flags"].append("primary_micro_conversion")
                flag(15, f"Micro-conversion '{item['name']}' ({cat}) is PRIMARY — inflates conversions and misleads Smart Bidding",
                     "Set it to secondary (Goals -> Conversions -> action -> 'Secondary').", item["name"])
            if cat in PURCHASE_LIKE:
                if s.get("all_30d") and not s.get("all_value_30d"):
                    item["flags"].append("missing_value")
                    flag(15, f"Purchase action '{item['name']}' has conversions but no value",
                         "Pass transaction value + currency in the tag (dynamic value) — required for tROAS.", item["name"])
                elif item["always_default_value"]:
                    item["flags"].append("static_value")
                    flag(8, f"Purchase action '{item['name']}' always uses a default value",
                         "Use dynamic transaction values.", item["name"])
                if item["counting"] == "ONE_PER_CLICK":
                    item["flags"].append("purchase_counted_once")
                    flag(8, f"Purchase action '{item['name']}' counts ONE per click", "Use 'Every' counting for purchases.", item["name"])
            if cat in LEAD_LIKE and item["counting"] == "MANY_PER_CLICK" and is_primary:
                item["flags"].append("lead_counted_many")
                flag(6, f"Lead action '{item['name']}' counts EVERY conversion", "Use 'One' counting for leads.", item["name"])
            if is_primary and item["attribution"] and item["attribution"] not in (DDA, "EXTERNAL"):
                item["flags"].append("non_dda_attribution")
                flag(3, f"'{item['name']}' uses {item['attribution']}", "Switch to data-driven attribution.", item["name"])
        rows.append(item)

    primary_purchase = [x for x in rows if x["status"] == "ENABLED" and x["primary"] and x["category"] in PURCHASE_LIKE]
    if len(primary_purchase) > 1:
        names = [x["name"] for x in primary_purchase]
        ga4 = [x for x in primary_purchase if "GOOGLE_ANALYTICS" in (x["type"] or "")]
        flag(20, f"{len(primary_purchase)} PRIMARY purchase actions ({', '.join(names)}) — likely double counting"
             + (" (GA4 import + Google Ads tag)" if ga4 else ""),
             "Keep one primary purchase source (usually the Google Ads tag with enhanced conversions); set others secondary.")
    if not primary:
        flag(25, "No enabled primary conversion actions", "Create / mark the main business conversion as primary.")

    settings = safe("tracking_settings", lambda: g.search(cid, Q.TRACKING_SETTINGS), errors) or [{}]
    st = settings[0] if settings else {}
    tracking = {
        "auto_tagging": st.get("customer.auto_tagging_enabled"),
        "conversion_tracking_status": st.get("customer.conversion_tracking_setting.conversion_tracking_status"),
        "customer_data_terms_accepted": st.get("customer.conversion_tracking_setting.accepted_customer_data_terms"),
        "enhanced_conversions": st.get("customer.conversion_tracking_setting.effective_enablement_setting")
        or st.get("customer.conversion_tracking_setting.enablement_setting"),
        "conversion_customer": st.get("customer.conversion_tracking_setting.google_ads_conversion_customer"),
        "consent_mode": "not exposed by the Google Ads API — verify in Google Tag diagnostics / GTM consent overview",
    }
    if tracking["auto_tagging"] is False:
        flag(20, "Auto-tagging is OFF", "Enable auto-tagging (GCLID) — required for accurate attribution and GA4 import.")
    if tracking["enhanced_conversions"] == "OFF":
        flag(10, "Enhanced conversions are OFF", "Enable enhanced conversions (Google tag / GTM user-provided data).")
    if tracking["customer_data_terms_accepted"] is False:
        flag(5, "Customer data terms not accepted", "Accept them in Goals -> Settings to allow enhanced conversions.")

    goals = safe("goals", lambda: g.search(cid, Q.CONVERSION_GOALS), errors) or []
    biddable_goals = sorted({x["customer_conversion_goal.category"] for x in goals if x.get("customer_conversion_goal.biddable")})

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: sev_order[f["severity"]])
    rows.sort(key=lambda x: (x["status"] != "ENABLED", not x["primary"], -(x["all_conv_30d"] or 0)))
    out = account_header(cid, info)
    out.update({
        "score": max(0, 100 - penalty),
        "findings": findings,
        "conversion_actions": rows[:40],
        "truncated": len(rows) > 40,
        "counts": {"total": len(rows), "enabled": len(enabled), "primary": len(primary)},
        "account_default_goals_biddable": biddable_goals,
        "tracking_settings": tracking,
        "business_type_used": business or "unknown (set business_type in the brief for sharper micro-conversion checks)",
    })
    if errors:
        out["section_errors"] = errors
    return out
