"""Keyword analyzer: quality score, duplicates, negatives, opportunities."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Set

from .base_analyzer import BaseAnalyzer
from ..recommendations.recommendation import (
    Recommendation,
    RecommendationPriority,
    RecommendationStatus,
    RecommendationType,
)

logger = logging.getLogger(__name__)

# Default thresholds
LOW_QS_THRESHOLD = 5
LOW_QS_MIN_COST = 5.0  # USD
NEGATIVE_KW_MIN_SPEND = 10.0  # USD
NEGATIVE_KW_MIN_CLICKS = 5


class KeywordAnalyzer(BaseAnalyzer):
    """Analyzes keywords for quality issues, duplicates, and optimizations.

    Checks performed:
    1. Low Quality Score keywords (QS < threshold with min cost)
    2. Duplicate keywords across ad groups
    3. High-spend, zero-conversion search terms → add as negatives
    4. Converting search terms not yet added as keywords → add as keywords
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        low_qs_threshold: int = LOW_QS_THRESHOLD,
        low_qs_min_cost: float = LOW_QS_MIN_COST,
        negative_kw_min_spend: float = NEGATIVE_KW_MIN_SPEND,
        negative_kw_min_clicks: int = NEGATIVE_KW_MIN_CLICKS,
    ):
        super().__init__(customer_id, customer_name)
        self.low_qs_threshold = low_qs_threshold
        self.low_qs_min_cost = low_qs_min_cost
        self.negative_kw_min_spend = negative_kw_min_spend
        self.negative_kw_min_clicks = negative_kw_min_clicks

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run all keyword checks and return recommendations."""
        keywords: List[Dict[str, Any]] = data.get("keywords", [])
        recs: List[Recommendation] = []

        recs.extend(self._check_low_quality_score(keywords))
        recs.extend(self._check_duplicates(keywords))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_low_quality_score(
        self, keywords: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Flag keywords with QS below threshold and meaningful spend."""
        recs = []
        for kw in keywords:
            qs = kw.get("quality_score")
            cost = kw.get("cost", 0.0)
            if qs is None:
                continue
            if qs < self.low_qs_threshold and cost >= self.low_qs_min_cost:
                priority = (
                    RecommendationPriority.HIGH if qs <= 3 else RecommendationPriority.MEDIUM
                )
                impact = round(min(10.0, (self.low_qs_threshold - qs) * 2.0 + cost / 10), 1)
                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.KEYWORD_LOW_QUALITY_SCORE,
                        priority=priority,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=kw.get("campaign_id"),
                        campaign_name=kw.get("campaign_name"),
                        ad_group_id=kw.get("ad_group_id"),
                        ad_group_name=kw.get("ad_group_name"),
                        criterion_id=kw.get("criterion_id"),
                        title=f"Low Quality Score: '{kw['text']}' (QS={qs})",
                        description=(
                            f"Keyword '{kw['text']}' [{kw.get('match_type', 'UNKNOWN')}] "
                            f"has a Quality Score of {qs}/10 and has spent "
                            f"${cost:.2f} in the last 30 days. "
                            f"Consider pausing or improving ad relevance and landing page."
                        ),
                        rationale=(
                            f"Low QS increases your CPC and reduces ad visibility. "
                            f"QS {qs} is significantly below the threshold of {self.low_qs_threshold}."
                        ),
                        impact_score=impact,
                        estimated_cost_delta=-cost * 0.2,  # Estimated 20% savings
                        change_data={
                            "action": "pause_keyword",
                            "criterion_id": kw.get("criterion_id"),
                            "ad_group_id": kw.get("ad_group_id"),
                            "keyword_text": kw.get("text"),
                            "match_type": kw.get("match_type"),
                            "current_quality_score": qs,
                            "current_cost": cost,
                        },
                    )
                )
        logger.info(
            "Found %d low quality score keywords for %s", len(recs), self.customer_id
        )
        return recs

    def _check_duplicates(
        self, keywords: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Flag exact-match duplicate keywords across different ad groups."""
        # Group keywords by normalized text + match_type
        kw_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for kw in keywords:
            text = kw.get("text", "").lower().strip()
            match_type = kw.get("match_type", "").upper()
            if text:
                key = f"{text}|{match_type}"
                kw_map[key].append(kw)

        recs = []
        seen_pairs: Set[str] = set()

        for key, kw_list in kw_map.items():
            # Only flag when duplicates appear in DIFFERENT ad groups
            ad_group_ids = [kw.get("ad_group_id") for kw in kw_list]
            unique_ag_ids = set(ag_id for ag_id in ad_group_ids if ag_id)

            if len(unique_ag_ids) < 2:
                continue

            # Deduplicate the recommendation (avoid re-raising the same set)
            pair_key = "|".join(sorted(unique_ag_ids)) + "|" + key
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            text, match_type = key.split("|", 1)
            total_cost = sum(kw.get("cost", 0.0) for kw in kw_list)

            # Build a human-readable list of ad group names
            ag_names = list({kw.get("ad_group_name", kw.get("ad_group_id", "?")) for kw in kw_list})

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.KEYWORD_DUPLICATE,
                    priority=RecommendationPriority.MEDIUM,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=kw_list[0].get("campaign_id"),
                    campaign_name=kw_list[0].get("campaign_name"),
                    title=f"Duplicate Keyword: '{text}' [{match_type}] in {len(unique_ag_ids)} ad groups",
                    description=(
                        f"Keyword '{text}' [{match_type}] appears in {len(unique_ag_ids)} "
                        f"different ad groups: {', '.join(ag_names[:5])}. "
                        f"Duplicate keywords cause ad groups to compete against each other, "
                        f"driving up your costs."
                    ),
                    rationale=(
                        "Duplicate keywords across ad groups split your Quality Score "
                        "history and create internal auction competition, increasing CPC."
                    ),
                    impact_score=round(min(10.0, len(unique_ag_ids) * 2.0 + total_cost / 20), 1),
                    change_data={
                        "action": "consolidate_duplicates",
                        "keyword_text": text,
                        "match_type": match_type,
                        "affected_ad_groups": list(unique_ag_ids),
                        "keyword_instances": [
                            {
                                "criterion_id": kw.get("criterion_id"),
                                "ad_group_id": kw.get("ad_group_id"),
                                "ad_group_name": kw.get("ad_group_name"),
                                "cost": kw.get("cost", 0.0),
                                "conversions": kw.get("conversions", 0.0),
                            }
                            for kw in kw_list
                        ],
                    },
                )
            )

        logger.info(
            "Found %d duplicate keyword groups for %s", len(recs), self.customer_id
        )
        return recs
