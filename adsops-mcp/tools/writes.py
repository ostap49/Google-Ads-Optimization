"""propose_* tools. Each one only CREATES a validated ChangeProposal (validate_only mutate) —
nothing changes in Google Ads until apply_proposal is called after explicit user approval."""
from __future__ import annotations

import importlib
from typing import Any

from safety import engine
from tools._registry import tool

for _m in ("bidding", "keywords", "status", "ads", "campaigns", "exclusions", "recommendations"):
    try:
        importlib.import_module(f"safety.changes.{_m}")
    except ModuleNotFoundError as ex:  # pragma: no cover - partial installs
        if ex.name != f"safety.changes.{_m}":
            raise


@tool(read_only=False, title="Propose: campaign daily budget")
def propose_update_campaign_budget(customer_id: str, campaign_id: str, new_daily_budget: float | None = None,
                                   change_pct: float | None = None, reason: str | None = None) -> dict[str, Any]:
    """Propose a new daily budget (account currency) or a % change. Guardrails: max_daily_budget and
    max_budget_change_pct (default 30%) per apply. Warns on shared budgets / low lost-IS(budget)."""
    return engine.propose("update_campaign_budget", customer_id,
                          {"campaign_id": campaign_id, "new_daily_budget": new_daily_budget, "change_pct": change_pct}, reason)


@tool(read_only=False, title="Propose: keyword CPC bids")
def propose_update_keyword_bids(customer_id: str, changes: list[dict[str, Any]], reason: str | None = None) -> dict[str, Any]:
    """changes: [{"id": "adGroupId~criterionId" (or ad_group_id + criterion_id), "cpc_bid": 12.5 | "change_pct": -20 | "clear": true}].
    Guardrail: max_bid_change_pct (default 30%) vs the current effective bid."""
    return engine.propose("update_keyword_bids", customer_id, {"changes": changes}, reason)


@tool(read_only=False, title="Propose: bid strategy / targets")
def propose_update_bid_strategy(customer_id: str, campaign_id: str, strategy: str, target_cpa: float | None = None,
                                target_roas: float | None = None, cpc_bid_ceiling: float | None = None,
                                location: str | None = None, fraction: float | None = None,
                                reason: str | None = None) -> dict[str, Any]:
    """strategy: MAXIMIZE_CONVERSIONS (optional target_cpa) | MAXIMIZE_CONVERSION_VALUE (optional target_roas, e.g. 4.0 = 400%)
    | TARGET_CPA | TARGET_ROAS (stored as Max Conv / Max Conv Value with target) | MANUAL_CPC | MAXIMIZE_CLICKS
    (optional cpc_bid_ceiling) | TARGET_IMPRESSION_SHARE (location + fraction 0-1, optional cpc_bid_ceiling).
    Guardrail: max_target_change_pct (default 20%) for tCPA/tROAS changes within the same strategy."""
    return engine.propose("update_bid_strategy", customer_id,
                          {"campaign_id": campaign_id, "strategy": strategy, "target_cpa": target_cpa,
                           "target_roas": target_roas, "cpc_bid_ceiling": cpc_bid_ceiling, "location": location,
                           "fraction": fraction}, reason)


@tool(read_only=False, title="Propose: negative keywords")
def propose_add_negative_keywords(customer_id: str, keywords: list[Any], level: str = "campaign",
                                  campaign_ids: list[str] | None = None, ad_group_ids: list[str] | None = None,
                                  shared_set_id: str | None = None, new_list_name: str | None = None,
                                  attach_campaign_ids: list[str] | None = None, match_type: str = "EXACT",
                                  reason: str | None = None) -> dict[str, Any]:
    """Add negatives. keywords: ["text", "[exact]", "\\"phrase\\"", {"text":..., "match_type":...}].
    level: campaign (campaign_ids) | ad_group (ad_group_ids) | shared_list (shared_set_id, or new_list_name +
    attach_campaign_ids) | account (account-level list — also applies to PMax; created + linked if missing).
    Already-present negatives are skipped. Impact = last-30d search terms these negatives would have blocked."""
    return engine.propose("add_negative_keywords", customer_id,
                          {"keywords": keywords, "level": level, "campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids,
                           "shared_set_id": shared_set_id, "new_list_name": new_list_name,
                           "attach_campaign_ids": attach_campaign_ids, "match_type": match_type}, reason)


@tool(read_only=False, title="Propose: add keywords")
def propose_add_keywords(customer_id: str, ad_group_id: str, keywords: list[Any], match_type: str = "PHRASE",
                         status: str = "ENABLED", reason: str | None = None) -> dict[str, Any]:
    """Add keywords to a Search ad group. keywords: ["text", "[exact]", {"text", "match_type", "cpc_bid", "final_url"}].
    Duplicates are skipped; warns on competitor terms (brief), conflicts with ad-group negatives, bids >3x default."""
    return engine.propose("add_keywords", customer_id,
                          {"ad_group_id": ad_group_id, "keywords": keywords, "match_type": match_type, "status": status}, reason)


@tool(read_only=False, title="Propose: pause / enable")
def propose_set_status(customer_id: str, entity_type: str, ids: list[str], status: str,
                       reason: str | None = None) -> dict[str, Any]:
    """Pause or enable entities. entity_type: campaign | ad_group | ad | keyword | asset_group.
    ids: campaign/ad group/asset group IDs; ads as "adGroupId~adId"; keywords as "adGroupId~criterionId".
    status: PAUSED | ENABLED. (Nothing can be removed through adsops.)"""
    return engine.propose("set_status", customer_id, {"entity_type": entity_type, "ids": ids, "status": status}, reason)


@tool(read_only=False, title="Propose: responsive search ad")
def propose_create_rsa(customer_id: str, ad_group_id: str, headlines: list[Any], descriptions: list[Any],
                       final_url: str, path1: str | None = None, path2: str | None = None, status: str = "PAUSED",
                       require_full: bool = False, reason: str | None = None) -> dict[str, Any]:
    """Create an RSA. headlines: up to 15 (each <=30 chars), descriptions: up to 4 (<=90), paths <=15.
    Items may be strings or {"text": ..., "pin": 1|2|3} (descriptions pin 1|2). Characters are counted the way
    Google does (Cyrillic = 1, wide CJK = 2, {KeyWord:default} counts the default). Also checks policy basics
    (no '!' in headlines, no emoji, no duplicates). Created PAUSED by default."""
    return engine.propose("create_rsa", customer_id,
                          {"ad_group_id": ad_group_id, "headlines": headlines, "descriptions": descriptions,
                           "final_url": final_url, "path1": path1, "path2": path2, "status": status,
                           "require_full": require_full}, reason)


def _assets(customer_id, asset_type, items, level, campaign_ids, ad_group_ids, reason):
    return engine.propose("create_assets", customer_id,
                          {"asset_type": asset_type, "items": items, "level": level,
                           "campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids}, reason)


@tool(read_only=False, title="Propose: sitelinks")
def propose_create_sitelinks(customer_id: str, sitelinks: list[dict[str, Any]], level: str = "campaign",
                             campaign_ids: list[str] | None = None, ad_group_ids: list[str] | None = None,
                             reason: str | None = None) -> dict[str, Any]:
    """sitelinks: [{"text" (<=25), "description1" (<=35), "description2" (<=35), "final_url"}].
    level: account | campaign | ad_group."""
    return _assets(customer_id, "sitelink", sitelinks, level, campaign_ids, ad_group_ids, reason)


@tool(read_only=False, title="Propose: callouts")
def propose_create_callouts(customer_id: str, callouts: list[str], level: str = "campaign",
                            campaign_ids: list[str] | None = None, ad_group_ids: list[str] | None = None,
                            reason: str | None = None) -> dict[str, Any]:
    """callouts: ["Безкоштовна доставка", ...] (<=25 chars each). level: account | campaign | ad_group."""
    return _assets(customer_id, "callout", callouts, level, campaign_ids, ad_group_ids, reason)


@tool(read_only=False, title="Propose: structured snippets")
def propose_create_snippets(customer_id: str, snippets: list[dict[str, Any]], level: str = "campaign",
                            campaign_ids: list[str] | None = None, ad_group_ids: list[str] | None = None,
                            reason: str | None = None) -> dict[str, Any]:
    """snippets: [{"header": "Brands"|"Types"|"Styles"|"Models"|"Service catalog"|..., "values": [3-10 values <=25]}].
    Headers are Google's English names (shown localized)."""
    return _assets(customer_id, "structured_snippet", snippets, level, campaign_ids, ad_group_ids, reason)


@tool(read_only=False, title="Propose: new Search campaign (paused)")
def propose_create_search_campaign(customer_id: str, name: str, daily_budget: float, ad_groups: list[dict[str, Any]],
                                   bidding: dict[str, Any] | None = None, geo: list[str] | None = None,
                                   languages: list[str] | None = None, negative_keywords: list[Any] | None = None,
                                   search_partners: bool = False, display_expansion: bool = False,
                                   presence_only: bool = True, final_url_suffix: str | None = None,
                                   reason: str | None = None) -> dict[str, Any]:
    """Create a complete Search campaign, ALWAYS PAUSED, in one atomic request.
    ad_groups: [{"name", "cpc_bid"?, "keywords": [...], "match_type"?, "rsa": {"headlines", "descriptions", "final_url", "path1", "path2"}}].
    bidding: {"strategy": MAXIMIZE_CONVERSIONS|MAXIMIZE_CONVERSION_VALUE|TARGET_CPA|TARGET_ROAS|MANUAL_CPC|MAXIMIZE_CLICKS,
    "target_cpa"?, "target_roas"?, "cpc_bid_ceiling"?}. geo: ["UA"] / ["Kyiv"] / geo IDs; languages: ["uk","ru"].
    Defaults: Search only (no partners / display), location option PRESENCE."""
    return engine.propose("create_search_campaign", customer_id,
                          {"name": name, "daily_budget": daily_budget, "ad_groups": ad_groups, "bidding": bidding,
                           "geo": geo, "languages": languages, "negative_keywords": negative_keywords,
                           "search_partners": search_partners, "display_expansion": display_expansion,
                           "presence_only": presence_only, "final_url_suffix": final_url_suffix}, reason)


@tool(read_only=False, title="Propose: new Performance Max campaign (paused)")
def propose_create_pmax_campaign(customer_id: str, name: str, daily_budget: float, asset_group: dict[str, Any],
                                 bidding: dict[str, Any] | None = None, geo: list[str] | None = None,
                                 languages: list[str] | None = None, merchant_center_id: str | None = None,
                                 feed_label: str | None = None, audience_signal: dict[str, Any] | None = None,
                                 asset_automation_opt_out: list[str] | None = None,
                                 reason: str | None = None) -> dict[str, Any]:
    """Create a PMax campaign + asset group, ALWAYS PAUSED, in one atomic request.
    asset_group: {"name", "final_url", "business_name" (<=25), "headlines" (3-15, <=30), "long_headlines" (1-5, <=90),
    "descriptions" (2-5, <=90, one <=60), "marketing_images" (1.91:1), "square_images" (1:1), "portrait_images" (4:5),
    "logos" (1:1), "landscape_logos" (4:1) — local paths or URLs, "youtube_video_ids", "path1", "path2"}.
    merchant_center_id (+ feed_label) adds the feed and an all-products listing group.
    audience_signal: {"search_themes": [...], "audience_ids": [...]}.
    asset_automation_opt_out: e.g. ["TEXT_ASSET_AUTOMATION", "GENERATE_IMAGE_ENHANCEMENT", "GENERATE_ENHANCED_YOUTUBE_VIDEOS"].
    bidding: MAXIMIZE_CONVERSION_VALUE (target_roas?) | MAXIMIZE_CONVERSIONS (target_cpa?)."""
    return engine.propose("create_pmax_campaign", customer_id,
                          {"name": name, "daily_budget": daily_budget, "asset_group": asset_group, "bidding": bidding,
                           "geo": geo, "languages": languages, "merchant_center_id": merchant_center_id,
                           "feed_label": feed_label, "audience_signal": audience_signal,
                           "asset_automation_opt_out": asset_automation_opt_out}, reason)


@tool(read_only=False, title="Propose: new Demand Gen campaign (paused)")
def propose_create_demand_gen_campaign(customer_id: str, name: str, daily_budget: float,
                                       bidding: dict[str, Any] | None = None, geo: list[str] | None = None,
                                       languages: list[str] | None = None, ad_group_name: str | None = None,
                                       ad: dict[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
    """Create a Demand Gen campaign + ad group (+ optional multi-asset ad), ALWAYS PAUSED.
    ad: {"headlines" (1-5, <=40), "descriptions" (1-5, <=90), "business_name", "final_url", "call_to_action"?,
    "marketing_images", "square_images", "portrait_images", "logos"}. bidding: MAXIMIZE_CONVERSIONS (target_cpa?) |
    MAXIMIZE_CONVERSION_VALUE (target_roas?) | MAXIMIZE_CLICKS."""
    return engine.propose("create_demand_gen_campaign", customer_id,
                          {"name": name, "daily_budget": daily_budget, "bidding": bidding, "geo": geo,
                           "languages": languages, "ad_group_name": ad_group_name, "ad": ad}, reason)


@tool(read_only=False, title="Propose: exclusions / adjustments")
def propose_add_exclusions(customer_id: str, exclusion_type: str, values: list[Any] | None = None,
                           campaign_ids: list[str] | None = None, level: str = "campaign",
                           bid_modifier: float | None = None, hours: list[int] | None = None,
                           days: list[str] | None = None, mode: str = "exclude", replace_existing: bool = False,
                           reason: str | None = None) -> dict[str, Any]:
    """exclusion_type:
    - placement: values = URLs / YouTube channel (UC...) / video URLs / "mobileapp::1-com.app"; level campaign
      (Display / Demand Gen / Video) or account (covers PMax).
    - location: values = country codes, names ("Kharkiv Oblast") or geo IDs; negative location on campaign_ids.
    - device: values = MOBILE|DESKTOP|TABLET, bid_modifier 0 (= -100%) / 0.5 (-50%) / 1.2 (+20%). Not for PMax/DG.
    - ad_schedule: hours [0-23] on days (default all) with mode exclude (stop serving) or bid_modifier (0.1-10).
      Builds a full-week schedule; existing schedules require replace_existing=True."""
    return engine.propose("add_exclusions", customer_id,
                          {"exclusion_type": exclusion_type, "values": values, "campaign_ids": campaign_ids,
                           "level": level, "bid_modifier": bid_modifier, "hours": hours, "days": days, "mode": mode,
                           "replace_existing": replace_existing}, reason)


@tool(read_only=False, title="Propose: apply a Google recommendation")
def propose_apply_recommendation(customer_id: str, recommendation_ids: list[str], reason: str | None = None) -> dict[str, Any]:
    """Apply specific recommendations by ID (from get_recommendations). Never applied in bulk or automatically.
    Not automatically reversible."""
    return engine.propose("apply_recommendation", customer_id, {"recommendation_ids": recommendation_ids}, reason)
