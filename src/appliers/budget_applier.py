"""Budget applier: update campaign budgets."""

from __future__ import annotations

import logging

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from .base_applier import BaseApplier
from ..recommendations.recommendation import Recommendation, RecommendationType

logger = logging.getLogger(__name__)


class BudgetApplier(BaseApplier):
    """Applies budget-related recommendations via CampaignBudgetService.

    Handles:
    - BUDGET_LIMITED      → increase campaign budget
    - BUDGET_REALLOCATION → decrease source budget + increase target budget
    """

    def apply(self, recommendation: Recommendation) -> bool:
        """Apply a budget change recommendation."""
        rec_type = recommendation.rec_type
        change_data = recommendation.change_data

        if rec_type == RecommendationType.BUDGET_LIMITED:
            return self._apply_budget_increase(
                recommendation.customer_id, change_data
            )
        elif rec_type == RecommendationType.BUDGET_REALLOCATION:
            return self._apply_budget_reallocation(
                recommendation.customer_id, change_data
            )
        else:
            self.logger.warning("BudgetApplier cannot handle rec_type %s", rec_type)
            return False

    # ------------------------------------------------------------------ #
    # API operations
    # ------------------------------------------------------------------ #

    def _apply_budget_increase(
        self,
        customer_id: str,
        change_data: dict,
    ) -> bool:
        """Increase a campaign budget to the recommended amount."""
        budget_id = change_data.get("budget_id")
        recommended_budget = change_data.get("recommended_budget")
        current_budget = change_data.get("current_budget", 0.0)

        if not budget_id:
            self.logger.error("No budget_id in change_data")
            return False

        # If no specific recommended amount, increase by 20%
        if not recommended_budget or recommended_budget <= current_budget:
            new_budget = round(current_budget * 1.20, 2)
        else:
            new_budget = recommended_budget

        return self._update_budget(customer_id, budget_id, new_budget)

    def _apply_budget_reallocation(
        self,
        customer_id: str,
        change_data: dict,
    ) -> bool:
        """Move budget from source campaign to target campaign."""
        source_budget_id = change_data.get("source_budget_id")
        source_new_budget = change_data.get("source_new_budget")
        target_budget_id = change_data.get("target_budget_id")
        target_new_budget = change_data.get("target_new_budget")

        if not source_budget_id or not target_budget_id:
            self.logger.error("Missing budget IDs in change_data for reallocation")
            return False

        if source_new_budget is None or target_new_budget is None:
            self.logger.error("Missing new budget amounts in change_data")
            return False

        # Apply both budget changes
        source_ok = self._update_budget(customer_id, source_budget_id, source_new_budget)
        target_ok = self._update_budget(customer_id, target_budget_id, target_new_budget)

        return source_ok and target_ok

    def _update_budget(
        self,
        customer_id: str,
        budget_id: str,
        new_amount_dollars: float,
    ) -> bool:
        """Update a campaign budget to a new daily amount."""
        if new_amount_dollars <= 0:
            self.logger.error(
                "Invalid budget amount $%.2f for budget %s", new_amount_dollars, budget_id
            )
            return False

        service = self.client.get_service("CampaignBudgetService")
        budget = self.client.get_type("CampaignBudget")

        resource_name = self.resource_name_for_campaign_budget(customer_id, budget_id)
        budget.resource_name = resource_name
        budget.amount_micros = int(new_amount_dollars * 1_000_000)

        operation = self.client.get_type("CampaignBudgetOperation")
        operation.update.CopyFrom(budget)
        operation.update_mask.paths.append("amount_micros")

        try:
            response = service.mutate_campaign_budgets(
                customer_id=customer_id,
                operations=[operation],
            )
            self.logger.info(
                "Updated budget %s to $%.2f/day for account %s",
                budget_id,
                new_amount_dollars,
                customer_id,
            )
            return True
        except GoogleAdsException:
            raise  # Let base_applier handle logging
