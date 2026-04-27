"""Recommendation models and engine."""

from .recommendation import (
    Recommendation,
    RecommendationBatch,
    RecommendationType,
    RecommendationPriority,
    RecommendationStatus,
)

__all__ = [
    "Recommendation",
    "RecommendationBatch",
    "RecommendationType",
    "RecommendationPriority",
    "RecommendationStatus",
]

# RecommendationEngine is imported separately to avoid circular imports
# Use: from src.recommendations.recommendation_engine import RecommendationEngine
