"""Account-level data fetching client."""

import logging
from typing import List, Dict, Any

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logger = logging.getLogger(__name__)


class AccountClient:
    """Fetches campaign and account-level data for a specific customer."""

    CAMPAIGNS_QUERY = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.bidding_strategy_type,
            campaign.target_cpa.target_cpa_micros,
            campaign.target_roas.target_roas,
            campaign_budget.id,
            campaign_budget.amount_micros,
            campaign_budget.has_recommended_budget,
            campaign_budget.recommended_budget_amount_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.search_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE
            campaign.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
    """

    AD_GROUPS_QUERY = """
        SELECT
            ad_group.id,
            ad_group.name,
            ad_group.status,
            campaign.id,
            campaign.status,
            ad_group.type,
            ad_group.cpc_bid_micros,
            ad_group.target_cpa_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM ad_group
        WHERE
            ad_group.status = 'ENABLED'
            AND campaign.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
    """

    def __init__(self, client: GoogleAdsClient, customer_id: str):
        self.client = client
        self.customer_id = customer_id.replace("-", "")
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = self.client.get_service("GoogleAdsService")
        return self._service

    def get_campaigns(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """Fetch all active campaigns with metrics."""
        query = self.CAMPAIGNS_QUERY.replace("LAST_30_DAYS", date_range)
        try:
            response = self.service.search_stream(
                customer_id=self.customer_id, query=query
            )
            campaigns = []
            for batch in response:
                for row in batch.results:
                    c = row.campaign
                    b = row.campaign_budget
                    m = row.metrics
                    cost = m.cost_micros / 1_000_000
                    roas = (
                        m.conversions_value / cost if cost > 0 else 0.0
                    )
                    campaigns.append(
                        {
                            "id": str(c.id),
                            "name": c.name,
                            "status": c.status.name,
                            "channel_type": c.advertising_channel_type.name,
                            "bidding_strategy": c.bidding_strategy_type.name,
                            "target_cpa": c.target_cpa.target_cpa_micros / 1_000_000
                            if c.target_cpa.target_cpa_micros
                            else None,
                            "target_roas": c.target_roas.target_roas or None,
                            "budget_id": str(b.id),
                            "budget_amount": b.amount_micros / 1_000_000,
                            "has_recommended_budget": b.has_recommended_budget,
                            "recommended_budget": b.recommended_budget_amount_micros / 1_000_000
                            if b.recommended_budget_amount_micros
                            else None,
                            "impressions": m.impressions,
                            "clicks": m.clicks,
                            "cost": cost,
                            "conversions": m.conversions,
                            "conversion_value": m.conversions_value,
                            "ctr": m.ctr,
                            "avg_cpc": m.average_cpc / 1_000_000 if m.average_cpc else 0.0,
                            "search_impression_share": m.search_impression_share,
                            "budget_lost_is": m.search_budget_lost_impression_share,
                            "rank_lost_is": m.search_rank_lost_impression_share,
                            "roas": roas,
                        }
                    )
            return campaigns
        except GoogleAdsException as exc:
            self._log_exc(exc)
            return []

    def get_ad_groups(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """Fetch all active ad groups with metrics."""
        query = self.AD_GROUPS_QUERY.replace("LAST_30_DAYS", date_range)
        try:
            response = self.service.search_stream(
                customer_id=self.customer_id, query=query
            )
            ad_groups = []
            for batch in response:
                for row in batch.results:
                    ag = row.ad_group
                    m = row.metrics
                    cost = m.cost_micros / 1_000_000
                    ad_groups.append(
                        {
                            "id": str(ag.id),
                            "name": ag.name,
                            "status": ag.status.name,
                            "campaign_id": str(row.campaign.id),
                            "type": ag.type_.name,
                            "cpc_bid": ag.cpc_bid_micros / 1_000_000
                            if ag.cpc_bid_micros
                            else None,
                            "target_cpa": ag.target_cpa_micros / 1_000_000
                            if ag.target_cpa_micros
                            else None,
                            "impressions": m.impressions,
                            "clicks": m.clicks,
                            "cost": cost,
                            "conversions": m.conversions,
                            "conversion_value": m.conversions_value,
                            "ctr": m.ctr,
                            "avg_cpc": m.average_cpc / 1_000_000 if m.average_cpc else 0.0,
                        }
                    )
            return ad_groups
        except GoogleAdsException as exc:
            self._log_exc(exc)
            return []

    @staticmethod
    def _log_exc(exc: GoogleAdsException) -> None:
        logger.error("Google Ads API error: %s", exc.error.code().name)
        for error in exc.failure.errors:
            logger.error("  %s", error.message)
