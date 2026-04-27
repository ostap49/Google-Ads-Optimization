"""Budget analyzer: limited campaigns and reallocation opportunities."""

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

# Default thresholds
BUDGET_LOST_IS_THRESHOLD = 0.10  # 10% lost to budget signals a limited campaign
HIGH_ROAS_THRESHOLD = 2.0        # ROAS above this = "high performer"
LOW_ROAS_THRESHOLD = 0.5         # ROAS below this = "low performer"


class BudgetAnalyzer(BaseAnalyzer):
    """Analyzes campaign budgets for constraint and reallocation issues.

    Checks performed:
    1. Campaigns limited by budget (search budget lost IS > threshold)
    2. Budget reallocation: move budget from low-ROAS to high-ROAS campaigns
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        budget_lost_is_threshold: float = BUDGET_LOST_IS_THRESHOLD,
        high_roas_threshold: float = HIGH_ROAS_THRESHOLD,
        low_roas_threshold: float = LOW_ROAS_THRESHOLD,
    ):
        super().__init__(customer_id, customer_name)
        self.budget_lost_is_threshold = budget_lost_is_threshold
        self.high_roas_threshold = high_roas_threshold
        self.low_roas_threshold = low_roas_threshold

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run budget analysis and return recommendations."""
        campaigns: List[Dict[str, Any]] = data.get("campaigns", [])
        recs: List[Recommendation] = []

        if not campaigns:
            return recs

        recs.extend(self._check_budget_limited(campaigns))
        recs.extend(self._check_budget_reallocation(campaigns))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_budget_limited(
        self, campaigns: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Flag campaigns losing significant impression share to budget constraints."""
        recs = []
        for camp in campaigns:
            budget_lost_is = camp.get("budget_lost_is", 0.0)
            cost = camp.get("cost", 0.0)
            roas = camp.get("roas", 0.0)
            budget_amount = camp.get("budget_amount", 0.0)
            recommended_budget = camp.get("recommended_budget")

            if camp.get("status") not in ("ENABLED", None):
                continue

            if budget_lost_is < self.budget_lost_is_threshold:
                continue

            if cost < 1.0:  # Not enough spend to matter
                continue

            priority = (
                RecommendationPriority.HIGH
                if budget_lost_is > 0.30
                else RecommendationPriority.MEDIUM
            )

            # Estimate potential if budget constraint removed
            estimated_missed_conversions = None
            if camp.get("conversions", 0) > 0 and budget_lost_is > 0:
                conversion_rate = camp["conversions"] / max(camp.get("clicks", 1), 1)
                estimated_extra_clicks = camp.get("clicks", 0) * (budget_lost_is / (1 - budget_lost_is))
                estimated_missed_conversions = round(estimated_extra_clicks * conversion_rate, 1)

            budget_desc = f"${budget_amount:.2f}/day"
            rec_budget_desc = (
                f" Google recommends ${recommended_budget:.2f}/day."
                if recommended_budget and recommended_budget > budget_amount
                else ""
            )

            impact = round(min(10.0, budget_lost_is * 20 + roas * 2), 1)

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.BUDGET_LIMITED,
                    priority=priority,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=camp.get("id"),
                    campaign_name=camp.get("name"),
                    budget_id=camp.get("budget_id"),
                    title=f"Budget Limited: '{camp['name']}' ({budget_lost_is*100:.0f}% IS lost)",
                    description=(
                        f"Campaign '{camp['name']}' is losing {budget_lost_is*100:.1f}% "
                        f"of impression share due to budget constraints. "
                        f"Current budget: {budget_desc}. ROAS: {roas:.2f}.{rec_budget_desc}"
                    ),
                    rationale=(
                        f"Budget-limited campaigns with good ROAS ({roas:.2f}) are leaving "
                        "money on the table. Increasing budget could capture more conversions."
                    ),
                    impact_score=impact,
                    estimated_conversion_delta=estimated_missed_conversions,
                    change_data={
                        "action": "increase_budget",
                        "budget_id": camp.get("budget_id"),
                        "campaign_id": camp.get("id"),
                        "current_budget": budget_amount,
                        "recommended_budget": recommended_budget,
                        "budget_lost_is": budget_lost_is,
                        "current_roas": roas,
                    },
                )
            )

        logger.info(
            "Found %d budget-limited campaigns for %s", len(recs), self.customer_id
        )
        return recs

    def _check_budget_reallocation(
        self, campaigns: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Suggest reallocating budget from low-performing to high-performing campaigns."""
        recs = []

        active_campaigns = [
            c for c in campaigns
            if c.get("status") in ("ENABLED", None) and c.get("cost", 0) > 5.0
        ]

        if len(active_campaigns) < 2:
            return recs

        # Identify high and low performers
        high_performers = [
            c for c in active_campaigns
            if c.get("roas", 0) >= self.high_roas_threshold
            and c.get("budget_lost_is", 0) > self.budget_lost_is_threshold
        ]
        low_performers = [
            c for c in active_campaigns
            if c.get("roas", 0) < self.low_roas_threshold
            and c.get("cost", 0) > 10.0
        ]

        if not high_performers or not low_performers:
            return recs

        # Create paired reallocation recommendations
        # Sort by impact: high performers with most IS lost vs low performers with most spend
        high_performers_sorted = sorted(
            high_performers, key=lambda c: c.get("budget_lost_is", 0), reverse=True
        )
        low_performers_sorted = sorted(
            low_performers, key=lambda c: c.get("cost", 0), reverse=True
        )

        # Limit to top pairings
        for high_camp in high_performers_sorted[:3]:
            for low_camp in low_performers_sorted[:3]:
                transfer_amount = round(
                    min(low_camp.get("budget_amount", 0) * 0.3, 50.0), 2
                )
                if transfer_amount < 1.0:
                    continue

                impact = round(
                    min(10.0, high_camp.get("roas", 0) + low_camp.get("cost", 0) / 20), 1
                )

                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.BUDGET_REALLOCATION,
                        priority=RecommendationPriority.HIGH,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=high_camp.get("id"),
                        campaign_name=high_camp.get("name"),
                        title=(
                            f"Reallocate Budget: '{low_camp['name']}' → '{high_camp['name']}'"
                        ),
                        description=(
                            f"Move ${transfer_amount:.2f}/day from '{low_camp['name']}' "
                            f"(ROAS: {low_camp.get('roas', 0):.2f}) to '{high_camp['name']}' "
                            f"(ROAS: {high_camp.get('roas', 0):.2f}, "
                            f"{high_camp.get('budget_lost_is', 0)*100:.0f}% IS lost to budget)."
                        ),
                        rationale=(
                            "Shifting budget from underperforming campaigns to campaigns "
                            "constrained by budget with high ROAS maximizes overall returns."
                        ),
                        impact_score=impact,
                        change_data={
                            "action": "reallocate_budget",
                            "source_campaign_id": low_camp.get("id"),
                            "source_budget_id": low_camp.get("budget_id"),
                            "source_current_budget": low_camp.get("budget_amount"),
                            "source_new_budget": round(
                                low_camp.get("budget_amount", 0) - transfer_amount, 2
                            ),
                            "target_campaign_id": high_camp.get("id"),
                            "target_budget_id": high_camp.get("budget_id"),
                            "target_current_budget": high_camp.get("budget_amount"),
                            "target_new_budget": round(
                                high_camp.get("budget_amount", 0) + transfer_amount, 2
                            ),
                            "transfer_amount": transfer_amount,
                        },
                    )
                )

        logger.info(
            "Found %d budget reallocation recommendations for %s",
            len(recs),
            self.customer_id,
        )
        return recs
