"""Bid applier: update keyword CPC bids."""

from __future__ import annotations

import logging

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from .base_applier import BaseApplier
from ..recommendations.recommendation import Recommendation, RecommendationType

logger = logging.getLogger(__name__)


class BidApplier(BaseApplier):
    """Applies bid-related recommendations via AdGroupCriterionService.

    Handles:
    - BID_INCREASE → raise cpc_bid_micros
    - BID_DECREASE → lower cpc_bid_micros
    """

    def apply(self, recommendation: Recommendation) -> bool:
        """Apply a bid change recommendation."""
        rec_type = recommendation.rec_type
        change_data = recommendation.change_data

        if rec_type not in (
            RecommendationType.BID_INCREASE,
            RecommendationType.BID_DECREASE,
        ):
            self.logger.warning("BidApplier cannot handle rec_type %s", rec_type)
            return False

        suggested_bid = change_data.get("suggested_bid")
        if suggested_bid is None:
            self.logger.warning(
                "No suggested_bid in change_data for recommendation %s",
                recommendation.id,
            )
            return False

        if suggested_bid <= 0:
            self.logger.warning(
                "Invalid suggested_bid %.4f for recommendation %s",
                suggested_bid,
                recommendation.id,
            )
            return False

        return self._update_keyword_bid(
            customer_id=recommendation.customer_id,
            ad_group_id=change_data["ad_group_id"],
            criterion_id=change_data["criterion_id"],
            new_bid_dollars=suggested_bid,
        )

    # ------------------------------------------------------------------ #
    # API operations
    # ------------------------------------------------------------------ #

    def _update_keyword_bid(
        self,
        customer_id: str,
        ad_group_id: str,
        criterion_id: str,
        new_bid_dollars: float,
    ) -> bool:
        """Update the CPC bid of a keyword."""
        service = self.client.get_service("AdGroupCriterionService")
        criterion = self.client.get_type("AdGroupCriterion")

        resource_name = self.resource_name_for_ad_group_criterion(
            customer_id, ad_group_id, criterion_id
        )
        criterion.resource_name = resource_name

        # Convert dollars to micros
        criterion.cpc_bid_micros = int(new_bid_dollars * 1_000_000)

        operation = self.client.get_type("AdGroupCriterionOperation")
        operation.update.CopyFrom(criterion)
        operation.update_mask.paths.append("cpc_bid_micros")

        try:
            response = service.mutate_ad_group_criteria(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Updated bid for criterion %s~%s to $%.4f for account %s",
                ad_group_id,
                criterion_id,
                new_bid_dollars,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise  # Let base_applier handle logging
