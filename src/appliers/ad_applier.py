"""Ad applier: pause/enable ads."""

from __future__ import annotations

import logging

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from .base_applier import BaseApplier
from ..recommendations.recommendation import Recommendation, RecommendationType

logger = logging.getLogger(__name__)


class AdApplier(BaseApplier):
    """Applies ad-related recommendations via AdGroupAdService.

    Handles:
    - AD_PAUSE_LOW_PERFORMER → pause the underperforming ad
    - AD_ADD_RESPONSIVE      → logs a manual action (RSA creation requires headlines/descriptions)
    """

    def apply(self, recommendation: Recommendation) -> bool:
        """Apply an ad recommendation."""
        rec_type = recommendation.rec_type
        change_data = recommendation.change_data

        if rec_type == RecommendationType.AD_PAUSE_LOW_PERFORMER:
            return self._pause_ad(
                customer_id=recommendation.customer_id,
                ad_group_id=change_data["ad_group_id"],
                ad_id=change_data["ad_id"],
            )
        elif rec_type == RecommendationType.AD_ADD_RESPONSIVE:
            # RSA creation requires user-provided content (headlines, descriptions)
            # Log this as a "manual action required" — the tool surfaces the recommendation
            # but the user must create the RSA in the UI or supply the ad copy.
            self.logger.info(
                "RSA recommendation %s for ad group %s requires manual creation. "
                "Please add a Responsive Search Ad in the Google Ads UI.",
                recommendation.id,
                change_data.get("ad_group_id"),
            )
            return True  # Treat as acknowledged
        else:
            self.logger.warning("AdApplier cannot handle rec_type %s", rec_type)
            return False

    # ------------------------------------------------------------------ #
    # API operations
    # ------------------------------------------------------------------ #

    def _pause_ad(
        self,
        customer_id: str,
        ad_group_id: str,
        ad_id: str,
    ) -> bool:
        """Pause an ad group ad."""
        service = self.client.get_service("AdGroupAdService")
        ad_group_ad = self.client.get_type("AdGroupAd")

        resource_name = self.resource_name_for_ad_group_ad(
            customer_id, ad_group_id, ad_id
        )
        ad_group_ad.resource_name = resource_name
        ad_group_ad.status = self.client.enums.AdGroupAdStatusEnum.PAUSED

        operation = self.client.get_type("AdGroupAdOperation")
        operation.update = ad_group_ad
        operation.update_mask.paths.append("status")

        try:
            response = service.mutate_ad_group_ads(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Paused ad %s in ad group %s for account %s",
                ad_id,
                ad_group_id,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise  # Let base_applier handle logging
