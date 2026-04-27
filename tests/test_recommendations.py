"""Tests for recommendation models, scoring, and deduplication."""

from __future__ import annotations

import pytest
from datetime import datetime

from src.recommendations.recommendation import (
    Recommendation,
    RecommendationBatch,
    RecommendationPriority,
    RecommendationStatus,
    RecommendationType,
)
from src.recommendations.recommendation_engine import RecommendationEngine


CUSTOMER_ID = "1234567890"
CUSTOMER_NAME = "Test Account"


# ================================================================== #
# Fixtures
# ================================================================== #

@pytest.fixture
def basic_recommendation():
    return Recommendation(
        rec_type=RecommendationType.KEYWORD_LOW_QUALITY_SCORE,
        priority=RecommendationPriority.HIGH,
        customer_id=CUSTOMER_ID,
        customer_name=CUSTOMER_NAME,
        campaign_id="camp001",
        campaign_name="Test Campaign",
        ad_group_id="ag001",
        criterion_id="crit001",
        title="Low QS: 'cheap stuff' (QS=2)",
        description="Keyword has low quality score.",
        rationale="QS 2 drives up CPCs.",
        impact_score=7.5,
        estimated_cost_delta=-15.00,
        change_data={
            "action": "pause_keyword",
            "criterion_id": "crit001",
            "ad_group_id": "ag001",
        },
    )


@pytest.fixture
def sample_batch(basic_recommendation):
    recs = [
        basic_recommendation,
        Recommendation(
            rec_type=RecommendationType.BUDGET_LIMITED,
            priority=RecommendationPriority.CRITICAL,
            customer_id=CUSTOMER_ID,
            customer_name=CUSTOMER_NAME,
            campaign_id="camp002",
            title="Budget Limited: 'Top Campaign'",
            description="Campaign is losing IS to budget.",
            impact_score=9.0,
            budget_id="bud002",
            change_data={
                "action": "increase_budget",
                "budget_id": "bud002",
                "current_budget": 50.0,
            },
        ),
        Recommendation(
            rec_type=RecommendationType.BID_DECREASE,
            priority=RecommendationPriority.MEDIUM,
            customer_id=CUSTOMER_ID,
            customer_name=CUSTOMER_NAME,
            campaign_id="camp001",
            criterion_id="crit002",
            title="Reduce Bid: 'irrelevant term'",
            description="Poor ROAS keyword.",
            impact_score=4.0,
            estimated_cost_delta=-8.00,
            change_data={
                "action": "update_bid",
                "criterion_id": "crit002",
                "suggested_bid": 0.50,
            },
        ),
    ]
    batch = RecommendationBatch(
        customer_ids=[CUSTOMER_ID],
        recommendations=recs,
    )
    return batch


# ================================================================== #
# Recommendation model tests
# ================================================================== #

class TestRecommendationModel:
    def test_default_id_generated(self, basic_recommendation):
        assert basic_recommendation.id is not None
        assert len(basic_recommendation.id) == 8

    def test_default_status_is_pending(self, basic_recommendation):
        assert basic_recommendation.status == RecommendationStatus.PENDING

    def test_mark_applied(self, basic_recommendation):
        basic_recommendation.mark_applied()
        assert basic_recommendation.status == RecommendationStatus.APPLIED
        assert basic_recommendation.applied_at is not None
        assert isinstance(basic_recommendation.applied_at, datetime)

    def test_mark_failed(self, basic_recommendation):
        error = "API error occurred"
        basic_recommendation.mark_failed(error)
        assert basic_recommendation.status == RecommendationStatus.FAILED
        assert basic_recommendation.error_message == error

    def test_mark_skipped(self, basic_recommendation):
        basic_recommendation.mark_skipped()
        assert basic_recommendation.status == RecommendationStatus.SKIPPED

    def test_priority_order_critical_lowest(self):
        rec = Recommendation(
            rec_type=RecommendationType.BUDGET_LIMITED,
            priority=RecommendationPriority.CRITICAL,
            customer_id=CUSTOMER_ID,
            title="Critical issue",
            description="",
            change_data={},
        )
        assert rec.priority_order == 0

    def test_priority_order_low_highest(self):
        rec = Recommendation(
            rec_type=RecommendationType.AD_ADD_RESPONSIVE,
            priority=RecommendationPriority.LOW,
            customer_id=CUSTOMER_ID,
            title="Low priority suggestion",
            description="",
            change_data={},
        )
        assert rec.priority_order == 3

    def test_impact_score_bounded(self):
        rec = Recommendation(
            rec_type=RecommendationType.KEYWORD_OPPORTUNITY,
            priority=RecommendationPriority.MEDIUM,
            customer_id=CUSTOMER_ID,
            title="Test",
            description="",
            impact_score=7.5,
            change_data={},
        )
        assert 0.0 <= rec.impact_score <= 10.0

    def test_impact_score_validation_rejects_out_of_range(self):
        with pytest.raises(Exception):
            Recommendation(
                rec_type=RecommendationType.KEYWORD_OPPORTUNITY,
                priority=RecommendationPriority.MEDIUM,
                customer_id=CUSTOMER_ID,
                title="Test",
                description="",
                impact_score=11.0,  # Invalid
                change_data={},
            )

    def test_short_summary_format(self, basic_recommendation):
        summary = basic_recommendation.short_summary()
        assert "HIGH" in summary
        assert "7.5" in summary

    def test_enum_values_stored_as_strings(self, basic_recommendation):
        """Pydantic should store enum values (strings), not enum objects."""
        # When use_enum_values=True, the stored value is the string
        assert isinstance(basic_recommendation.rec_type, str)
        assert isinstance(basic_recommendation.priority, str)
        assert isinstance(basic_recommendation.status, str)


# ================================================================== #
# RecommendationBatch tests
# ================================================================== #

class TestRecommendationBatch:
    def test_total_count(self, sample_batch):
        assert sample_batch.total_count() == 3

    def test_applied_count_initially_zero(self, sample_batch):
        assert sample_batch.applied_count() == 0

    def test_applied_count_after_apply(self, sample_batch):
        sample_batch.recommendations[0].mark_applied()
        assert sample_batch.applied_count() == 1

    def test_pending_returns_only_pending(self, sample_batch):
        sample_batch.recommendations[0].mark_skipped()
        pending = sample_batch.pending()
        assert len(pending) == 2

    def test_pending_sorted_by_priority_then_impact(self, sample_batch):
        pending = sample_batch.pending()
        # CRITICAL (impact=9.0) should come first, then HIGH (impact=7.5), then MEDIUM (impact=4.0)
        assert pending[0].priority == RecommendationPriority.CRITICAL
        assert pending[1].priority == RecommendationPriority.HIGH
        assert pending[2].priority == RecommendationPriority.MEDIUM

    def test_by_customer_filters_correctly(self, sample_batch):
        """by_customer should return only recs for the given customer."""
        # Add a rec for a different customer
        sample_batch.recommendations.append(
            Recommendation(
                rec_type=RecommendationType.AD_ADD_RESPONSIVE,
                priority=RecommendationPriority.LOW,
                customer_id="9999999999",  # Different customer
                title="Other account rec",
                description="",
                change_data={},
            )
        )
        customer_recs = sample_batch.by_customer(CUSTOMER_ID)
        assert len(customer_recs) == 3
        assert all(r.customer_id == CUSTOMER_ID for r in customer_recs)

    def test_generated_at_is_datetime(self, sample_batch):
        assert isinstance(sample_batch.generated_at, datetime)


# ================================================================== #
# RecommendationEngine tests
# ================================================================== #

class TestRecommendationEngine:
    def _make_minimal_data(self) -> dict:
        """Return the smallest dataset that exercises all analyzers."""
        return {
            "campaigns": [
                {
                    "id": "camp001",
                    "name": "Test Campaign",
                    "status": "ENABLED",
                    "bidding_strategy": "MANUAL_CPC",
                    "target_cpa": None,
                    "target_roas": None,
                    "budget_id": "bud001",
                    "budget_amount": 50.0,
                    "has_recommended_budget": False,
                    "recommended_budget": None,
                    "impressions": 5000,
                    "clicks": 200,
                    "cost": 300.0,
                    "conversions": 15.0,
                    "conversion_value": 900.0,
                    "ctr": 0.04,
                    "avg_cpc": 1.50,
                    "search_impression_share": 0.55,
                    "budget_lost_is": 0.0,
                    "rank_lost_is": 0.05,
                    "roas": 3.0,
                }
            ],
            "ad_groups": [],
            "keywords": [
                {
                    "criterion_id": "crit001",
                    "text": "low quality kw",
                    "match_type": "BROAD",
                    "status": "ENABLED",
                    "quality_score": 2,
                    "cpc_bid": 1.50,
                    "effective_cpc": 1.50,
                    "ad_group_id": "ag001",
                    "ad_group_name": "Test AG",
                    "campaign_id": "camp001",
                    "campaign_name": "Test Campaign",
                    "impressions": 500,
                    "clicks": 20,
                    "cost": 30.0,
                    "conversions": 0.5,
                    "conversion_value": 10.0,
                    "ctr": 0.04,
                    "avg_cpc": 1.50,
                    "impression_share": 0.3,
                    "rank_lost_is": 0.05,
                    "roas": 0.33,
                }
            ],
            "ads": [],
            "search_terms": [],
        }

    def test_analyze_account_returns_list(self):
        engine = RecommendationEngine()
        data = self._make_minimal_data()
        recs = engine.analyze_account(CUSTOMER_ID, CUSTOMER_NAME, data)
        assert isinstance(recs, list)

    def test_analyze_account_produces_recommendations(self):
        engine = RecommendationEngine()
        data = self._make_minimal_data()
        recs = engine.analyze_account(CUSTOMER_ID, CUSTOMER_NAME, data)
        # Should at least find the low-QS keyword
        assert len(recs) >= 1
        types = [r.rec_type for r in recs]
        assert RecommendationType.KEYWORD_LOW_QUALITY_SCORE in types

    def test_deduplication_removes_duplicate_recs(self):
        """Two recommendations of the same type+entity should be deduplicated."""
        engine = RecommendationEngine()
        rec1 = Recommendation(
            rec_type=RecommendationType.BID_DECREASE,
            priority=RecommendationPriority.HIGH,
            customer_id=CUSTOMER_ID,
            criterion_id="crit001",
            campaign_id="camp001",
            ad_group_id="ag001",
            title="Reduce bid 1",
            description="",
            change_data={},
        )
        rec2 = Recommendation(
            rec_type=RecommendationType.BID_DECREASE,
            priority=RecommendationPriority.MEDIUM,
            customer_id=CUSTOMER_ID,
            criterion_id="crit001",
            campaign_id="camp001",
            ad_group_id="ag001",
            title="Reduce bid 2 (duplicate)",
            description="",
            change_data={},
        )
        result = engine._deduplicate([rec1, rec2])
        assert len(result) == 1
        assert result[0].title == "Reduce bid 1"  # First one wins

    def test_deduplication_keeps_different_entities(self):
        """Recs for different criteria should both be kept."""
        engine = RecommendationEngine()
        rec1 = Recommendation(
            rec_type=RecommendationType.BID_DECREASE,
            priority=RecommendationPriority.HIGH,
            customer_id=CUSTOMER_ID,
            criterion_id="crit001",
            ad_group_id="ag001",
            title="Reduce bid crit001",
            description="",
            change_data={},
        )
        rec2 = Recommendation(
            rec_type=RecommendationType.BID_DECREASE,
            priority=RecommendationPriority.HIGH,
            customer_id=CUSTOMER_ID,
            criterion_id="crit002",
            ad_group_id="ag001",
            title="Reduce bid crit002",
            description="",
            change_data={},
        )
        result = engine._deduplicate([rec1, rec2])
        assert len(result) == 2

    def test_analyze_multiple_accounts(self):
        engine = RecommendationEngine()
        data = self._make_minimal_data()
        accounts_data = [
            {"customer_id": "111111", "customer_name": "Account 1", "data": data},
            {"customer_id": "222222", "customer_name": "Account 2", "data": data},
        ]
        batch = engine.analyze_multiple_accounts(accounts_data)

        assert isinstance(batch, RecommendationBatch)
        assert "111111" in batch.customer_ids
        assert "222222" in batch.customer_ids
        assert len(batch.recommendations) >= 2

    def test_sorted_by_priority_then_impact(self):
        """Recommendations should be sorted: CRITICAL first, then by impact descending."""
        engine = RecommendationEngine()
        data = self._make_minimal_data()

        # Add a critical campaign budget issue
        data["campaigns"][0]["budget_lost_is"] = 0.40

        recs = engine.analyze_account(CUSTOMER_ID, CUSTOMER_NAME, data)
        if len(recs) >= 2:
            for i in range(len(recs) - 1):
                r_curr = recs[i]
                r_next = recs[i + 1]
                # Priority order should be non-decreasing
                assert r_curr.priority_order <= r_next.priority_order or (
                    r_curr.priority_order == r_next.priority_order
                    and r_curr.impact_score >= r_next.impact_score
                )

    def test_handles_empty_data_gracefully(self):
        engine = RecommendationEngine()
        empty_data = {
            "campaigns": [],
            "ad_groups": [],
            "keywords": [],
            "ads": [],
            "search_terms": [],
        }
        recs = engine.analyze_account(CUSTOMER_ID, CUSTOMER_NAME, empty_data)
        assert recs == []
