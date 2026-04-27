"""Performance analyzer: account-level trend and anomaly detection."""

from __future__ import annotations

import logging
import statistics
from typing import Any, Dict, List

from .base_analyzer import BaseAnalyzer
from ..recommendations.recommendation import (
    Recommendation,
    RecommendationPriority,
    RecommendationType,
)

logger = logging.getLogger(__name__)

# Thresholds for flagging account-level issues
HIGH_ROAS_MIN = 2.0
MIN_COST_FOR_ANALYSIS = 50.0  # Minimum account spend to generate meaningful insights


class PerformanceAnalyzer(BaseAnalyzer):
    """Analyzes account-level performance for trends and anomalies.

    Produces summary-level insights about the account's overall health
    and identifies campaigns that are significantly over- or under-performing.
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        high_roas_min: float = HIGH_ROAS_MIN,
        min_cost: float = MIN_COST_FOR_ANALYSIS,
    ):
        super().__init__(customer_id, customer_name)
        self.high_roas_min = high_roas_min
        self.min_cost = min_cost

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run performance analysis and return recommendations."""
        campaigns: List[Dict[str, Any]] = data.get("campaigns", [])
        keywords: List[Dict[str, Any]] = data.get("keywords", [])
        recs: List[Recommendation] = []

        if not campaigns:
            return recs

        # Compute account totals
        total_cost = sum(c.get("cost", 0.0) for c in campaigns)
        if total_cost < self.min_cost:
            logger.info(
                "Account %s total spend $%.2f below analysis threshold $%.2f",
                self.customer_id,
                total_cost,
                self.min_cost,
            )
            return recs

        recs.extend(self._check_campaign_outliers(campaigns))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_campaign_outliers(
        self, campaigns: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Identify campaigns that are outliers in terms of ROAS vs cost."""
        recs = []

        active_campaigns = [
            c for c in campaigns
            if c.get("status") in ("ENABLED", None)
            and c.get("cost", 0) >= 5.0
        ]

        if len(active_campaigns) < 3:
            return recs

        roas_values = [c.get("roas", 0.0) for c in active_campaigns]
        avg_roas = statistics.mean(roas_values)
        if len(roas_values) >= 2:
            stdev_roas = statistics.stdev(roas_values)
        else:
            return recs

        if stdev_roas == 0:
            return recs

        for camp in active_campaigns:
            roas = camp.get("roas", 0.0)
            cost = camp.get("cost", 0.0)
            z_score = (roas - avg_roas) / stdev_roas

            # Campaign is performing significantly worse than average
            if z_score < -1.5 and cost >= 20.0:
                impact = round(min(10.0, abs(z_score) * 2 + cost / 50), 1)
                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.BID_DECREASE,
                        priority=RecommendationPriority.MEDIUM,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=camp.get("id"),
                        campaign_name=camp.get("name"),
                        title=(
                            f"Underperforming Campaign: '{camp['name']}' "
                            f"(ROAS={roas:.2f} vs avg {avg_roas:.2f})"
                        ),
                        description=(
                            f"Campaign '{camp['name']}' has ROAS of {roas:.2f}, "
                            f"which is {abs(z_score):.1f} standard deviations below "
                            f"the account average of {avg_roas:.2f}. "
                            f"It has spent ${cost:.2f} in the last 30 days. "
                            f"Review keywords, bids, and ad copy for improvement opportunities."
                        ),
                        rationale=(
                            "Campaigns performing significantly below account average "
                            "drag down overall returns. Addressing root causes (poor "
                            "keyword match, irrelevant ads, weak landing pages) will "
                            "improve account health."
                        ),
                        impact_score=impact,
                        change_data={
                            "action": "review_campaign",
                            "campaign_id": camp.get("id"),
                            "current_roas": roas,
                            "account_avg_roas": avg_roas,
                            "z_score": round(z_score, 2),
                            "cost": cost,
                        },
                    )
                )

            # Campaign is performing significantly better — potential budget opportunity
            elif z_score > 1.5 and camp.get("budget_lost_is", 0) > 0.05:
                impact = round(min(10.0, z_score * 2 + camp.get("budget_lost_is", 0) * 10), 1)
                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.BUDGET_LIMITED,
                        priority=RecommendationPriority.HIGH,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=camp.get("id"),
                        campaign_name=camp.get("name"),
                        budget_id=camp.get("budget_id"),
                        title=(
                            f"Top Performer Budget Limited: '{camp['name']}' "
                            f"(ROAS={roas:.2f})"
                        ),
                        description=(
                            f"Campaign '{camp['name']}' has ROAS of {roas:.2f}, "
                            f"which is {z_score:.1f} standard deviations above account average "
                            f"({avg_roas:.2f}). It's losing "
                            f"{camp.get('budget_lost_is', 0)*100:.0f}% IS to budget. "
                            f"Increasing its budget could significantly improve returns."
                        ),
                        rationale=(
                            "Your best-performing campaigns should not be constrained by budget. "
                            "Every impression lost here is a missed conversion opportunity."
                        ),
                        impact_score=impact,
                        change_data={
                            "action": "increase_budget",
                            "budget_id": camp.get("budget_id"),
                            "campaign_id": camp.get("id"),
                            "current_budget": camp.get("budget_amount"),
                            "recommended_budget": camp.get("recommended_budget"),
                            "current_roas": roas,
                            "account_avg_roas": avg_roas,
                        },
                    )
                )

        logger.info(
            "Found %d performance outlier recommendations for %s",
            len(recs),
            self.customer_id,
        )
        return recs
