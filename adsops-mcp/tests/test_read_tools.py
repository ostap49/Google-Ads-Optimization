"""Every read tool runs end-to-end against synthetic v26-shaped rows; every issued GAQL field is validated."""
import pytest

from memory.brief import update_brief

CID = "1234567890"


def _ok(out):
    assert isinstance(out, dict)
    assert "error" not in out, out
    return out


@pytest.mark.parametrize("name,kwargs", [
    ("get_account_overview", {}),
    ("get_campaign_performance", {}),
    ("get_campaign_performance", {"segment": "device"}),
    ("get_campaign_performance", {"segment": "hour", "compare": False}),
    ("get_campaign_performance", {"segment": "date"}),
    ("get_adgroup_and_keyword_performance", {}),
    ("get_adgroup_and_keyword_performance", {"level": "ad_group"}),
    ("get_search_terms", {}),
    ("ngram_analysis", {}),
    ("pmax_insights", {}),
    ("get_pmax_reusable_assets", {"campaign_id": "111"}),
    ("get_shopping_products", {}),
    ("analyze_wasted_spend", {}),
    ("budget_pacing", {"customer_id": CID}),
    ("audit_conversion_tracking", {}),
    ("account_audit", {}),
    ("get_recommendations", {}),
    ("anomaly_scan", {"customer_id": CID}),
])
def test_read_tool_runs(auto, name, kwargs):
    import server
    server.build_server()
    from tools._registry import TOOLS
    update_brief(CID, {"target_cpa": 300, "target_roas": 4, "monthly_budget": 60000,
                       "brand_terms": ["stryi"], "competitor_terms": ["rozetka"]})
    out = _ok(TOOLS[name].fn(**({"customer_id": CID} | kwargs)))
    assert out.get("currency") == "UAH" or name in ("budget_pacing",)


def test_negative_match_semantics():
    from tools.text import negative_matches
    assert negative_matches("купити диван київ", "диван", "BROAD")
    assert negative_matches("купити диван київ", "диван київ", "PHRASE")
    assert not negative_matches("купити київ диван", "диван київ", "PHRASE")
    assert negative_matches("київ диван купити", "купити київ", "BROAD")
    assert not negative_matches("купити диван", "диван", "EXACT")
    assert negative_matches("Диван", "диван", "EXACT")


def test_ad_text_length_cyrillic():
    from tools.text import ad_text_length
    assert ad_text_length("Купити диван у Львові") == 21
    assert ad_text_length("й") == 1  # й as base + combining breve counts once
    assert ad_text_length("日本") == 4


def test_search_term_flags(fake):
    from tools.search_terms import get_search_terms
    update_brief(CID, {"target_cpa": 100, "competitor_terms": ["rozetka"], "brand_terms": ["stryi"]})
    base = {"campaign.id": 1, "campaign.name": "S", "campaign.advertising_channel_type": "SEARCH",
            "ad_group.id": 11, "ad_group.name": "AG", "metrics.impressions": 100}
    fake.route("FROM search_term_view", [
        {**base, "search_term_view.search_term": "диван безкоштовно", "search_term_view.status": "NONE",
         "metrics.cost_micros": 250_000_000, "metrics.clicks": 20, "metrics.conversions": 0, "metrics.conversions_value": 0},
        {**base, "search_term_view.search_term": "кутовий диван львів", "search_term_view.status": "NONE",
         "metrics.cost_micros": 150_000_000, "metrics.clicks": 10, "metrics.conversions": 3, "metrics.conversions_value": 9000},
        {**base, "search_term_view.search_term": "rozetka диван", "search_term_view.status": "NONE",
         "metrics.cost_micros": 5_000_000, "metrics.clicks": 1, "metrics.conversions": 0, "metrics.conversions_value": 0},
        {**base, "search_term_view.search_term": "диван б у", "search_term_view.status": "NONE",
         "metrics.cost_micros": 5_000_000, "metrics.clicks": 1, "metrics.conversions": 0, "metrics.conversions_value": 0},
    ])
    fake.route("FROM campaign_criterion", [{"campaign.id": 1, "campaign_criterion.criterion_id": 5,
                                            "campaign_criterion.keyword.text": "б у", "campaign_criterion.keyword.match_type": "PHRASE"}])
    out = get_search_terms(CID, include_pmax=False)
    flags = {r["search_term"]: r["flags"] for r in out["rows"]}
    assert "zero_conv_spender" in flags["диван безкоштовно"] and "irrelevant_ngram" in flags["диван безкоштовно"]
    assert "promote_to_keyword" in flags["кутовий диван львів"]
    assert "competitor" in flags["rozetka диван"]
    assert "covered_by_negative" in flags["диван б у"]
    assert out["waste_threshold"]["amount"] == 200.0


def test_ngrams_cyrillic():
    from tools.search_terms import ngram_table
    rows = [{"search_term": "купити диван київ", "cost": 10.0, "clicks": 1, "impressions": 10, "conversions": 0, "conv_value": 0},
            {"search_term": "диван київ недорого", "cost": 5.0, "clicks": 1, "impressions": 10, "conversions": 1, "conv_value": 100}]
    t = {(a["n"], a["ngram"]): a for a in ngram_table(rows)}
    assert t[(2, "диван київ")]["cost"] == 15.0 and t[(2, "диван київ")]["terms"] == 2
    assert t[(1, "диван")]["cpa"] == 15.0


def test_anomaly_detects_spend_spike_and_zero_conv():
    from tools.anomaly import scan, zero_conv_days
    days = [f"2026-01-{d:02d}" for d in range(1, 30)]
    s = {d: {"cost": 1000.0 + (i % 3) * 20, "clicks": 100.0, "impressions": 2000.0, "conversions": 10.0, "value": 0.0}
         for i, d in enumerate(days)}
    s[days[-1]] = {"cost": 3000.0, "clicks": 100.0, "impressions": 2000.0, "conversions": 0.0, "value": 0.0}
    found = {a["metric"] for a in scan(s, days[:28], days[-1:], "yesterday", 2.5, 25)}
    assert "spend" in found
    assert zero_conv_days(s, days[:28], days[-1:])[0]["date"] == days[-1]
    # stable day -> nothing flagged
    s[days[-1]] = dict(s[days[0]])
    assert scan(s, days[:28], days[-1:], "yesterday", 2.5, 25) == []


def test_pacing_math(fake, monkeypatch):
    from datetime import date
    import tools.pacing as P
    monkeypatch.setattr(P, "today_in", lambda tz: date(2026, 10, 11))
    update_brief(CID, {"monthly_budget": 31000})
    row = {"campaign.id": 1, "campaign.name": "A", "campaign.status": "ENABLED", "campaign.advertising_channel_type": "SEARCH",
           "campaign.primary_status": "LIMITED", "campaign.primary_status_reasons": ["BUDGET_CONSTRAINED"],
           "campaign_budget.amount_micros": 1_000_000_000, "campaign_budget.explicitly_shared": False,
           "metrics.cost_micros": 10_500_000_000, "metrics.clicks": 1, "metrics.impressions": 1,
           "metrics.conversions": 10, "metrics.conversions_value": 0,
           "metrics.search_impression_share": 0.5, "metrics.search_budget_lost_impression_share": 0.3,
           "metrics.search_rank_lost_impression_share": 0.2}
    fake.route(r"segments.date = '2026-10-11'", [{"campaign.id": 1, "metrics.cost_micros": 500_000_000}])
    fake.route(r"BETWEEN '2026-10-04' AND '2026-10-10'", [{"campaign.id": 1, "metrics.cost_micros": 7_000_000_000}])
    fake.route(r"FROM campaign WHERE segments.date BETWEEN '2026-10-01'", [row])
    out = P.pace_account(CID)
    # spent 10000 through yesterday + 1000/day x 21 days remaining (incl. today) = 31000
    assert out["projected_eom"] == 31000.0 and out["status"] == "on_track"
    assert out["limited_by_budget"][0]["lost_is_budget"] == 30.0


def test_conversion_audit_flags(fake):
    from tools.conversions import audit_conversion_tracking
    update_brief(CID, {"business_type": "ecommerce"})
    base = {"conversion_action.status": "ENABLED", "conversion_action.origin": "WEBSITE",
            "conversion_action.include_in_conversions_metric": True,
            "conversion_action.attribution_model_settings.attribution_model": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
            "conversion_action.value_settings.always_use_default_value": False}
    fake.route("FROM conversion_action", [
        {**base, "conversion_action.id": 1, "conversion_action.name": "Purchase (tag)", "conversion_action.type": "WEBPAGE",
         "conversion_action.category": "PURCHASE", "conversion_action.primary_for_goal": True, "conversion_action.counting_type": "MANY_PER_CLICK"},
        {**base, "conversion_action.id": 2, "conversion_action.name": "GA4 purchase", "conversion_action.type": "GOOGLE_ANALYTICS_4_PURCHASE",
         "conversion_action.category": "PURCHASE", "conversion_action.primary_for_goal": True, "conversion_action.counting_type": "ONE_PER_CLICK"},
        {**base, "conversion_action.id": 3, "conversion_action.name": "Add to cart", "conversion_action.type": "WEBPAGE",
         "conversion_action.category": "ADD_TO_CART", "conversion_action.primary_for_goal": True, "conversion_action.counting_type": "MANY_PER_CLICK"},
    ])
    fake.route("FROM customer WHERE segments.date", [])
    fake.route("conversion_tracking_setting", [{"customer.auto_tagging_enabled": True,
                                                "customer.conversion_tracking_setting.effective_enablement_setting": "OFF"}])
    out = audit_conversion_tracking(CID)
    issues = " | ".join(f["issue"] for f in out["findings"])
    assert "PRIMARY purchase actions" in issues and "Micro-conversion 'Add to cart'" in issues
    assert "Enhanced conversions are OFF" in issues and "no conversions in 7 days" in issues
    assert out["score"] < 50
