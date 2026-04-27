"""Recommendation engine: aggregates all analyzer outputs."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..analyzers.keyword_analyzer import KeywordAnalyzer
from ..analyzers.bid_analyzer import BidAnalyzer
from ..analyzers.budget_analyzer import BudgetAnalyzer
from ..analyzers.ad_analyzer import AdAnalyzer
from ..analyzers.search_term_analyzer import SearchTermAnalyzer
from ..analyzers.performance_analyzer import PerformanceAnalyzer
from .recommendation import Recommendation, RecommendationBatch, RecommendationStatus

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Runs all analyzers against account data and produces a ranked batch.

    Usage:
        engine = RecommendationEngine()
        batch = engine.analyze_account(customer_id, customer_name, data)
    """

    def __init__(
        self,
        low_qs_threshold: int = 5,
        low_qs_min_cost: float = 5.0,
        negative_min_spend: float = 10.0,
        negative_min_clicks: int = 5,
        budget_lost_is_threshold: float = 0.10,
        low_roas_ratio: float = 0.5,
    ):
        self.low_qs_threshold = low_qs_threshold
        self.low_qs_min_cost = low_qs_min_cost
        self.negative_min_spend = negative_min_spend
        self.negative_min_clicks = negative_min_clicks
        self.budget_lost_is_threshold = budget_lost_is_threshold
        self.low_roas_ratio = low_roas_ratio

    def analyze_account(
        self,
        customer_id: str,
        customer_name: str,
        data: Dict[str, Any],
        date_range: str = "LAST_30_DAYS",
    ) -> List[Recommendation]:
        """Run all analyzers for a single account and return sorted recommendations.

        Args:
            customer_id: Google Ads customer ID (without dashes).
            customer_name: Human-readable account name.
            data: Dict with keys 'campaigns', 'ad_groups', 'keywords',
                  'ads', 'search_terms'.
            date_range: GAQL date range string for context.

        Returns:
            List of :class:`Recommendation` sorted by priority then impact.
        """
        analyzers = [
            KeywordAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
                low_qs_threshold=self.low_qs_threshold,
                low_qs_min_cost=self.low_qs_min_cost,
            ),
            SearchTermAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
                negative_min_spend=self.negative_min_spend,
                negative_min_clicks=self.negative_min_clicks,
            ),
            BidAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
                low_roas_ratio=self.low_roas_ratio,
            ),
            BudgetAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
                budget_lost_is_threshold=self.budget_lost_is_threshold,
            ),
            AdAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
            ),
            PerformanceAnalyzer(
                customer_id=customer_id,
                customer_name=customer_name,
            ),
        ]

        all_recs: List[Recommendation] = []
        for analyzer in analyzers:
            try:
                recs = analyzer.analyze(data)
                all_recs.extend(recs)
                logger.debug(
                    "%s produced %d recommendations for account %s",
                    analyzer.__class__.__name__,
                    len(recs),
                    customer_id,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "Analyzer %s failed for account %s: %s",
                    analyzer.__class__.__name__,
                    customer_id,
                    exc,
                    exc_info=True,
                )

        # Deduplicate and sort
        deduplicated = self._deduplicate(all_recs)
        sorted_recs = sorted(
            deduplicated,
            key=lambda r: (r.priority_order, -r.impact_score),
        )

        logger.info(
            "Generated %d recommendations (%d after dedup) for account %s",
            len(all_recs),
            len(sorted_recs),
            customer_id,
        )
        return sorted_recs

    def analyze_multiple_accounts(
        self,
        accounts_data: List[Dict[str, Any]],
        date_range: str = "LAST_30_DAYS",
    ) -> RecommendationBatch:
        """Run analysis for multiple accounts and return a RecommendationBatch.

        Args:
            accounts_data: List of dicts, each with:
                - 'customer_id': str
                - 'customer_name': str
                - 'data': Dict with campaigns/keywords/ads/search_terms

        Returns:
            :class:`RecommendationBatch` with all recommendations.
        """
        batch = RecommendationBatch(date_range=date_range)

        for account in accounts_data:
            customer_id = account["customer_id"]
            customer_name = account.get("customer_name", customer_id)
            data = account.get("data", {})

            batch.customer_ids.append(customer_id)
            recs = self.analyze_account(customer_id, customer_name, data, date_range)
            batch.recommendations.extend(recs)

        logger.info(
            "Total: %d recommendations across %d accounts",
            len(batch.recommendations),
            len(accounts_data),
        )
        return batch

    # ------------------------------------------------------------------ #
    # Deduplication
    # ------------------------------------------------------------------ #

    @staticmethod
    def _deduplicate(recs: List[Recommendation]) -> List[Recommendation]:
        """Remove duplicate recommendations based on type + entity IDs.

        Two recommendations are considered duplicates if they target the
        same type, customer, and primary entity (criterion/campaign/ad).
        """
        seen: set = set()
        unique: List[Recommendation] = []

        for rec in recs:
            key = (
                rec.rec_type,
                rec.customer_id,
                rec.criterion_id or "",
                rec.campaign_id or "",
                rec.ad_group_id or "",
                rec.ad_id or "",
                rec.budget_id or "",
            )
            if key not in seen:
                seen.add(key)
                unique.append(rec)

        return unique
