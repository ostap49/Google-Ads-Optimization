"""Recommendation data model using Pydantic."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RecommendationType(str, Enum):
    """Types of optimization recommendations."""

    KEYWORD_LOW_QUALITY_SCORE = "KEYWORD_LOW_QUALITY_SCORE"
    KEYWORD_DUPLICATE = "KEYWORD_DUPLICATE"
    KEYWORD_ADD_NEGATIVE = "KEYWORD_ADD_NEGATIVE"
    KEYWORD_OPPORTUNITY = "KEYWORD_OPPORTUNITY"
    BID_INCREASE = "BID_INCREASE"
    BID_DECREASE = "BID_DECREASE"
    BUDGET_LIMITED = "BUDGET_LIMITED"
    BUDGET_REALLOCATION = "BUDGET_REALLOCATION"
    AD_PAUSE_LOW_PERFORMER = "AD_PAUSE_LOW_PERFORMER"
    AD_ADD_RESPONSIVE = "AD_ADD_RESPONSIVE"


class RecommendationPriority(str, Enum):
    """Priority levels for recommendations."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RecommendationStatus(str, Enum):
    """Status of a recommendation."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class Recommendation(BaseModel):
    """A single actionable optimization recommendation."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    rec_type: RecommendationType
    priority: RecommendationPriority
    status: RecommendationStatus = RecommendationStatus.PENDING

    # Account / entity context
    customer_id: str
    customer_name: str = ""
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    ad_group_id: Optional[str] = None
    ad_group_name: Optional[str] = None
    criterion_id: Optional[str] = None
    ad_id: Optional[str] = None
    budget_id: Optional[str] = None

    # Human-readable strings
    title: str
    description: str
    rationale: str = ""

    # Impact estimation
    impact_score: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Estimated impact score 0-10",
    )
    estimated_cost_delta: Optional[float] = None  # + means more spend, - means savings
    estimated_conversion_delta: Optional[float] = None

    # Proposed change payload (used by appliers)
    change_data: Dict[str, Any] = Field(default_factory=dict)

    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    applied_at: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        use_enum_values = True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @property
    def priority_order(self) -> int:
        """Return a numeric sort key for priority (lower = more urgent)."""
        order = {
            RecommendationPriority.CRITICAL: 0,
            RecommendationPriority.HIGH: 1,
            RecommendationPriority.MEDIUM: 2,
            RecommendationPriority.LOW: 3,
        }
        return order.get(self.priority, 99)  # type: ignore[arg-type]

    def mark_applied(self) -> None:
        """Mark this recommendation as successfully applied."""
        self.status = RecommendationStatus.APPLIED
        self.applied_at = datetime.utcnow()

    def mark_failed(self, error: str) -> None:
        """Mark this recommendation as failed."""
        self.status = RecommendationStatus.FAILED
        self.error_message = error

    def mark_skipped(self) -> None:
        """Mark this recommendation as skipped by user."""
        self.status = RecommendationStatus.SKIPPED

    def short_summary(self) -> str:
        """Return a one-line summary of the recommendation."""
        impact = f"[impact: {self.impact_score:.1f}/10]"
        return f"[{self.priority}] {self.title} {impact}"


class RecommendationBatch(BaseModel):
    """A collection of recommendations for one or more accounts."""

    customer_ids: List[str] = Field(default_factory=list)
    recommendations: List[Recommendation] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    date_range: str = "LAST_30_DAYS"

    def by_customer(self, customer_id: str) -> List[Recommendation]:
        """Filter recommendations for a specific customer."""
        return [r for r in self.recommendations if r.customer_id == customer_id]

    def pending(self) -> List[Recommendation]:
        """Return all pending recommendations sorted by priority then impact."""
        return sorted(
            [r for r in self.recommendations if r.status == RecommendationStatus.PENDING],
            key=lambda r: (r.priority_order, -r.impact_score),
        )

    def total_count(self) -> int:
        return len(self.recommendations)

    def applied_count(self) -> int:
        return sum(
            1 for r in self.recommendations if r.status == RecommendationStatus.APPLIED
        )
