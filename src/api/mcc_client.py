"""MCC (Manager Account) client for listing all child accounts."""

import logging
from typing import List, Dict, Any

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logger = logging.getLogger(__name__)


class MCCClient:
    """Fetches all client accounts under an MCC."""

    CUSTOMER_QUERY = """
        SELECT
            customer_client.id,
            customer_client.descriptive_name,
            customer_client.currency_code,
            customer_client.time_zone,
            customer_client.status,
            customer_client.manager,
            customer_client.test_account,
            customer_client.level
        FROM customer_client
        WHERE
            customer_client.level = 1
            AND customer_client.status = 'ENABLED'
    """

    CUSTOMER_PERF_QUERY = """
        SELECT
            customer.id,
            customer.descriptive_name,
            customer.currency_code,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc,
            metrics.conversions_value
        FROM customer
        WHERE segments.date DURING LAST_30_DAYS
    """

    def __init__(self, client: GoogleAdsClient, mcc_customer_id: str):
        self.client = client
        self.mcc_customer_id = mcc_customer_id.replace("-", "")

    def list_accounts(self) -> List[Dict[str, Any]]:
        """List all enabled client accounts under the MCC."""
        service = self.client.get_service("GoogleAdsService")
        try:
            response = service.search_stream(
                customer_id=self.mcc_customer_id,
                query=self.CUSTOMER_QUERY,
            )
            accounts = []
            for batch in response:
                for row in batch.results:
                    cc = row.customer_client
                    accounts.append(
                        {
                            "id": str(cc.id),
                            "name": cc.descriptive_name,
                            "currency": cc.currency_code,
                            "timezone": cc.time_zone,
                            "status": cc.status.name,
                            "is_manager": cc.manager,
                            "is_test": cc.test_account,
                        }
                    )
            logger.info("Found %d client accounts under MCC %s", len(accounts), self.mcc_customer_id)
            return accounts
        except GoogleAdsException as exc:
            self._handle_google_ads_exception(exc)
            return []

    def get_accounts_with_performance(self, date_range: str = "LAST_30_DAYS") -> List[Dict[str, Any]]:
        """List accounts with their 30-day performance metrics."""
        accounts = self.list_accounts()
        enriched = []

        for account in accounts:
            if account["is_manager"]:
                continue
            perf = self._fetch_account_performance(account["id"], date_range)
            merged = {**account, **perf}
            enriched.append(merged)

        return enriched

    def _fetch_account_performance(
        self, customer_id: str, date_range: str = "LAST_30_DAYS"
    ) -> Dict[str, Any]:
        """Fetch aggregate performance metrics for a single account."""
        service = self.client.get_service("GoogleAdsService")
        query = f"""
            SELECT
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc,
                metrics.conversions_value
            FROM customer
            WHERE segments.date DURING {date_range}
        """
        try:
            response = service.search_stream(customer_id=customer_id, query=query)
            totals: Dict[str, Any] = {
                "impressions": 0,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0.0,
                "ctr": 0.0,
                "avg_cpc": 0.0,
                "conversion_value": 0.0,
                "roas": 0.0,
            }
            for batch in response:
                for row in batch.results:
                    m = row.metrics
                    totals["impressions"] += m.impressions
                    totals["clicks"] += m.clicks
                    totals["cost"] += m.cost_micros / 1_000_000
                    totals["conversions"] += m.conversions
                    totals["conversion_value"] += m.conversions_value

            if totals["clicks"] > 0:
                totals["ctr"] = totals["clicks"] / max(totals["impressions"], 1)
                totals["avg_cpc"] = totals["cost"] / totals["clicks"]
            if totals["cost"] > 0:
                totals["roas"] = totals["conversion_value"] / totals["cost"]

            return totals
        except GoogleAdsException as exc:
            logger.warning(
                "Could not fetch performance for account %s: %s",
                customer_id,
                exc.failure.errors[0].message if exc.failure.errors else str(exc),
            )
            return {
                "impressions": 0,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0.0,
                "ctr": 0.0,
                "avg_cpc": 0.0,
                "conversion_value": 0.0,
                "roas": 0.0,
            }

    @staticmethod
    def _handle_google_ads_exception(exc: GoogleAdsException) -> None:
        """Log a Google Ads API exception."""
        logger.error(
            "Google Ads API request failed with status '%s'.",
            exc.error.code().name,
        )
        for error in exc.failure.errors:
            logger.error("\tError: %s", error.message)
            if error.location:
                for field_path_element in error.location.field_path_elements:
                    logger.error("\t\tOn field: %s", field_path_element.field_name)
