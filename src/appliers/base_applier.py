"""Abstract base class for all recommendation appliers."""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from ..recommendations.recommendation import Recommendation, RecommendationStatus

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "changes_log.db"


def _init_db(db_path: str) -> sqlite3.Connection:
    """Initialize (or open) the SQLite changes log database."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS changes_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            account_id  TEXT NOT NULL,
            rec_id      TEXT NOT NULL,
            rec_type    TEXT NOT NULL,
            action      TEXT NOT NULL,
            entity_id   TEXT,
            before_value TEXT,
            after_value  TEXT,
            status      TEXT NOT NULL,
            error_msg   TEXT
        )
        """
    )
    conn.commit()
    return conn


class BaseApplier(ABC):
    """Abstract base for all appliers that mutate Google Ads entities.

    Subclasses must implement :meth:`apply` which performs the actual API
    mutation and returns ``True`` on success.

    All changes are logged to a local SQLite database for audit.
    """

    def __init__(
        self,
        client: GoogleAdsClient,
        db_path: str = DEFAULT_DB_PATH,
    ):
        self.client = client
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _init_db(self.db_path)
        return self._conn

    @abstractmethod
    def apply(self, recommendation: Recommendation) -> bool:
        """Apply the recommendation via the Google Ads API.

        Returns:
            ``True`` if the change was applied successfully, ``False`` otherwise.
        """

    def apply_safe(self, recommendation: Recommendation) -> bool:
        """Apply a recommendation and log the result, handling exceptions."""
        try:
            success = self.apply(recommendation)
            if success:
                recommendation.mark_applied()
                self._log_change(recommendation, success=True)
                self.logger.info(
                    "Applied recommendation %s (%s) for account %s",
                    recommendation.id,
                    recommendation.rec_type,
                    recommendation.customer_id,
                )
            else:
                recommendation.mark_failed("apply() returned False")
                self._log_change(recommendation, success=False)
            return success
        except GoogleAdsException as exc:
            error_msg = self._format_google_ads_error(exc)
            self.logger.error(
                "Google Ads API error applying recommendation %s: %s",
                recommendation.id,
                error_msg,
            )
            recommendation.mark_failed(error_msg)
            self._log_change(recommendation, success=False, error=error_msg)
            return False
        except Exception as exc:  # pylint: disable=broad-except
            error_msg = str(exc)
            self.logger.error(
                "Unexpected error applying recommendation %s: %s",
                recommendation.id,
                error_msg,
                exc_info=True,
            )
            recommendation.mark_failed(error_msg)
            self._log_change(recommendation, success=False, error=error_msg)
            return False

    def _log_change(
        self,
        rec: Recommendation,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Write a change record to the SQLite audit log."""
        change_data = rec.change_data
        action = change_data.get("action", rec.rec_type)
        entity_id = (
            rec.criterion_id
            or rec.ad_id
            or rec.campaign_id
            or rec.budget_id
            or ""
        )
        before_value = str(change_data.get("current_bid") or change_data.get("current_budget") or "")
        after_value = str(change_data.get("suggested_bid") or change_data.get("target_new_budget") or "")

        try:
            self.conn.execute(
                """
                INSERT INTO changes_log
                    (timestamp, account_id, rec_id, rec_type, action,
                     entity_id, before_value, after_value, status, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(),
                    rec.customer_id,
                    rec.id,
                    rec.rec_type,
                    action,
                    entity_id,
                    before_value,
                    after_value,
                    "SUCCESS" if success else "FAILED",
                    error,
                ),
            )
            self.conn.commit()
        except sqlite3.Error as db_err:
            self.logger.warning("Failed to write to changes log: %s", db_err)

    @staticmethod
    def _format_google_ads_error(exc: GoogleAdsException) -> str:
        """Format a GoogleAdsException into a readable string."""
        messages = []
        for error in exc.failure.errors:
            messages.append(error.message)
            if error.location:
                for fpe in error.location.field_path_elements:
                    messages.append(f"  On field: {fpe.field_name}")
        return " | ".join(messages) if messages else str(exc)

    def resource_name_for_ad_group_criterion(
        self, customer_id: str, ad_group_id: str, criterion_id: str
    ) -> str:
        """Build a resource name for an ad group criterion."""
        return f"customers/{customer_id}/adGroupCriteria/{ad_group_id}~{criterion_id}"

    def resource_name_for_campaign_budget(
        self, customer_id: str, budget_id: str
    ) -> str:
        """Build a resource name for a campaign budget."""
        return f"customers/{customer_id}/campaignBudgets/{budget_id}"

    def resource_name_for_ad_group_ad(
        self, customer_id: str, ad_group_id: str, ad_id: str
    ) -> str:
        """Build a resource name for an ad group ad."""
        return f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
