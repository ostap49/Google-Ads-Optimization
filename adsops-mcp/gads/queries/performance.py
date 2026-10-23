"""GAQL builders: account / campaign / ad group / keyword performance."""
from __future__ import annotations

from core.dates import DateRange

M = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
     "metrics.conversions, metrics.conversions_value")
IS = ("metrics.search_impression_share, metrics.search_budget_lost_impression_share, "
      "metrics.search_rank_lost_impression_share")

SEGMENTS = {
    "date": "segments.date",
    "device": "segments.device",
    "network": "segments.ad_network_type",
    "day_of_week": "segments.day_of_week",
    "hour": "segments.hour",
}
# Impression-share metrics cannot be segmented by hour.
IS_COMPATIBLE = {None, "date", "device", "network", "day_of_week"}


def account_totals(dr: DateRange) -> str:
    return f"SELECT {M}, {IS}, metrics.all_conversions FROM customer WHERE {dr.gaql()}"


def account_daily(dr: DateRange) -> str:
    return f"SELECT segments.date, {M} FROM customer WHERE {dr.gaql()}"


def campaigns(dr: DateRange, segment: str | None = None, channel_types: list[str] | None = None,
              campaign_ids: list[str] | None = None, include_paused: bool = True) -> str:
    seg = f", {SEGMENTS[segment]}" if segment else ""
    is_ = f", {IS}" if segment in IS_COMPATIBLE else ""
    where = [dr.gaql(), "campaign.status != 'REMOVED'"]
    if not include_paused:
        where.append("campaign.status = 'ENABLED'")
    if channel_types:
        where.append("campaign.advertising_channel_type IN (" + ", ".join(f"'{c.upper()}'" for c in channel_types) + ")")
    if campaign_ids:
        where.append("campaign.id IN (" + ", ".join(str(int(c)) for c in campaign_ids) + ")")
    return (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign.bidding_strategy_type, campaign_budget.amount_micros, campaign.primary_status"
        f"{seg}, {M}{is_} FROM campaign WHERE " + " AND ".join(where)
    )


def ad_groups(dr: DateRange, campaign_id: str | None = None) -> str:
    where = [dr.gaql(), "ad_group.status != 'REMOVED'", "campaign.status != 'REMOVED'"]
    if campaign_id:
        where.append(f"campaign.id = {int(campaign_id)}")
    return (
        "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
        f"ad_group.cpc_bid_micros, {M}, metrics.search_impression_share FROM ad_group WHERE " + " AND ".join(where)
    )


def keywords(dr: DateRange, campaign_id: str | None = None, ad_group_id: str | None = None) -> str:
    where = [dr.gaql(), "ad_group_criterion.status != 'REMOVED'", "ad_group.status != 'REMOVED'",
             "campaign.status != 'REMOVED'", "ad_group_criterion.negative = FALSE"]
    if campaign_id:
        where.append(f"campaign.id = {int(campaign_id)}")
    if ad_group_id:
        where.append(f"ad_group.id = {int(ad_group_id)}")
    return (
        "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
        "ad_group_criterion.cpc_bid_micros, ad_group_criterion.effective_cpc_bid_micros, "
        "ad_group_criterion.quality_info.quality_score, ad_group_criterion.quality_info.search_predicted_ctr, "
        "ad_group_criterion.quality_info.creative_quality_score, "
        "ad_group_criterion.quality_info.post_click_quality_score, ad_group_criterion.system_serving_status, "
        f"{M}, metrics.search_impression_share FROM keyword_view WHERE " + " AND ".join(where)
    )
