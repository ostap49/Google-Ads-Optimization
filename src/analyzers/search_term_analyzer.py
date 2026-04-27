"""Search term analyzer: negative keyword candidates and keyword opportunities."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from .base_analyzer import BaseAnalyzer
from ..recommendations.recommendation import (
    Recommendation,
    RecommendationPriority,
    RecommendationType,
)

logger = logging.getLogger(__name__)

# Default thresholds
NEGATIVE_KW_MIN_SPEND = 10.0   # USD
NEGATIVE_KW_MIN_CLICKS = 5
OPPORTUNITY_MIN_CONVERSIONS = 1.0
OPPORTUNITY_MIN_CLICKS = 5


class SearchTermAnalyzer(BaseAnalyzer):
    """Analyzes the search terms report for negative kw and kw opportunities.

    Checks performed:
    1. Search terms with high spend and zero conversions → add as negatives
    2. Search terms with conversions not yet in keyword list → add as keywords
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        negative_min_spend: float = NEGATIVE_KW_MIN_SPEND,
        negative_min_clicks: int = NEGATIVE_KW_MIN_CLICKS,
        opportunity_min_conversions: float = OPPORTUNITY_MIN_CONVERSIONS,
        opportunity_min_clicks: int = OPPORTUNITY_MIN_CLICKS,
    ):
        super().__init__(customer_id, customer_name)
        self.negative_min_spend = negative_min_spend
        self.negative_min_clicks = negative_min_clicks
        self.opportunity_min_conversions = opportunity_min_conversions
        self.opportunity_min_clicks = opportunity_min_clicks

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run search term analysis and return recommendations."""
        search_terms: List[Dict[str, Any]] = data.get("search_terms", [])
        keywords: List[Dict[str, Any]] = data.get("keywords", [])
        recs: List[Recommendation] = []

        if not search_terms:
            return recs

        # Build a set of existing keyword texts (lowercased) for lookup
        existing_keywords: Set[str] = {
            kw.get("text", "").lower().strip() for kw in keywords if kw.get("text")
        }

        recs.extend(self._check_negative_candidates(search_terms))
        recs.extend(self._check_keyword_opportunities(search_terms, existing_keywords))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_negative_candidates(
        self, search_terms: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Flag high-spend, zero-conversion search terms as negative candidates."""
        recs = []

        for term in search_terms:
            cost = term.get("cost", 0.0)
            clicks = term.get("clicks", 0)
            conversions = term.get("conversions", 0.0)
            text = term.get("search_term", "").strip()

            if not text:
                continue

            # Must meet minimum thresholds and have zero conversions
            if conversions > 0:
                continue
            if cost < self.negative_min_spend and clicks < self.negative_min_clicks:
                continue

            # Skip if it's already a negative (status == NEGATIVE)
            if term.get("status") == "NEGATIVE":
                continue

            priority = (
                RecommendationPriority.HIGH
                if cost >= 20.0
                else RecommendationPriority.MEDIUM
            )
            impact = round(min(10.0, cost / 5 + clicks / 10), 1)

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.KEYWORD_ADD_NEGATIVE,
                    priority=priority,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=term.get("campaign_id"),
                    campaign_name=term.get("campaign_name"),
                    ad_group_id=term.get("ad_group_id"),
                    ad_group_name=term.get("ad_group_name"),
                    title=f"Add Negative Keyword: '{text}'",
                    description=(
                        f"Search term '{text}' has generated {clicks} clicks and "
                        f"${cost:.2f} in spend with 0 conversions in ad group "
                        f"'{term.get('ad_group_name')}'. "
                        f"Adding it as a negative keyword will stop wasted spend."
                    ),
                    rationale=(
                        f"This search term consumed ${cost:.2f} with no conversions. "
                        "Blocking it as a negative keyword recovers that budget for "
                        "better-performing queries."
                    ),
                    impact_score=impact,
                    estimated_cost_delta=-cost,
                    change_data={
                        "action": "add_negative_keyword",
                        "search_term": text,
                        "campaign_id": term.get("campaign_id"),
                        "ad_group_id": term.get("ad_group_id"),
                        "match_type": "EXACT",  # Add as exact negative by default
                        "cost": cost,
                        "clicks": clicks,
                        "conversions": conversions,
                    },
                )
            )

        logger.info(
            "Found %d negative keyword candidates for %s", len(recs), self.customer_id
        )
        return recs

    def _check_keyword_opportunities(
        self,
        search_terms: List[Dict[str, Any]],
        existing_keywords: Set[str],
    ) -> List[Recommendation]:
        """Flag converting search terms not yet added as keywords."""
        recs = []

        for term in search_terms:
            text = term.get("search_term", "").strip()
            conversions = term.get("conversions", 0.0)
            clicks = term.get("clicks", 0)
            cost = term.get("cost", 0.0)
            roas = term.get("roas", 0.0)

            if not text:
                continue

            # Must have meaningful conversions and clicks
            if conversions < self.opportunity_min_conversions:
                continue
            if clicks < self.opportunity_min_clicks:
                continue

            # Skip if already an exact match keyword
            if text.lower() in existing_keywords:
                continue

            impact = round(
                min(10.0, conversions * 2 + roas * 0.5 + clicks / 10), 1
            )

            cpa = cost / conversions if conversions > 0 else 0.0

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.KEYWORD_OPPORTUNITY,
                    priority=RecommendationPriority.HIGH,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=term.get("campaign_id"),
                    campaign_name=term.get("campaign_name"),
                    ad_group_id=term.get("ad_group_id"),
                    ad_group_name=term.get("ad_group_name"),
                    title=f"Add Keyword Opportunity: '{text}'",
                    description=(
                        f"Search term '{text}' generated {conversions:.1f} conversions "
                        f"with a CPA of ${cpa:.2f} and {clicks} clicks, "
                        f"but is not yet an explicit keyword in ad group "
                        f"'{term.get('ad_group_name')}'. "
                        f"Adding it as an exact-match keyword gives you better control "
                        f"over bids and ad copy."
                    ),
                    rationale=(
                        "Explicitly adding high-converting search terms as keywords lets "
                        "you write dedicated ad copy, set specific bids, and track "
                        "performance independently."
                    ),
                    impact_score=impact,
                    estimated_conversion_delta=conversions * 0.2,  # 20% boost from targeted ad
                    change_data={
                        "action": "add_keyword",
                        "search_term": text,
                        "suggested_keyword_text": text,
                        "match_type": "EXACT",
                        "campaign_id": term.get("campaign_id"),
                        "ad_group_id": term.get("ad_group_id"),
                        "conversions": conversions,
                        "clicks": clicks,
                        "cost": cost,
                        "cpa": cpa,
                        "roas": roas,
                    },
                )
            )

        logger.info(
            "Found %d keyword opportunities for %s", len(recs), self.customer_id
        )
        return recs
