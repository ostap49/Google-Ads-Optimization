"""GAQL builders: account audit."""
from __future__ import annotations

from core.dates import DateRange

M = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
     "metrics.conversions, metrics.conversions_value")

CAMPAIGN_SETTINGS = """
SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
       campaign.advertising_channel_sub_type, campaign.bidding_strategy_type, campaign.bidding_strategy,
       campaign.network_settings.target_google_search, campaign.network_settings.target_search_network,
       campaign.network_settings.target_content_network,
       campaign.geo_target_type_setting.positive_geo_target_type,
       campaign.geo_target_type_setting.negative_geo_target_type,
       campaign.maximize_conversions.target_cpa_micros, campaign.maximize_conversion_value.target_roas,
       campaign.target_cpa.target_cpa_micros, campaign.target_roas.target_roas,
       campaign_budget.amount_micros, campaign.primary_status, campaign.primary_status_reasons
FROM campaign
WHERE campaign.status = 'ENABLED'
"""

AD_GROUPS = """
SELECT campaign.id, campaign.status, ad_group.id, ad_group.name, ad_group.status, ad_group.type
FROM ad_group
WHERE ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'
"""

KEYWORDS = """
SELECT campaign.id, ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
       ad_group_criterion.keyword.match_type, ad_group_criterion.status, ad_group_criterion.negative,
       ad_group_criterion.type, ad_group.status, campaign.status
FROM ad_group_criterion
WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.negative = FALSE
  AND ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'
"""

ADS = """
SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type,
       ad_group_ad.status, ad_group_ad.ad_strength, ad_group_ad.ad.responsive_search_ad.headlines,
       ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.final_urls,
       ad_group_ad.policy_summary.approval_status, ad_group_ad.policy_summary.review_status,
       ad_group_ad.policy_summary.policy_topic_entries, ad_group.status, campaign.status
FROM ad_group_ad
WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'
"""

CAMPAIGN_ASSETS = """
SELECT campaign.id, campaign_asset.field_type, campaign_asset.status, campaign.status
FROM campaign_asset
WHERE campaign_asset.status = 'ENABLED' AND campaign.status = 'ENABLED'
"""

CUSTOMER_ASSETS = """
SELECT customer_asset.field_type, customer_asset.status
FROM customer_asset
WHERE customer_asset.status = 'ENABLED'
"""

AUTO_APPLY = """
SELECT recommendation_subscription.type, recommendation_subscription.status,
       recommendation_subscription.modify_date_time
FROM recommendation_subscription
"""

CUSTOMER = """
SELECT customer.id, customer.optimization_score, customer.auto_tagging_enabled FROM customer
"""


def campaign_metrics(dr: DateRange) -> str:
    return f"SELECT campaign.id, {M} FROM campaign WHERE {dr.gaql()} AND campaign.status = 'ENABLED'"


def match_type_metrics(dr: DateRange) -> str:
    return (f"SELECT campaign.id, ad_group_criterion.keyword.match_type, {M} FROM keyword_view "
            f"WHERE {dr.gaql()} AND campaign.status = 'ENABLED'")
