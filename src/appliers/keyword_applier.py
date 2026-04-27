"""Keyword applier: add keywords, add negatives, pause keywords."""

from __future__ import annotations

import logging
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from .base_applier import BaseApplier
from ..recommendations.recommendation import Recommendation, RecommendationType

logger = logging.getLogger(__name__)


class KeywordApplier(BaseApplier):
    """Applies keyword-related recommendations via Google Ads API.

    Handles:
    - KEYWORD_LOW_QUALITY_SCORE → pause keyword
    - KEYWORD_ADD_NEGATIVE       → add negative keyword to ad group
    - KEYWORD_OPPORTUNITY        → add new keyword to ad group
    """

    def apply(self, recommendation: Recommendation) -> bool:
        """Dispatch to the appropriate handler based on rec_type."""
        rec_type = recommendation.rec_type
        change_data = recommendation.change_data

        if rec_type == RecommendationType.KEYWORD_LOW_QUALITY_SCORE:
            return self._pause_keyword(
                customer_id=recommendation.customer_id,
                ad_group_id=change_data["ad_group_id"],
                criterion_id=change_data["criterion_id"],
            )
        elif rec_type == RecommendationType.KEYWORD_ADD_NEGATIVE:
            return self._add_negative_keyword(
                customer_id=recommendation.customer_id,
                campaign_id=change_data["campaign_id"],
                ad_group_id=change_data.get("ad_group_id"),
                text=change_data["search_term"],
                match_type=change_data.get("match_type", "EXACT"),
            )
        elif rec_type == RecommendationType.KEYWORD_OPPORTUNITY:
            return self._add_keyword(
                customer_id=recommendation.customer_id,
                ad_group_id=change_data["ad_group_id"],
                text=change_data["suggested_keyword_text"],
                match_type=change_data.get("match_type", "EXACT"),
            )
        else:
            self.logger.warning(
                "KeywordApplier cannot handle rec_type %s", rec_type
            )
            return False

    # ------------------------------------------------------------------ #
    # API operations
    # ------------------------------------------------------------------ #

    def _pause_keyword(
        self,
        customer_id: str,
        ad_group_id: str,
        criterion_id: str,
    ) -> bool:
        """Pause an ad group criterion (keyword)."""
        service = self.client.get_service("AdGroupCriterionService")
        criterion = self.client.get_type("AdGroupCriterion")

        resource_name = self.resource_name_for_ad_group_criterion(
            customer_id, ad_group_id, criterion_id
        )
        criterion.resource_name = resource_name

        # Set status to PAUSED
        criterion.status = self.client.enums.AdGroupCriterionStatusEnum.PAUSED

        operation = self.client.get_type("AdGroupCriterionOperation")
        operation.update.CopyFrom(criterion)
        operation.update_mask.paths.append("status")

        try:
            response = service.mutate_ad_group_criteria(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Paused keyword %s~%s for account %s",
                ad_group_id,
                criterion_id,
                customer_id,
            )
            return True
        except GoogleAdsException as exc:
            raise  # Let base_applier handle logging

    def _add_negative_keyword(
        self,
        customer_id: str,
        campaign_id: str,
        ad_group_id: str,
        text: str,
        match_type: str = "EXACT",
    ) -> bool:
        """Add a negative keyword to an ad group (or campaign if no ad_group_id)."""
        if ad_group_id:
            return self._add_ad_group_negative_keyword(
                customer_id, ad_group_id, text, match_type
            )
        else:
            return self._add_campaign_negative_keyword(
                customer_id, campaign_id, text, match_type
            )

    def _add_ad_group_negative_keyword(
        self,
        customer_id: str,
        ad_group_id: str,
        text: str,
        match_type: str,
    ) -> bool:
        """Add a negative keyword at the ad group level."""
        service = self.client.get_service("AdGroupCriterionService")
        criterion = self.client.get_type("AdGroupCriterion")

        criterion.ad_group = self.client.get_service("AdGroupService").ad_group_path(
            customer_id, ad_group_id
        )
        criterion.status = self.client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.negative = True
        criterion.keyword.text = text

        match_type_enum = getattr(
            self.client.enums.KeywordMatchTypeEnum,
            match_type.upper(),
            self.client.enums.KeywordMatchTypeEnum.EXACT,
        )
        criterion.keyword.match_type = match_type_enum

        operation = self.client.get_type("AdGroupCriterionOperation")
        operation.create.CopyFrom(criterion)

        try:
            response = service.mutate_ad_group_criteria(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Added negative keyword '%s' to ad group %s for account %s",
                text,
                ad_group_id,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise

    def _add_campaign_negative_keyword(
        self,
        customer_id: str,
        campaign_id: str,
        text: str,
        match_type: str,
    ) -> bool:
        """Add a negative keyword at the campaign level."""
        service = self.client.get_service("CampaignCriterionService")
        criterion = self.client.get_type("CampaignCriterion")

        criterion.campaign = self.client.get_service("CampaignService").campaign_path(
            customer_id, campaign_id
        )
        criterion.negative = True
        criterion.keyword.text = text
        match_type_enum = getattr(
            self.client.enums.KeywordMatchTypeEnum,
            match_type.upper(),
            self.client.enums.KeywordMatchTypeEnum.EXACT,
        )
        criterion.keyword.match_type = match_type_enum

        operation = self.client.get_type("CampaignCriterionOperation")
        operation.create.CopyFrom(criterion)

        try:
            response = service.mutate_campaign_criteria(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Added campaign-level negative keyword '%s' to campaign %s for account %s",
                text,
                campaign_id,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise

    def _add_keyword(
        self,
        customer_id: str,
        ad_group_id: str,
        text: str,
        match_type: str = "EXACT",
    ) -> bool:
        """Add a new positive keyword to an ad group."""
        service = self.client.get_service("AdGroupCriterionService")
        criterion = self.client.get_type("AdGroupCriterion")

        criterion.ad_group = self.client.get_service("AdGroupService").ad_group_path(
            customer_id, ad_group_id
        )
        criterion.status = self.client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text = text
        match_type_enum = getattr(
            self.client.enums.KeywordMatchTypeEnum,
            match_type.upper(),
            self.client.enums.KeywordMatchTypeEnum.EXACT,
        )
        criterion.keyword.match_type = match_type_enum

        operation = self.client.get_type("AdGroupCriterionOperation")
        operation.create.CopyFrom(criterion)

        try:
            response = service.mutate_ad_group_criteria(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Added keyword '%s' [%s] to ad group %s for account %s",
                text,
                match_type,
                ad_group_id,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise
