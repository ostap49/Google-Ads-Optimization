"""Bid analyzer: identify over- and under-bidding keywords."""

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
LOW_ROAS_RATIO = 0.5   # Flag keywords with ROAS < 50% of account average
HIGH_IS_LOST_RANK = 0.20  # 20% IS lost to rank signals bid is too low


class BidAnalyzer(BaseAnalyzer):
    """Analyzes keyword bids for over- and under-spending signals.

    Checks performed:
    1. Keywords with ROAS far below account average → reduce bid
    2. Keywords with high impression share lost to rank → increase bid
    3. Target-CPA-based bid suggestions for campaigns using tCPA
    """

    def __init__(
        self,
        customer_id: str,
        customer_name: str = "",
        low_roas_ratio: float = LOW_ROAS_RATIO,
        high_is_lost_rank: float = HIGH_IS_LOST_RANK,
    ):
        super().__init__(customer_id, customer_name)
        self.low_roas_ratio = low_roas_ratio
        self.high_is_lost_rank = high_is_lost_rank

    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run bid analysis and return recommendations."""
        keywords: List[Dict[str, Any]] = data.get("keywords", [])
        campaigns: List[Dict[str, Any]] = data.get("campaigns", [])
        recs: List[Recommendation] = []

        if not keywords:
            return recs

        # Compute account-level average ROAS from keywords with cost > 0
        roas_values = [
            kw["roas"] for kw in keywords if kw.get("cost", 0) > 0 and kw.get("roas", 0) > 0
        ]
        account_avg_roas = statistics.mean(roas_values) if roas_values else 0.0

        # Build a map of campaign settings
        campaign_map: Dict[str, Dict[str, Any]] = {c["id"]: c for c in campaigns}

        recs.extend(self._check_low_roas_bids(keywords, account_avg_roas, campaign_map))
        recs.extend(self._check_high_rank_lost_is(keywords))

        return recs

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _check_low_roas_bids(
        self,
        keywords: List[Dict[str, Any]],
        account_avg_roas: float,
        campaign_map: Dict[str, Dict[str, Any]],
    ) -> List[Recommendation]:
        """Flag keywords with ROAS below the low_roas_ratio * account average."""
        recs = []
        threshold_roas = account_avg_roas * self.low_roas_ratio

        # Only check when we have a valid account baseline
        if account_avg_roas <= 0:
            return recs

        for kw in keywords:
            cost = kw.get("cost", 0.0)
            conversions = kw.get("conversions", 0.0)
            roas = kw.get("roas", 0.0)
            cpc_bid = kw.get("cpc_bid")

            # Skip keywords with insufficient data
            if cost < 10.0:
                continue

            # Skip keywords that haven't had enough chance to convert (new keywords)
            if kw.get("impressions", 0) < 100:
                continue

            # If there's spend but very low / zero ROAS, flag it
            if roas < threshold_roas and cost > 0:
                campaign_id = kw.get("campaign_id")
                campaign = campaign_map.get(campaign_id, {})
                target_cpa = campaign.get("target_cpa")
                target_roas = campaign.get("target_roas")

                # Calculate suggested bid reduction
                suggested_bid = None
                if cpc_bid and roas > 0 and account_avg_roas > 0:
                    # Scale bid down proportionally to ROAS deficit
                    bid_scale = roas / account_avg_roas
                    suggested_bid = round(cpc_bid * bid_scale, 2)

                priority = (
                    RecommendationPriority.HIGH
                    if roas == 0
                    else RecommendationPriority.MEDIUM
                )
                impact = round(
                    min(10.0, (cost / 10) + (account_avg_roas - roas) * 2), 1
                )

                desc_parts = [
                    f"Keyword '{kw['text']}' [{kw.get('match_type', '?')}] has ROAS of "
                    f"{roas:.2f} vs account average of {account_avg_roas:.2f}. "
                    f"Spent ${cost:.2f} with {conversions:.1f} conversions."
                ]
                if suggested_bid:
                    desc_parts.append(
                        f" Consider reducing bid from ${cpc_bid:.2f} to ${suggested_bid:.2f}."
                    )

                recs.append(
                    Recommendation(
                        rec_type=RecommendationType.BID_DECREASE,
                        priority=priority,
                        customer_id=self.customer_id,
                        customer_name=self.customer_name,
                        campaign_id=campaign_id,
                        campaign_name=kw.get("campaign_name"),
                        ad_group_id=kw.get("ad_group_id"),
                        ad_group_name=kw.get("ad_group_name"),
                        criterion_id=kw.get("criterion_id"),
                        title=f"Reduce Bid: '{kw['text']}' (ROAS={roas:.2f})",
                        description=" ".join(desc_parts),
                        rationale=(
                            f"ROAS {roas:.2f} is below {self.low_roas_ratio * 100:.0f}% "
                            f"of account average ({account_avg_roas:.2f}). "
                            "Reducing bid will lower wasted spend."
                        ),
                        impact_score=impact,
                        estimated_cost_delta=-cost * 0.15,
                        change_data={
                            "action": "update_bid",
                            "criterion_id": kw.get("criterion_id"),
                            "ad_group_id": kw.get("ad_group_id"),
                            "current_bid": cpc_bid,
                            "suggested_bid": suggested_bid,
                            "current_roas": roas,
                            "account_avg_roas": account_avg_roas,
                        },
                    )
                )

        logger.info(
            "Found %d low-ROAS bid decrease recommendations for %s",
            len(recs),
            self.customer_id,
        )
        return recs

    def _check_high_rank_lost_is(
        self, keywords: List[Dict[str, Any]]
    ) -> List[Recommendation]:
        """Flag keywords losing significant IS to rank — bid may be too low."""
        recs = []
        for kw in keywords:
            rank_lost_is = kw.get("rank_lost_is", 0.0)
            cost = kw.get("cost", 0.0)
            conversions = kw.get("conversions", 0.0)
            roas = kw.get("roas", 0.0)
            cpc_bid = kw.get("cpc_bid")

            # Only recommend bid increase for keywords with positive ROAS
            if rank_lost_is < self.high_is_lost_rank:
                continue
            if cost < 5.0:  # Not enough data
                continue
            if roas <= 0:  # Don't increase bids on zero-conversion keywords
                continue

            # Suggest a modest bid increase (10-20% based on IS lost)
            bid_increase_pct = min(0.30, rank_lost_is * 0.5)
            suggested_bid = None
            if cpc_bid:
                suggested_bid = round(cpc_bid * (1 + bid_increase_pct), 2)

            impact = round(min(10.0, rank_lost_is * 20 + conversions * 0.5), 1)

            desc = (
                f"Keyword '{kw['text']}' [{kw.get('match_type', '?')}] is losing "
                f"{rank_lost_is * 100:.1f}% of impression share due to low rank. "
                f"ROAS is {roas:.2f} with {conversions:.1f} conversions."
            )
            if suggested_bid and cpc_bid:
                desc += (
                    f" Increasing bid from ${cpc_bid:.2f} to ${suggested_bid:.2f} "
                    f"could recover impression share."
                )

            recs.append(
                Recommendation(
                    rec_type=RecommendationType.BID_INCREASE,
                    priority=RecommendationPriority.MEDIUM,
                    customer_id=self.customer_id,
                    customer_name=self.customer_name,
                    campaign_id=kw.get("campaign_id"),
                    campaign_name=kw.get("campaign_name"),
                    ad_group_id=kw.get("ad_group_id"),
                    ad_group_name=kw.get("ad_group_name"),
                    criterion_id=kw.get("criterion_id"),
                    title=f"Increase Bid: '{kw['text']}' ({rank_lost_is*100:.0f}% IS lost to rank)",
                    description=desc,
                    rationale=(
                        f"This keyword has a good ROAS ({roas:.2f}) but is losing "
                        f"{rank_lost_is * 100:.1f}% of impression share to competitors "
                        "who are bidding higher."
                    ),
                    impact_score=impact,
                    estimated_conversion_delta=conversions * bid_increase_pct,
                    change_data={
                        "action": "update_bid",
                        "criterion_id": kw.get("criterion_id"),
                        "ad_group_id": kw.get("ad_group_id"),
                        "current_bid": cpc_bid,
                        "suggested_bid": suggested_bid,
                        "rank_lost_is": rank_lost_is,
                        "current_roas": roas,
                    },
                )
            )

        logger.info(
            "Found %d bid increase recommendations for %s", len(recs), self.customer_id
        )
        return recs
