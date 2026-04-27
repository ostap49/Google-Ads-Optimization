"""Ad analyzer: low-performing ads and missing responsive search ads."""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from typing import Any, Dict, List

from .base_analyzer import BaseAnalyzer
from ..recommendations.recommendation import (
    Recommendation,
    RecommendationPriority,
    RecommendationType,
)

logger = logging.getLogger(__name__)

# CTR difference threshold: if an ad's CTR is this far below the ad group average
CTR_BELOW_AVERAGE_THRESHOLD = 0.30  # 30% below the mean
MIN_IMPRESSIONS_FOR_SIGNIFICANCE = 200  # Need enough impressions to judge


class AdAnalyzer(BaseAnalyzer):
    """Analyzes ads for performance issues and missing RSA recommendations.

    Checks performed:
    1. Ads with CTR significantly below their ad group average → pause
    2. Ad groups with no responsive search ads → add RSA
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        ctr_below_average_threshold: float = CTR_BELOW_AVERAGE_THRESHOLD,
        min_impressions: int = MIN_IMPRESSIONS_FOR_SIGNIFICANCE,
    ):
        super().__init__(customer_id, customer_name)
        self.ctr_below_average_threshold = ctr_below_average_threshold
        self.min_impressions = min_impressions

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run ad analysis and return recommendations."""
        ads: List[Dict[str, Any]] = data.get("ads", [])
        recs: List[Recommendation] = []

        if not ads:
            return recs

        recs.extend(self._check_low_ctr_ads(ads))
        recs.extend(self._check_missing_rsa(ads))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_low_ctr_ads(self, ads: List[Dict[str, Any]]) -> List[Recommendation]:
        """Pause ads with CTR significantly below their ad group average."""
        # Group ads by ad group
        ag_ads: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for ad in ads:
            if ad.get("status") == "ENABLED" and ad.get("impressions", 0) > 0:
                ag_ads[ad["ad_group_id"]].append(ad)

        recs = []
        for ag_id, group_ads in ag_ads.items():
            # Need at least 2 ads to compare
            if len(group_ads) < 2:
                continue

            # Filter to ads with enough impressions for statistical significance
            significant_ads = [
                a for a in group_ads if a.get("impressions", 0) >= self.min_impressions
            ]
            if len(significant_ads) < 2:
                continue

            avg_ctr = statistics.mean(a["ctr"] for a in significant_ads)
            if avg_ctr <= 0:
                continue

            for ad in significant_ads:
                ad_ctr = ad.get("ctr", 0.0)
                ctr_deficit = (avg_ctr - ad_ctr) / avg_ctr if avg_ctr > 0 else 0

                if ctr_deficit < self.ctr_below_average_threshold:
                    continue

                impact = round(
                    min(10.0, ctr_deficit * 10 + ad.get("impressions", 0) / 1000), 1
                )

                headline_preview = ad.get("headline", "")[:60] or f"Ad ID {ad['ad_id']}"

                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.AD_PAUSE_LOW_PERFORMER,
                        priority=RecommendationPriority.MEDIUM,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=ad.get("campaign_id"),
                        campaign_name=ad.get("campaign_name"),
                        ad_group_id=ag_id,
                        ad_group_name=ad.get("ad_group_name"),
                        ad_id=ad.get("ad_id"),
                        title=f"Pause Low-CTR Ad: '{headline_preview[:40]}'",
                        description=(
                            f"Ad '{headline_preview}' in ad group '{ad.get('ad_group_name')}' "
                            f"has a CTR of {ad_ctr*100:.2f}%, which is {ctr_deficit*100:.0f}% "
                            f"below the ad group average of {avg_ctr*100:.2f}%. "
                            f"({ad.get('impressions', 0):,} impressions)"
                        ),
                        rationale=(
                            "Pausing underperforming ads allows budget to flow to better "
                            "performers and improves the ad group's overall Quality Score."
                        ),
                        impact_score=impact,
                        change_data={
                            "action": "pause_ad",
                            "ad_id": ad.get("ad_id"),
                            "ad_group_id": ag_id,
                            "current_ctr": ad_ctr,
                            "ad_group_avg_ctr": avg_ctr,
                            "ctr_deficit_pct": round(ctr_deficit * 100, 1),
                            "ad_type": ad.get("ad_type"),
                            "headline": ad.get("headline"),
                        },
                    )
                )

        logger.info(
            "Found %d low-CTR ad recommendations for %s", len(recs), self.customer_id
        )
        return recs

    def _check_missing_rsa(self, ads: List[Dict[str, Any]]) -> List[Recommendation]:
        """Recommend adding responsive search ads to ad groups that lack them."""
        # Find ad groups that have ads but no RESPONSIVE_SEARCH_AD
        ag_has_rsa: Dict[str, bool] = defaultdict(bool)
        ag_info: Dict[str, Dict[str, Any]] = {}

        for ad in ads:
            if ad.get("status") == "REMOVED":
                continue
            ag_id = ad["ad_group_id"]
            if ad.get("ad_type") == "RESPONSIVE_SEARCH_AD" and ad.get("status") == "ENABLED":
                ag_has_rsa[ag_id] = True
            # Store first ad info for context
            if ag_id not in ag_info:
                ag_info[ag_id] = ad

        recs = []
        # Ad groups that have ads (appear in ag_info) but no RSA
        for ag_id, info in ag_info.items():
            if ag_has_rsa.get(ag_id):
                continue

            # Only flag if the ad group has some activity
            total_impressions = sum(
                a.get("impressions", 0) for a in ads if a.get("ad_group_id") == ag_id
            )
            if total_impressions < 100:
                continue

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.AD_ADD_RESPONSIVE,
                    priority=RecommendationPriority.MEDIUM,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=info.get("campaign_id"),
                    campaign_name=info.get("campaign_name"),
                    ad_group_id=ag_id,
                    ad_group_name=info.get("ad_group_name"),
                    title=f"Add RSA: '{info.get('ad_group_name', ag_id)}'",
                    description=(
                        f"Ad group '{info.get('ad_group_name')}' in campaign "
                        f"'{info.get('campaign_name')}' has no Responsive Search Ad. "
                        f"RSAs allow Google to test different headline/description combinations "
                        f"to find what works best."
                    ),
                    rationale=(
                        "Responsive Search Ads improve ad relevance by automatically "
                        "combining headlines and descriptions. Google recommends at least "
                        "one RSA per ad group."
                    ),
                    impact_score=5.0,
                    change_data={
                        "action": "add_responsive_search_ad",
                        "ad_group_id": ag_id,
                        "ad_group_name": info.get("ad_group_name"),
                        "campaign_id": info.get("campaign_id"),
                        "total_impressions": total_impressions,
                    },
                )
            )

        logger.info(
            "Found %d missing RSA recommendations for %s", len(recs), self.customer_id
        )
        return recs
