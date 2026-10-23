"""Google recommendations with an opinionated "should I apply?" verdict. Never auto-applied."""
from __future__ import annotations

from collections import Counter
from typing import Any

from core.fmt import compact, from_micros, r
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, ctx

Q = """
SELECT recommendation.resource_name, recommendation.type, recommendation.campaign, recommendation.ad_group,
       recommendation.dismissed,
       recommendation.impact.base_metrics.cost_micros, recommendation.impact.base_metrics.conversions,
       recommendation.impact.base_metrics.conversions_value, recommendation.impact.base_metrics.clicks,
       recommendation.impact.potential_metrics.cost_micros, recommendation.impact.potential_metrics.conversions,
       recommendation.impact.potential_metrics.conversions_value, recommendation.impact.potential_metrics.clicks,
       recommendation.campaign_budget_recommendation.current_budget_amount_micros,
       recommendation.campaign_budget_recommendation.recommended_budget_amount_micros,
       recommendation.keyword_recommendation.keyword.text, recommendation.keyword_recommendation.keyword.match_type,
       recommendation.keyword_recommendation.recommended_cpc_bid_micros,
       recommendation.target_cpa_opt_in_recommendation.recommended_target_cpa_micros,
       recommendation.set_target_cpa_recommendation.recommended_target_cpa_micros,
       recommendation.forecasting_set_target_roas_recommendation.recommended_target_roas,
       recommendation.raise_target_cpa_recommendation.target_adjustment.recommended_target_multiplier,
       recommendation.lower_target_roas_recommendation.target_adjustment.recommended_target_multiplier,
       recommendation.use_broad_match_keyword_recommendation.keyword.text
FROM recommendation
WHERE recommendation.dismissed = FALSE
"""

ALWAYS_NO = {
    "DISPLAY_EXPANSION_OPT_IN": "Display expansion on Search dilutes quality; control Display in dedicated campaigns.",
    "SEARCH_PARTNERS_OPT_IN": "Search partners rarely match Google Search quality; test only with segmented reporting.",
    "ENHANCED_CPC_OPT_IN": "eCPC is deprecated; use Smart Bidding instead.",
    "MAXIMIZE_CLICKS_OPT_IN": "Optimizes for traffic, not business results.",
    "KEYWORD_MATCH_TYPE": "Blanket match-type loosening loses control; expand manually from search-term data.",
    "TEXT_AD": "Expanded text ads are retired.",
}
OWN_COPY = {"RESPONSIVE_SEARCH_AD", "RESPONSIVE_SEARCH_AD_ASSET", "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH",
            "CALLOUT_ASSET", "SITELINK_ASSET", "CALL_ASSET", "LEAD_FORM_ASSET", "IMPROVE_PERFORMANCE_MAX_AD_STRENGTH",
            "IMPROVE_DEMAND_GEN_AD_STRENGTH"}
FEED_FIXES = {"SHOPPING_ADD_AGE_GROUP", "SHOPPING_ADD_COLOR", "SHOPPING_ADD_GENDER", "SHOPPING_ADD_GTIN",
              "SHOPPING_ADD_MORE_IDENTIFIERS", "SHOPPING_ADD_SIZE", "SHOPPING_FIX_DISAPPROVED_PRODUCTS",
              "SHOPPING_FIX_SUSPENDED_MERCHANT_CENTER_ACCOUNT", "SHOPPING_FIX_MERCHANT_CENTER_ACCOUNT_SUSPENSION_WARNING",
              "IMPROVE_GOOGLE_TAG_COVERAGE"}


def verdict(rec: dict[str, Any], t: dict[str, Any], conv_30d: float | None) -> tuple[str, str]:
    typ = rec["type"]
    if typ in ALWAYS_NO:
        return "no", ALWAYS_NO[typ]
    if typ in FEED_FIXES:
        return "yes_manually", "Fix at the source (Merchant Center feed / tag) — not via apply."
    if typ in OWN_COPY:
        return "yes_own_copy", "Valid gap, but write your own assets (create_rsa / create_assets) instead of Google's auto-text."
    if typ == "USE_BROAD_MATCH_KEYWORD":
        ok = conv_30d is not None and conv_30d >= 50
        return ("maybe", "Only with Smart Bidding, strong conversion volume and solid negatives; test in an experiment.") if ok else \
               ("no", "Not enough conversion data for broad match to be safe.")
    if typ == "KEYWORD":
        kw = (rec.get("keyword") or "").lower()
        if any(c in kw for c in t["competitor_terms"]):
            return "no", "Competitor term — check brief rules."
        return "review", "Check relevance vs search terms and intent before adding."
    if typ in ("CAMPAIGN_BUDGET", "FORECASTING_CAMPAIGN_BUDGET", "MARGINAL_ROI_CAMPAIGN_BUDGET", "RAISE_BUDGET_PERFORMANCE_TOO_LOW"):
        return "maybe", "Raise only if the campaign is at/better than target CPA/ROAS and lost IS (budget) is material; respect monthly budget pacing."
    if typ == "MOVE_UNUSED_BUDGET":
        return "maybe", "Reasonable if the receiving campaign hits targets."
    if typ in ("TARGET_CPA_OPT_IN", "SET_TARGET_CPA", "FORECASTING_SET_TARGET_CPA"):
        rec_t = rec.get("recommended_target_cpa")
        if t.get("target_cpa") and rec_t and rec_t > 1.2 * float(t["target_cpa"]):
            return "no", f"Recommended tCPA {rec_t} is >20% above brief target {t['target_cpa']}."
        if conv_30d is not None and conv_30d < 15:
            return "no", "Fewer than 15 conversions/30d — not enough data for tCPA."
        return "maybe", "OK if conversion tracking is clean (run audit_conversion_tracking first)."
    if typ in ("TARGET_ROAS_OPT_IN", "SET_TARGET_ROAS", "FORECASTING_SET_TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE_OPT_IN"):
        if conv_30d is not None and conv_30d < 30:
            return "no", "Fewer than 30 conversions/30d — value bidding will be unstable."
        return "maybe", "Requires reliable conversion values."
    if typ in ("RAISE_TARGET_CPA", "RAISE_TARGET_CPA_BID_TOO_LOW", "RAISE_TARGET_CPA_PERFORMANCE_BID_TOO_LOW",
               "LOWER_TARGET_ROAS", "LOWER_TARGET_ROAS_PERFORMANCE_BID_TOO_LOW"):
        return "review", "Trades efficiency for volume — compare with margins / brief targets before accepting."
    if typ == "MAXIMIZE_CONVERSIONS_OPT_IN":
        return ("maybe", "Usually fine with clean tracking.") if (conv_30d or 0) >= 15 else ("review", "Low conversion volume.")
    if typ == "OPTIMIZE_AD_ROTATION":
        return "yes", "Optimized rotation is the right default."
    if typ in ("PERFORMANCE_MAX_OPT_IN", "UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX",
               "SHOPPING_MIGRATE_REGULAR_SHOPPING_CAMPAIGN_OFFERS_TO_PERFORMANCE_MAX",
               "MIGRATE_DYNAMIC_SEARCH_ADS_CAMPAIGN_TO_PERFORMANCE_MAX", "PERFORMANCE_MAX_FINAL_URL_OPT_IN"):
        return "review", "Strategic change — evaluate cannibalization, brand exclusions and feed quality; never one-click."
    return "review", "No standing rule; judge against brief targets."


@tool(title="Google recommendations with verdicts")
def get_recommendations(customer_id: str, types: list[str] | None = None, campaign_id: str | None = None,
                        limit: int | None = 40) -> dict[str, Any]:
    """List Google's active recommendations with an opinionated verdict (yes / yes_own_copy / yes_manually /
    maybe / review / no) and the reason, using brief targets and 30-day conversion volume.
    Nothing is applied. To apply one: propose_apply_recommendation(recommendation_id=...)."""
    g, cid, info, _ = ctx(customer_id)
    t = targets(cid)
    q = Q
    if types:
        q += " AND recommendation.type IN (" + ", ".join(f"'{x.upper()}'" for x in types) + ")"
    if campaign_id:
        q += f" AND recommendation.campaign = 'customers/{cid}/campaigns/{int(campaign_id)}'"
    rows = g.search(cid, q)
    camp_rows = g.search(cid, "SELECT campaign.id, campaign.name, metrics.conversions FROM campaign "
                              "WHERE segments.date DURING LAST_30_DAYS AND campaign.status != 'REMOVED'")
    conv = {f"customers/{cid}/campaigns/{x['campaign.id']}": x.get("metrics.conversions") or 0 for x in camp_rows}
    names = {f"customers/{cid}/campaigns/{x['campaign.id']}": x["campaign.name"] for x in camp_rows}
    names.update({f"customers/{cid}/campaigns/{x['campaign.id']}": x["campaign.name"]
                  for x in g.cached_search(cid, "SELECT campaign.id, campaign.name FROM campaign")})
    total_conv = sum(conv.values())
    recs = []
    for x in rows:
        rn = x["recommendation.resource_name"]
        camp = x.get("recommendation.campaign") or ""
        rec = {
            "recommendation_id": rn.split("/")[-1], "resource_name": rn, "type": x["recommendation.type"],
            "campaign": names.get(camp, camp or None),
            "impact": {
                "cost_delta": r((from_micros(x.get("recommendation.impact.potential_metrics.cost_micros")) or 0)
                                - (from_micros(x.get("recommendation.impact.base_metrics.cost_micros")) or 0)),
                "conversions_delta": r((x.get("recommendation.impact.potential_metrics.conversions") or 0)
                                       - (x.get("recommendation.impact.base_metrics.conversions") or 0)),
                "clicks_delta": r((x.get("recommendation.impact.potential_metrics.clicks") or 0)
                                  - (x.get("recommendation.impact.base_metrics.clicks") or 0)),
            },
        }
        details = {
            "current_budget": from_micros(x.get("recommendation.campaign_budget_recommendation.current_budget_amount_micros")),
            "recommended_budget": from_micros(x.get("recommendation.campaign_budget_recommendation.recommended_budget_amount_micros")),
            "keyword": x.get("recommendation.keyword_recommendation.keyword.text") or x.get("recommendation.use_broad_match_keyword_recommendation.keyword.text"),
            "match_type": x.get("recommendation.keyword_recommendation.keyword.match_type"),
            "recommended_cpc": from_micros(x.get("recommendation.keyword_recommendation.recommended_cpc_bid_micros")),
            "recommended_target_cpa": from_micros(x.get("recommendation.target_cpa_opt_in_recommendation.recommended_target_cpa_micros")
                                                  or x.get("recommendation.set_target_cpa_recommendation.recommended_target_cpa_micros")),
            "recommended_target_roas": x.get("recommendation.forecasting_set_target_roas_recommendation.recommended_target_roas"),
            "target_multiplier": x.get("recommendation.raise_target_cpa_recommendation.target_adjustment.recommended_target_multiplier")
            or x.get("recommendation.lower_target_roas_recommendation.target_adjustment.recommended_target_multiplier"),
        }
        rec.update({k: v for k, v in details.items() if v not in (None, 0, "", "UNSPECIFIED")})
        v, why = verdict(rec, t, conv.get(camp, total_conv) if camp else total_conv)
        rec["verdict"], rec["why"] = v, why
        recs.append(rec)
    order = {"yes": 0, "yes_own_copy": 1, "yes_manually": 2, "maybe": 3, "review": 4, "no": 5}
    recs.sort(key=lambda x: (order.get(x["verdict"], 9), -(x["impact"]["conversions_delta"] or 0)))
    out = compact(recs, limit=int(limit or 40))
    out.update(account_header(cid, info))
    out["by_verdict"] = dict(Counter(x["verdict"] for x in recs))
    out["by_type"] = dict(Counter(x["type"] for x in recs).most_common())
    return out
