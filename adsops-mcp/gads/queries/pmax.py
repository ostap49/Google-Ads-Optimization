"""GAQL builders: Performance Max + Shopping products."""
from __future__ import annotations

from core.dates import DateRange

M = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
     "metrics.conversions, metrics.conversions_value")
PMAX = "campaign.advertising_channel_type = 'PERFORMANCE_MAX'"


def _camp(campaign_id: str | None) -> str:
    return f" AND campaign.id = {int(campaign_id)}" if campaign_id else ""


def asset_groups(dr: DateRange, campaign_id: str | None = None) -> str:
    return (
        "SELECT campaign.id, campaign.name, asset_group.id, asset_group.name, asset_group.status, "
        f"asset_group.primary_status, asset_group.ad_strength, {M} FROM asset_group "
        f"WHERE {dr.gaql()} AND {PMAX} AND asset_group.status != 'REMOVED'{_camp(campaign_id)}"
    )


def asset_group_assets(campaign_id: str | None = None, dr: DateRange | None = None) -> str:
    metrics = f", {M}" if dr else ""
    date = f" AND {dr.gaql()}" if dr else ""
    return (
        "SELECT campaign.id, asset_group.id, asset_group.name, asset_group_asset.field_type, "
        "asset_group_asset.status, asset_group_asset.primary_status, asset_group_asset.source, "
        "asset.id, asset.type, asset.name, asset.text_asset.text, asset.youtube_video_asset.youtube_video_id"
        f"{metrics} FROM asset_group_asset WHERE {PMAX} AND asset_group_asset.status != 'REMOVED'"
        f"{_camp(campaign_id)}{date}"
    )


def search_term_insights(dr: DateRange, campaign_id: str) -> str:
    return (
        "SELECT campaign_search_term_insight.category_label, campaign_search_term_insight.id, "
        "metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value "
        f"FROM campaign_search_term_insight WHERE {dr.gaql()} "
        f"AND campaign_search_term_insight.campaign_id = '{int(campaign_id)}'"
    )


def product_performance(dr: DateRange, campaign_id: str | None = None, channel: str | None = "PERFORMANCE_MAX") -> str:
    ch = f" AND campaign.advertising_channel_type = '{channel}'" if channel else ""
    return (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, segments.product_item_id, "
        "segments.product_title, segments.product_brand, segments.product_type_l1, "
        f"segments.product_custom_attribute0, {M} FROM shopping_performance_view WHERE {dr.gaql()}{ch}{_camp(campaign_id)}"
    )


def listing_groups(campaign_id: str | None = None) -> str:
    return (
        "SELECT campaign.id, asset_group.id, asset_group.name, asset_group_listing_group_filter.id, "
        "asset_group_listing_group_filter.type, asset_group_listing_group_filter.listing_source, "
        "asset_group_listing_group_filter.parent_listing_group_filter "
        f"FROM asset_group_listing_group_filter WHERE {PMAX}{_camp(campaign_id)}"
    )


def placements(dr: DateRange, campaign_id: str | None = None) -> str:
    return (
        "SELECT campaign.id, performance_max_placement_view.display_name, performance_max_placement_view.placement, "
        "performance_max_placement_view.placement_type, performance_max_placement_view.target_url, metrics.impressions "
        f"FROM performance_max_placement_view WHERE {dr.gaql()}{_camp(campaign_id)}"
    )


def channel_breakdown(dr: DateRange, campaign_id: str | None = None) -> str:
    return (
        f"SELECT campaign.id, campaign.name, segments.ad_network_type, {M} FROM campaign "
        f"WHERE {dr.gaql()} AND {PMAX}{_camp(campaign_id)}"
    )


SHOPPING_PRODUCTS = (
    "SELECT shopping_product.item_id, shopping_product.title, shopping_product.brand, shopping_product.status, "
    "shopping_product.availability, shopping_product.price_micros, shopping_product.currency_code, "
    "shopping_product.issues, shopping_product.campaign, shopping_product.merchant_center_id "
    "FROM shopping_product"
)
