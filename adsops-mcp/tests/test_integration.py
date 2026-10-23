"""Integration test against a Google Ads TEST account (test MCC). Never runs by default.

    ADSOPS_TEST_CUSTOMER_ID=1234567890 pytest -m integration
    ADSOPS_INTEGRATION_APPLY=1 ...   # also apply + roll back a negative keyword (test accounts never serve ads)

Your google-ads.yaml must point at a TEST manager account (developer token may be in test mode).
"""
import os

import pytest

pytestmark = pytest.mark.integration
CID = os.getenv("ADSOPS_TEST_CUSTOMER_ID")


@pytest.fixture(autouse=True)
def _real_client(monkeypatch):
    if not CID:
        pytest.skip("ADSOPS_TEST_CUSTOMER_ID not set")
    from gads.client import Gads, set_gads
    g = Gads()
    info = g.account_info(CID)
    if not info["test_account"]:
        pytest.skip("Refusing to run integration tests against a non-test account")
    set_gads(g)


def test_reads_against_test_account():
    import server
    server.build_server()
    from tools._registry import TOOLS
    assert "error" not in TOOLS["list_accounts"].fn()
    assert "error" not in TOOLS["run_gaql"].fn(customer_id=CID, query="SELECT campaign.id, campaign.name FROM campaign")
    for name in ("get_account_overview", "get_campaign_performance", "audit_conversion_tracking", "account_audit"):
        out = TOOLS[name].fn(customer_id=CID)
        assert "error" not in out, (name, out)


def test_write_cycle_on_test_account():
    from safety import engine
    import tools.writes  # noqa: F401
    camps = __import__("gads.client", fromlist=["get_gads"]).get_gads().search(
        CID, "SELECT campaign.id, campaign.advertising_channel_type FROM campaign WHERE campaign.status != 'REMOVED' "
             "AND campaign.advertising_channel_type = 'SEARCH' LIMIT 1")
    if not camps:
        pytest.skip("test account has no Search campaign")
    camp = str(camps[0]["campaign.id"])
    p = engine.propose("add_negative_keywords", CID, {"level": "campaign", "campaign_ids": [camp],
                                                      "keywords": ["adsops integration test zzz"]}, "integration test")
    assert p["status"] == "validated", p
    if not os.getenv("ADSOPS_INTEGRATION_APPLY"):
        engine.reject(p["proposal_id"])
        return
    applied = engine.apply(p["proposal_id"])
    assert applied["status"] == "applied" and applied["can_rollback"]
    rb = engine.rollback(p["proposal_id"])
    assert engine.apply(rb["proposal_id"])["status"] == "applied"
