"""Full structured account audit with a score and prioritized fixes."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from core.fmt import from_micros, r
from gads.queries import audit as Q
from gads.queries import performance as PQ
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, agg, ctx, is_metrics, m, safe

SEVERITY_POINTS = {"critical": 25, "high": 12, "medium": 6, "low": 2}
SECTION_WEIGHTS = {
    "conversion_tracking": 0.20, "bidding": 0.12, "structure": 0.08, "match_types": 0.07, "negatives": 0.10,
    "ads": 0.12, "assets": 0.08, "settings": 0.12, "auto_apply": 0.04, "policy": 0.05, "brief_rules": 0.02,
}
SMART = {"TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"}
SEARCH_LIKE = {"SEARCH"}


class Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, section: str, severity: str, issue: str, fix: str, entities: list[str] | None = None,
            cost_at_stake: float | None = None) -> None:
        self.items.append({"section": section, "severity": severity, "issue": issue, "fix": fix,
                           "entities": (entities or [])[:10], "entity_count": len(entities or []),
                           "cost_at_stake_30d": r(cost_at_stake) if cost_at_stake else None})

    def score(self, section: str) -> int:
        return max(0, 100 - sum(SEVERITY_POINTS[f["severity"]] for f in self.items if f["section"] == section))


@tool(title="Full account audit")
def account_audit(customer_id: str, date_range: str = "LAST_30_DAYS", include_conversion_audit: bool = True) -> dict[str, Any]:
    """Structured audit: structure, match types, negatives coverage, RSA ad strength + pinning, asset coverage
    (sitelinks/callouts/snippets/images), bid strategy vs conversion volume, location targeting (presence vs
    interest), Search network settings (partners / display expansion), auto-applied recommendations, policy
    disapprovals, conversion tracking, brief rules (competitor keywords, brand IS).
    Returns per-section scores (0-100), an overall weighted score, and prioritized fixes."""
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    F = Findings()
    errors: dict[str, str] = {}

    camps = {str(x["campaign.id"]): x for x in g.search(cid, Q.CAMPAIGN_SETTINGS)}
    cm = {str(x["campaign.id"]): m(x) for x in (safe("campaign_metrics", lambda: g.search(cid, Q.campaign_metrics(dr)), errors) or [])}
    zero = m({})

    def cost(cid_: str) -> float:
        return cm.get(cid_, zero)["cost"] or 0

    search_camps = {k: v for k, v in camps.items() if v["campaign.advertising_channel_type"] in SEARCH_LIKE}

    # ---- structure
    ags = safe("ad_groups", lambda: g.search(cid, Q.AD_GROUPS), errors) or []
    kws = safe("keywords", lambda: g.search(cid, Q.KEYWORDS), errors) or []
    ads = safe("ads", lambda: g.search(cid, Q.ADS), errors) or []
    kw_per_ag = Counter(str(k["ad_group.id"]) for k in kws)
    ads_per_ag = Counter(str(a["ad_group.id"]) for a in ads)
    ag_names = {str(a["ad_group.id"]): (str(a["campaign.id"]), a["ad_group.name"], a["ad_group.type"]) for a in ags}
    search_ags = [k for k, v in ag_names.items() if v[0] in search_camps and v[2] == "SEARCH_STANDARD"]
    no_ads = [ag_names[k][1] for k in ag_names if ads_per_ag.get(k, 0) == 0 and ag_names[k][2] not in (None, "UNSPECIFIED")
              and ag_names[k][0] in camps and camps[ag_names[k][0]]["campaign.advertising_channel_type"] in ("SEARCH", "DISPLAY", "DEMAND_GEN", "VIDEO")]
    no_kw = [ag_names[k][1] for k in search_ags if kw_per_ag.get(k, 0) == 0]
    bloated = [f"{ag_names[k][1]} ({kw_per_ag[k]})" for k in search_ags if kw_per_ag.get(k, 0) > 30]
    if no_ads:
        F.add("structure", "high", f"{len(no_ads)} enabled ad groups without enabled ads", "Add an RSA or pause the ad group.", no_ads)
    if no_kw:
        F.add("structure", "medium", f"{len(no_kw)} Search ad groups without keywords", "Add keywords or pause.", no_kw)
    if bloated:
        F.add("structure", "low", f"{len(bloated)} ad groups with >30 keywords", "Split by theme for tighter ad relevance.", bloated)
    zero_spend_enabled = [v["campaign.name"] for k, v in camps.items() if cost(k) == 0]
    if zero_spend_enabled:
        F.add("structure", "low", f"{len(zero_spend_enabled)} enabled campaigns with no spend in the period",
              "Check eligibility (budget, bids, disapprovals, targeting) or pause.", zero_spend_enabled)

    # ---- match types
    mt = safe("match_types", lambda: g.search(cid, Q.match_type_metrics(dr)), errors) or []
    by_mt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in mt:
        by_mt[x.get("ad_group_criterion.keyword.match_type") or "?"].append(x)
    mt_table = {k: agg(v) for k, v in by_mt.items()}
    total_kw_cost = sum(v["cost"] or 0 for v in mt_table.values()) or 0
    match_summary = {k: {"cost_share_pct": r((v["cost"] or 0) / total_kw_cost * 100, 1) if total_kw_cost else None,
                         "cpa": v["cpa"], "roas": v["roas"], "conversions": v["conversions"]} for k, v in mt_table.items()}
    broad_manual = [v["campaign.name"] for k, v in search_camps.items()
                    if v["campaign.bidding_strategy_type"] in ("MANUAL_CPC", "MAXIMIZE_CLICKS", "TARGET_SPEND")
                    and any(str(x["campaign.id"]) == k and x.get("ad_group_criterion.keyword.match_type") == "BROAD" for x in kws)]
    if broad_manual:
        F.add("match_types", "high", "Broad match keywords without Smart Bidding", "Use phrase/exact, or switch to tCPA/tROAS first.", broad_manual)
    bm = mt_table.get("BROAD")
    if bm and bm["cost"] and t.get("target_cpa") and bm["cpa"] and bm["cpa"] > 1.5 * float(t["target_cpa"]):
        F.add("match_types", "medium", f"Broad match CPA {bm['cpa']} is >1.5x target", "Mine search terms; tighten or add negatives.",
              cost_at_stake=bm["cost"])

    # ---- negatives
    from tools.search_terms import load_negatives
    negs = safe("negatives", lambda: load_negatives(g, cid), errors)
    no_negs = []
    if negs:
        for k, v in search_camps.items():
            n = len(negs["campaign"].get(k, [])) + len(negs["account"])
            if n < 5:
                no_negs.append(v["campaign.name"])
    if no_negs:
        F.add("negatives", "high", f"{len(no_negs)} Search campaigns with <5 negatives (incl. shared/account lists)",
              "Build a shared negative list (jobs, free, DIY, competitors...) and attach it.", no_negs,
              sum(cost(k) for k, v in search_camps.items() if v["campaign.name"] in no_negs))
    if negs is not None and not negs["account"] and any(v["campaign.advertising_channel_type"] == "PERFORMANCE_MAX" for v in camps.values()):
        F.add("negatives", "medium", "No account-level negative keyword list while PMax runs",
              "Add an account-level negative list (applies to PMax search inventory).")

    # ---- ads / RSA
    rsa = [a for a in ads if a.get("ad_group_ad.ad.type") == "RESPONSIVE_SEARCH_AD"]
    strength = Counter(a.get("ad_group_ad.ad_strength") or "UNKNOWN" for a in rsa)
    poor = [f"{a['campaign.name']} / {a['ad_group.name']}" for a in rsa if a.get("ad_group_ad.ad_strength") in ("POOR", "AVERAGE")]
    few_headlines, over_pinned = [], []
    for a in rsa:
        hl = a.get("ad_group_ad.ad.responsive_search_ad.headlines") or []
        pinned = [h for h in hl if isinstance(h, dict) and h.get("pinned_field") not in (None, "UNSPECIFIED", 0)]
        label = f"{a['campaign.name']} / {a['ad_group.name']}"
        if len(hl) < 10:
            few_headlines.append(f"{label} ({len(hl)})")
        if len(pinned) >= 3:
            over_pinned.append(f"{label} ({len(pinned)} pinned)")
    rsa_per_ag = Counter(str(a["ad_group.id"]) for a in rsa)
    no_rsa = [ag_names[k][1] for k in search_ags if rsa_per_ag.get(k, 0) == 0]
    if poor:
        F.add("ads", "high" if len(poor) > max(3, len(rsa) * 0.3) else "medium",
              f"{len(poor)} RSAs with POOR/AVERAGE ad strength", "Add unique headlines (15) / descriptions (4), include keywords.", poor)
    if few_headlines:
        F.add("ads", "medium", f"{len(few_headlines)} RSAs with fewer than 10 headlines", "Fill to 15 headlines.", few_headlines)
    if over_pinned:
        F.add("ads", "low", f"{len(over_pinned)} RSAs with heavy pinning (>=3 headlines pinned)",
              "Pin only what's legally/brand required; pin 2-3 variants per position if you must.", over_pinned)
    if no_rsa:
        F.add("ads", "high", f"{len(no_rsa)} Search ad groups without an RSA", "Create an RSA (propose_create_rsa).", no_rsa)

    # ---- policy
    disapproved = [f"{a['campaign.name']} / {a['ad_group.name']} / ad {a['ad_group_ad.ad.id']}" for a in ads
                   if a.get("ad_group_ad.policy_summary.approval_status") == "DISAPPROVED"]
    limited = [f"{a['campaign.name']} / {a['ad_group.name']} / ad {a['ad_group_ad.ad.id']}" for a in ads
               if a.get("ad_group_ad.policy_summary.approval_status") == "APPROVED_LIMITED"]
    topics = Counter()
    for a in ads:
        if a.get("ad_group_ad.policy_summary.approval_status") in ("DISAPPROVED", "APPROVED_LIMITED"):
            for e in a.get("ad_group_ad.policy_summary.policy_topic_entries") or []:
                if isinstance(e, dict):
                    topics[e.get("topic")] += 1
    if disapproved:
        F.add("policy", "critical" if len(disapproved) > 5 else "high", f"{len(disapproved)} disapproved ads",
              f"Fix policy issues ({', '.join(f'{k}: {v}' for k, v in topics.most_common(3))}) and resubmit.", disapproved)
    if limited:
        F.add("policy", "low", f"{len(limited)} ads approved (limited)", "Review limited topics (trademarks, health...).", limited)

    # ---- assets
    ca = safe("campaign_assets", lambda: g.search(cid, Q.CAMPAIGN_ASSETS), errors) or []
    cu = safe("customer_assets", lambda: g.search(cid, Q.CUSTOMER_ASSETS), errors) or []
    acct_types = Counter(x["customer_asset.field_type"] for x in cu)
    per_camp: dict[str, Counter] = defaultdict(Counter)
    for x in ca:
        per_camp[str(x["campaign.id"])][x["campaign_asset.field_type"]] += 1
    asset_gaps: dict[str, list[str]] = defaultdict(list)
    for k, v in search_camps.items():
        have = per_camp.get(k, Counter()) + acct_types
        for ft, need in (("SITELINK", 4), ("CALLOUT", 4), ("STRUCTURED_SNIPPET", 1), ("AD_IMAGE", 1)):
            if have.get(ft, 0) < need:
                asset_gaps[ft].append(v["campaign.name"])
    for ft, names in asset_gaps.items():
        sev = "high" if ft == "SITELINK" else "medium" if ft in ("CALLOUT", "STRUCTURED_SNIPPET") else "low"
        F.add("assets", sev, f"{len(names)} Search campaigns with too few {ft.lower()} assets",
              f"Add {ft.lower()}s at campaign or account level (propose_create_assets).", names)

    # ---- bidding vs data volume
    for k, v in camps.items():
        bst = v["campaign.bidding_strategy_type"]
        conv = cm.get(k, zero)["conversions"] or 0
        name = v["campaign.name"]
        has_target = bool(v.get("campaign.maximize_conversions.target_cpa_micros") or v.get("campaign.target_cpa.target_cpa_micros"))
        has_troas = bool(v.get("campaign.maximize_conversion_value.target_roas") or v.get("campaign.target_roas.target_roas"))
        if bst in SMART and (has_target and conv < 15):
            F.add("bidding", "high", f"'{name}': tCPA with only {r(conv)} conversions / period", "Use Maximize Conversions without target, or consolidate campaigns.", [name], cost(k))
        if bst in SMART and (has_troas and conv < 30):
            F.add("bidding", "medium", f"'{name}': tROAS with {r(conv)} conversions / period", "tROAS needs ~30+ conversions/30d; consider Max Conv Value without target.", [name], cost(k))
        if bst in ("MANUAL_CPC", "ENHANCED_CPC") and conv >= 30:
            F.add("bidding", "medium", f"'{name}': manual CPC with {r(conv)} conversions", "Enough data for Smart Bidding — test Max Conversions / tCPA.", [name])
        if bst in ("TARGET_SPEND", "MAXIMIZE_CLICKS") and conv >= 15:
            F.add("bidding", "medium", f"'{name}': Maximize Clicks despite {r(conv)} conversions", "Move to conversion-based bidding.", [name])
        if t.get("target_cpa"):
            tcpa = from_micros(v.get("campaign.maximize_conversions.target_cpa_micros") or v.get("campaign.target_cpa.target_cpa_micros"))
            if tcpa and tcpa > 1.3 * float(t["target_cpa"]):
                F.add("bidding", "low", f"'{name}': campaign tCPA {tcpa} is >30% above brief target {t['target_cpa']}", "Align targets with the brief.", [name])

    # ---- settings
    interest = [v["campaign.name"] for v in camps.values()
                if v.get("campaign.geo_target_type_setting.positive_geo_target_type") == "PRESENCE_OR_INTEREST"
                and v["campaign.advertising_channel_type"] in ("SEARCH", "DISPLAY", "SHOPPING", "PERFORMANCE_MAX", "DEMAND_GEN")]
    if interest:
        F.add("settings", "high", f"{len(interest)} campaigns target 'Presence or interest'",
              "Switch to 'Presence: people in or regularly in your locations' (huge leak for UA/US local targeting).", interest,
              sum(cost(k) for k, v in camps.items() if v["campaign.name"] in interest))
    display_exp = [v["campaign.name"] for v in search_camps.values() if v.get("campaign.network_settings.target_content_network")]
    partners = [v["campaign.name"] for v in search_camps.values() if v.get("campaign.network_settings.target_search_network")]
    if display_exp:
        F.add("settings", "high", f"{len(display_exp)} Search campaigns with Display Network expansion ON", "Turn off Display expansion.", display_exp)
    if partners:
        F.add("settings", "medium", f"{len(partners)} Search campaigns with Search Partners ON", "Check partner performance (segment by network); usually off.", partners)
    cust = (safe("customer", lambda: g.search(cid, Q.CUSTOMER), errors) or [{}])[0]
    if cust.get("customer.auto_tagging_enabled") is False:
        F.add("settings", "critical", "Auto-tagging is OFF", "Enable auto-tagging.")

    # ---- auto-apply
    subs = safe("auto_apply", lambda: g.search(cid, Q.AUTO_APPLY), errors) or []
    enabled_subs = [x["recommendation_subscription.type"] for x in subs if x.get("recommendation_subscription.status") == "ENABLED"]
    risky = {"USE_BROAD_MATCH_KEYWORD", "KEYWORD", "SEARCH_PARTNERS_OPT_IN", "DISPLAY_EXPANSION_OPT_IN", "TARGET_CPA_OPT_IN",
             "MAXIMIZE_CONVERSIONS_OPT_IN", "CAMPAIGN_BUDGET", "RAISE_TARGET_CPA", "LOWER_TARGET_ROAS", "KEYWORD_MATCH_TYPE"}
    risky_on = [x for x in enabled_subs if x in risky]
    if risky_on:
        F.add("auto_apply", "high", f"Auto-apply ON for {', '.join(risky_on)}", "Turn off auto-apply (Recommendations -> Auto-apply) for these types.", risky_on)
    elif enabled_subs:
        F.add("auto_apply", "low", f"Auto-apply ON for {len(enabled_subs)} types", "Review: " + ", ".join(enabled_subs), enabled_subs)

    # ---- brief rules
    if t["competitor_terms"]:
        comp_kw = [k["ad_group_criterion.keyword.text"] for k in kws
                   if any(c in (k.get("ad_group_criterion.keyword.text") or "").lower() for c in t["competitor_terms"])]
        if comp_kw:
            F.add("brief_rules", "high", f"{len(comp_kw)} active keywords contain competitor terms from the brief",
                  "Check the brief rules — pause if competitor bidding is not allowed.", comp_kw)
    if t.get("brand_impression_share_min"):
        brand_rows = safe("brand_is", lambda: g.search(cid, PQ.campaigns(dr)), errors) or []
        for x in brand_rows:
            if t["brand_campaign_match"] in (x["campaign.name"] or "").lower() and x["campaign.advertising_channel_type"] == "SEARCH":
                is_ = is_metrics(x).get("search_is")
                if is_ is not None and is_ < float(t["brand_impression_share_min"]) * 100:
                    F.add("brief_rules", "high", f"Brand campaign '{x['campaign.name']}' IS {is_}% < {float(t['brand_impression_share_min']) * 100}%",
                          "Raise budget / bids on brand; check lost IS (budget vs rank).", [x["campaign.name"]])

    # ---- conversion tracking
    conv_audit = None
    if include_conversion_audit:
        from tools.conversions import audit_conversion_tracking
        try:
            conv_audit = audit_conversion_tracking.raw(cid)
            for f in conv_audit.get("findings", [])[:8]:
                F.add("conversion_tracking", f["severity"], f["issue"], f["fix"])
        except Exception as ex:  # noqa: BLE001
            errors["conversion_tracking"] = str(ex)[:300]

    sections = {s: F.score(s) for s in SECTION_WEIGHTS}
    if conv_audit:
        sections["conversion_tracking"] = conv_audit["score"]
    overall = round(sum(sections[s] * w for s, w in SECTION_WEIGHTS.items()) / sum(SECTION_WEIGHTS.values()))
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    fixes = sorted(F.items, key=lambda f: (order[f["severity"]], -(f["cost_at_stake_30d"] or 0)))
    out = account_header(cid, info, dr)
    out.update({
        "overall_score": overall,
        "grade": "A" if overall >= 85 else "B" if overall >= 70 else "C" if overall >= 55 else "D" if overall >= 40 else "F",
        "section_scores": sections,
        "prioritized_fixes": fixes[:25],
        "fix_count": len(fixes),
        "truncated": len(fixes) > 25,
        "facts": {
            "enabled_campaigns": len(camps), "search_campaigns": len(search_camps), "enabled_ad_groups": len(ags),
            "enabled_keywords": len(kws), "enabled_ads": len(ads), "rsa_ad_strength": dict(strength),
            "match_type_mix": match_summary, "auto_apply_enabled": enabled_subs,
            "optimization_score": r((cust.get("customer.optimization_score") or 0) * 100, 1) if cust.get("customer.optimization_score") is not None else None,
            "account_kpis": agg([{"metrics.cost_micros": int((v["cost"] or 0) * 1e6), "metrics.clicks": v["clicks"],
                                  "metrics.impressions": v["impressions"], "metrics.conversions": v["conversions"],
                                  "metrics.conversions_value": v["conv_value"]} for v in cm.values()]),
        },
    })
    if errors:
        out["section_errors"] = errors
    return out
