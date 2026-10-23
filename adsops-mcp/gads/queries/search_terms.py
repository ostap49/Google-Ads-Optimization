"""GAQL builders: search terms + negative keyword inventory."""
from __future__ import annotations

from core.dates import DateRange

M = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
     "metrics.conversions, metrics.conversions_value")


def search_terms(dr: DateRange, campaign_id: str | None = None, ad_group_id: str | None = None) -> str:
    where = [dr.gaql(), "campaign.status != 'REMOVED'"]
    if campaign_id:
        where.append(f"campaign.id = {int(campaign_id)}")
    if ad_group_id:
        where.append(f"ad_group.id = {int(ad_group_id)}")
    return (
        "SELECT search_term_view.search_term, search_term_view.status, campaign.id, campaign.name, "
        "campaign.advertising_channel_type, ad_group.id, ad_group.name, "
        f"{M} FROM search_term_view WHERE " + " AND ".join(where)
    )


def pmax_search_terms(dr: DateRange, campaign_id: str | None = None) -> str:
    """Performance Max (and other campaign-level) search terms, API v23+."""
    where = [dr.gaql(), "campaign.advertising_channel_type = 'PERFORMANCE_MAX'"]
    if campaign_id:
        where.append(f"campaign.id = {int(campaign_id)}")
    return (
        "SELECT campaign_search_term_view.search_term, campaign.id, campaign.name, "
        f"campaign.advertising_channel_type, {M} FROM campaign_search_term_view WHERE " + " AND ".join(where)
    )


CAMPAIGN_NEGATIVES = """
SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.keyword.text,
       campaign_criterion.keyword.match_type, campaign_criterion.negative, campaign_criterion.type
FROM campaign_criterion
WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD' AND campaign.status != 'REMOVED'
"""

AD_GROUP_NEGATIVES = """
SELECT campaign.id, ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
       ad_group_criterion.keyword.match_type, ad_group_criterion.negative, ad_group_criterion.type
FROM ad_group_criterion
WHERE ad_group_criterion.negative = TRUE AND ad_group_criterion.type = 'KEYWORD' AND ad_group.status != 'REMOVED'
  AND campaign.status != 'REMOVED'
"""

CAMPAIGN_SHARED_SETS = """
SELECT campaign.id, campaign_shared_set.shared_set, campaign_shared_set.status, shared_set.id, shared_set.name, shared_set.type
FROM campaign_shared_set
WHERE campaign_shared_set.status = 'ENABLED' AND shared_set.type = 'NEGATIVE_KEYWORDS' AND campaign.status != 'REMOVED'
"""

SHARED_CRITERIA = """
SELECT shared_set.id, shared_set.name, shared_set.type, shared_set.status, shared_criterion.criterion_id,
       shared_criterion.keyword.text, shared_criterion.keyword.match_type, shared_criterion.type
FROM shared_criterion
WHERE shared_set.type IN ('NEGATIVE_KEYWORDS', 'ACCOUNT_LEVEL_NEGATIVE_KEYWORDS') AND shared_set.status = 'ENABLED'
  AND shared_criterion.type = 'KEYWORD'
"""

SHARED_SETS = """
SELECT shared_set.id, shared_set.name, shared_set.type, shared_set.status, shared_set.member_count,
       shared_set.reference_count
FROM shared_set
WHERE shared_set.status = 'ENABLED'
"""
