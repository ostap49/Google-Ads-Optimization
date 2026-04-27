"""Abstract base class for all analyzers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from ..recommendations.recommendation import Recommendation

logger = logging.getLogger(__name__)


class BaseAnalyzer(ABC):
    """Abstract base for all Google Ads analyzers.

    Subclasses receive a snapshot of account data and produce a list of
    :class:`Recommendation` objects.
    """

    def __init__(self, customer_id: str, customer_name: str = ""):
        self.customer_id = customer_id
        self.customer_name = customer_name
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def analyze(self, data: Dict[str, Any]) -> List[Recommendation]:
        """Run analysis on the provided data snapshot.

        Args:
            data: Dict with keys such as 'keywords', 'ads', 'campaigns',
                  'ad_groups', 'search_terms'.

        Returns:
            List of :class:`Recommendation` instances.
        """

    def _safe_roas(self, conversion_value: float, cost: float) -> float:
        """Return ROAS, guarding against division by zero."""
        if cost <= 0:
            return 0.0
        return conversion_value / cost

    def _safe_cpa(self, cost: float, conversions: float) -> float:
        """Return CPA, guarding against division by zero."""
        if conversions <= 0:
            return 0.0
        return cost / conversions

    def _micros_to_dollars(self, micros: int) -> float:
        return micros / 1_000_000

    def _dollars_to_micros(self, dollars: float) -> int:
        return int(dollars * 1_000_000)
