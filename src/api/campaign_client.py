"""Campaign-level data fetching: keywords, ads, search terms."""

import logging
from typing import List, Dict, Any

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logger = logging.getLogger(__name__)


class CampaignClient:
    """Fetches keyword, ad, and search term data for a customer."""

    KEYWORDS_QUERY = """
        SELECT
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.status,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.post_click_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.cpc_bid_micros,
            ad_group_criterion.effective_cpc_bid_micros,
            ad_group.id,
            ad_group.name,
            ad_group.status,
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.search_impression_share,
            metrics.search_rank_lost_impression_share
        FROM keyword_view
        WHERE
            ad_group_criterion.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
    """

    ADS_QUERY = """
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.type,
            ad_group_ad.ad.final_urls,
            ad_group_ad.ad.expanded_text_ad.headline_part1,
            ad_group_ad.ad.expanded_text_ad.headline_part2,
            ad_group_ad.ad.expanded_text_ad.description,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.status,
            ad_group_ad.policy_summary.approval_status,
            ad_group.id,
            ad_group.name,
            ad_group.status,
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc
        FROM ad_group_ad
        WHERE
            ad_group_ad.status != 'REMOVED'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
    """

    SEARCH_TERMS_QUERY = """
        SELECT
            search_term_view.search_term,
            search_term_view.status,
            ad_group.id,
            ad_group.name,
            ad_group.status,
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM search_term_view
        WHERE
            campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND segments.date DURING LAST_30_DAYS
            AND metrics.clicks > 0
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

    def get_keywords(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """Fetch all active keywords with quality scores and metrics."""
        query = self.KEYWORDS_QUERY.replace("LAST_30_DAYS", date_range)
        try:
            response = self.service.search_stream(
                customer_id=self.customer_id, query=query
            )
            keywords = []
            for batch in response:
                for row in batch.results:
                    crit = row.ad_group_criterion
                    ag = row.ad_group
                    camp = row.campaign
                    m = row.metrics
                    cost = m.cost_micros / 1_000_000
                    roas = m.conversions_value / cost if cost > 0 else 0.0

                    keywords.append(
                        {
                            "criterion_id": str(crit.criterion_id),
                            "text": crit.keyword.text,
                            "match_type": crit.keyword.match_type.name,
                            "status": crit.status.name,
                            "quality_score": crit.quality_info.quality_score or None,
                            "post_click_quality": crit.quality_info.post_click_quality_score.name
                            if crit.quality_info.post_click_quality_score
                            else None,
                            "predicted_ctr_quality": crit.quality_info.search_predicted_ctr.name
                            if crit.quality_info.search_predicted_ctr
                            else None,
                            "creative_quality": crit.quality_info.creative_quality_score.name
                            if crit.quality_info.creative_quality_score
                            else None,
                            "cpc_bid": crit.cpc_bid_micros / 1_000_000
                            if crit.cpc_bid_micros
                            else None,
                            "effective_cpc": crit.effective_cpc_bid_micros / 1_000_000
                            if crit.effective_cpc_bid_micros
                            else None,
                            "ad_group_id": str(ag.id),
                            "ad_group_name": ag.name,
                            "campaign_id": str(camp.id),
                            "campaign_name": camp.name,
                            "impressions": m.impressions,
                            "clicks": m.clicks,
                            "cost": cost,
                            "conversions": m.conversions,
                            "conversion_value": m.conversions_value,
                            "ctr": m.ctr,
                            "avg_cpc": m.average_cpc / 1_000_000 if m.average_cpc else 0.0,
                            "impression_share": m.search_impression_share,
                            "rank_lost_is": m.search_rank_lost_impression_share,
                            "roas": roas,
                        }
                    )
            logger.info(
                "Fetched %d keywords for account %s", len(keywords), self.customer_id
            )
            return keywords
        except GoogleAdsException as exc:
            self._log_exc(exc)
            return []

    def get_ads(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """Fetch all active ads with performance metrics."""
        query = self.ADS_QUERY.replace("LAST_30_DAYS", date_range)
        try:
            response = self.service.search_stream(
                customer_id=self.customer_id, query=query
            )
            ads = []
            for batch in response:
                for row in batch.results:
                    ad_wrapper = row.ad_group_ad
                    ad = ad_wrapper.ad
                    ag = row.ad_group
                    camp = row.campaign
                    m = row.metrics
                    cost = m.cost_micros / 1_000_000

                    # Extract headline/description based on ad type
                    ad_type = ad.type_.name
                    headline = ""
                    description = ""
                    if ad_type == "EXPANDED_TEXT_AD":
                        headline = f"{ad.expanded_text_ad.headline_part1} | {ad.expanded_text_ad.headline_part2}"
                        description = ad.expanded_text_ad.description
                    elif ad_type == "RESPONSIVE_SEARCH_AD":
                        headlines = [h.text for h in ad.responsive_search_ad.headlines]
                        descriptions = [d.text for d in ad.responsive_search_ad.descriptions]
                        headline = " | ".join(headlines[:3]) if headlines else ""
                        description = " | ".join(descriptions[:2]) if descriptions else ""

                    ads.append(
                        {
                            "ad_id": str(ad.id),
                            "ad_type": ad_type,
                            "headline": headline,
                            "description": description,
                            "final_urls": list(ad.final_urls),
                            "status": ad_wrapper.status.name,
                            "approval_status": ad_wrapper.policy_summary.approval_status.name
                            if ad_wrapper.policy_summary
                            else "UNKNOWN",
                            "ad_group_id": str(ag.id),
                            "ad_group_name": ag.name,
                            "campaign_id": str(camp.id),
                            "campaign_name": camp.name,
                            "impressions": m.impressions,
                            "clicks": m.clicks,
                            "cost": cost,
                            "conversions": m.conversions,
                            "ctr": m.ctr,
                            "avg_cpc": m.average_cpc / 1_000_000 if m.average_cpc else 0.0,
                        }
                    )
            logger.info(
                "Fetched %d ads for account %s", len(ads), self.customer_id
            )
            return ads
        except GoogleAdsException as exc:
            self._log_exc(exc)
            return []

    def get_search_terms(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """Fetch search terms report for the account."""
        query = self.SEARCH_TERMS_QUERY.replace("LAST_30_DAYS", date_range)
        try:
            response = self.service.search_stream(
                customer_id=self.customer_id, query=query
            )
            terms = []
            for batch in response:
                for row in batch.results:
                    stv = row.search_term_view
                    ag = row.ad_group
                    camp = row.campaign
                    m = row.metrics
                    cost = m.cost_micros / 1_000_000
                    roas = m.conversions_value / cost if cost > 0 else 0.0

                    terms.append(
                        {
                            "search_term": stv.search_term,
                            "status": stv.status.name,
                            "ad_group_id": str(ag.id),
                            "ad_group_name": ag.name,
                            "campaign_id": str(camp.id),
                            "campaign_name": camp.name,
                            "impressions": m.impressions,
                            "clicks": m.clicks,
                            "cost": cost,
                            "conversions": m.conversions,
                            "conversion_value": m.conversions_value,
                            "ctr": m.ctr,
                            "avg_cpc": m.average_cpc / 1_000_000 if m.average_cpc else 0.0,
                            "roas": roas,
                        }
                    )
            logger.info(
                "Fetched %d search terms for account %s", len(terms), self.customer_id
            )
            return terms
        except GoogleAdsException as exc:
            self._log_exc(exc)
            return []

    @staticmethod
    def _log_exc(exc: GoogleAdsException) -> None:
        logger.error("Google Ads API error: %s", exc.error.code().name)
        for error in exc.failure.errors:
            logger.error("  %s", error.message)
