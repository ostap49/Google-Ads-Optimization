"""Two-step flow, guardrails, dry-run, drift, audit log, rollback."""
import os

import anyio
import pytest
import yaml

from core import db
from gads.client import GadsError, MutateResult
from safety import engine
from safety.guard import GuardrailError

CID = "1234567890"


def _config(data):
    path = os.environ["ADSOPS_CONFIG"]
    with open(path, "w") as f:
        yaml.safe_dump(data, f)
    from core.config import get_settings
    get_settings.cache_clear()


def _campaign(fake, amount=100.0, shared=False, camp_id=111):
    row = {"campaign.id": camp_id, "campaign.name": "Search | Brand", "campaign.status": "ENABLED",
           "campaign.advertising_channel_type": "SEARCH", "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
           "campaign.bidding_strategy": None, "campaign_budget.resource_name": f"customers/{CID}/campaignBudgets/9",
           "campaign_budget.amount_micros": int(amount * 1e6), "campaign_budget.explicitly_shared": shared,
           "campaign_budget.reference_count": 1, "campaign_budget.name": "b",
           "campaign.maximize_conversions.target_cpa_micros": 200_000_000}
    fake.route(r"FROM campaign WHERE campaign.id = ", [row])
    fake.route(r"DURING LAST_30_DAYS AND campaign.id", [{"campaign.id": camp_id, "metrics.cost_micros": 3_000_000_000,
                                                        "metrics.conversions": 20, "metrics.conversions_value": 0,
                                                        "metrics.search_budget_lost_impression_share": 0.25}])
    return row


def _apply(pid, confirmed=True, force=False):
    from safety.tools import apply_proposal
    return anyio.run(lambda: apply_proposal(pid, user_confirmed=confirmed, force=force))


def test_full_cycle_budget(fake):
    import tools.writes  # noqa: F401  registers specs
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 125}, "lost IS budget 25%")
    assert p["status"] == "validated" and p["diff"][0]["before"] == 100.0 and p["diff"][0]["after"] == 125.0
    assert fake.mutates[-1]["validate_only"] is True
    op = fake.mutates[-1]["ops"][0].campaign_budget_operation
    assert op.update.amount_micros == 125_000_000 and list(op.update_mask.paths) == ["amount_micros"]
    assert "next_step" in p

    # apply refused without confirmation
    assert "error" in _apply(p["proposal_id"], confirmed=False)
    out = _apply(p["proposal_id"])
    assert out["status"] == "applied" and fake.mutates[-1]["validate_only"] is False
    log = engine.audit_log(CID)
    assert any(e["old_value"] == 100.0 and e["new_value"] == 125.0 for e in log)
    assert all(e["request_id"] == "req-1" for e in log)

    # cannot apply twice
    with pytest.raises(ValueError):
        engine.apply(p["proposal_id"])

    # rollback: new proposal restoring 100
    _campaign(fake, 125)
    rb = engine.rollback(p["proposal_id"])
    assert rb["diff"][0]["before"] == 125.0 and rb["diff"][0]["after"] == 100.0 and rb["rollback_of"] == p["proposal_id"]
    engine.apply(rb["proposal_id"])
    assert engine._load(p["proposal_id"])["status"] == "rolled_back"


def test_budget_pct_guardrail(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    with pytest.raises(GuardrailError, match="max_budget_change_pct"):
        engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 150})
    _config({"defaults": {"max_budget_change_pct": 60}})
    assert engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 150})["status"] == "validated"


def test_max_daily_budget_and_protected_and_forbidden(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    _config({"accounts": {CID: {"max_daily_budget": 110}}})
    with pytest.raises(GuardrailError, match="max_daily_budget"):
        engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 120})
    _config({"accounts": {CID: {"protected_campaign_ids": [111]}}})
    with pytest.raises(GuardrailError, match="protected"):
        engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 105})
    _config({"accounts": {CID: {"forbidden_actions": ["update_campaign_budget"]}}})
    with pytest.raises(GuardrailError, match="forbidden"):
        engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 105})


def test_guardrails_via_alias(fake):
    import tools.writes  # noqa: F401
    from core.accounts import set_alias
    set_alias(CID, "stryi")
    _campaign(fake, 100)
    _config({"accounts": {"stryi": {"protected_campaign_ids": ["111"]}}})
    with pytest.raises(GuardrailError):
        engine.propose("update_campaign_budget", "stryi", {"campaign_id": "111", "new_daily_budget": 105})


def test_protected_rechecked_at_apply(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})
    _config({"accounts": {CID: {"protected_campaign_ids": ["111"]}}})
    with pytest.raises(GuardrailError):
        engine.apply(p["proposal_id"])


def test_dry_run_never_mutates(fake, monkeypatch):
    import tools.writes  # noqa: F401
    monkeypatch.setenv("ADSOPS_DRY_RUN", "true")
    from core.config import get_settings
    get_settings.cache_clear()
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})
    out = engine.apply(p["proposal_id"])
    assert out["status"] == "dry_run"
    assert all(m["validate_only"] for m in fake.mutates)
    assert all(e["dry_run"] == 1 for e in engine.audit_log(CID))


def test_drift_blocks_apply(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})
    _campaign(fake, 90)  # someone changed it in the UI
    out = engine.apply(p["proposal_id"])
    assert out["status"] == "stale" and out["drift"][0]["now"] == 90.0
    assert engine.apply(p["proposal_id"], force=True)["status"] == "applied"


def test_expired_proposal(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})
    with db.connect() as conn:
        conn.execute("UPDATE proposal SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (p["proposal_id"],))
    with pytest.raises(ValueError, match="older than"):
        engine.apply(p["proposal_id"])


def test_validation_failure_marks_invalid(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)

    def fail(ops, validate_only):
        raise GadsError("mutate failed: range_error.TOO_LOW", [{"code": "range_error.TOO_LOW", "message": "too low"}], "r")
    fake.mutate_result = fail
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})
    assert p["status"] == "invalid" and p["validation"]["errors"][0]["code"] == "range_error.TOO_LOW"
    with pytest.raises(ValueError):
        engine.apply(p["proposal_id"])


def test_partial_failure_recorded(fake):
    import tools.writes  # noqa: F401
    fake.route(r"FROM ad_group_criterion WHERE ad_group.id IN", [
        {"campaign.id": 1, "campaign.name": "C", "campaign.bidding_strategy_type": "MANUAL_CPC", "ad_group.id": 5,
         "ad_group.name": "AG", "ad_group.cpc_bid_micros": 10_000_000, "ad_group_criterion.criterion_id": c,
         "ad_group_criterion.keyword.text": f"kw{c}", "ad_group_criterion.keyword.match_type": "EXACT",
         "ad_group_criterion.cpc_bid_micros": 10_000_000, "ad_group_criterion.effective_cpc_bid_micros": 10_000_000}
        for c in (1, 2)])
    p = engine.propose("update_keyword_bids", CID, {"changes": [{"id": "5~1", "cpc_bid": 12}, {"id": "5~2", "change_pct": -20}]})
    assert [d["after"] for d in p["diff"]] == [12.0, 8.0] and fake.mutates[-1]["partial_failure"] is True

    def partial(ops, validate_only):
        return MutateResult(resource_names=["customers/1/adGroupCriteria/5~1", None],
                            partial_failures=[{"operation_index": 1, "code": "x", "message": "bad"}], request_id="r2")
    fake.mutate_result = partial
    out = engine.apply(p["proposal_id"])
    assert out["status"] == "partially_applied"
    assert any(e["outcome"] == "error" for e in engine.audit_log(CID))
    with pytest.raises(GuardrailError):
        engine.propose("update_keyword_bids", CID, {"changes": [{"id": "5~1", "cpc_bid": 20}]})


def test_bid_strategy_target_guard_and_rollback(fake):
    import tools.writes  # noqa: F401
    _campaign(fake, 100)
    with pytest.raises(GuardrailError, match="max_target_change_pct"):
        engine.propose("update_bid_strategy", CID, {"campaign_id": "111", "strategy": "TARGET_CPA", "target_cpa": 300})
    p = engine.propose("update_bid_strategy", CID, {"campaign_id": "111", "strategy": "TARGET_CPA", "target_cpa": 220})
    camp = fake.mutates[-1]["ops"][0].campaign_operation
    assert camp.update.maximize_conversions.target_cpa_micros == 220_000_000
    assert list(camp.update_mask.paths) == ["maximize_conversions.target_cpa_micros"]
    assert engine._load(p["proposal_id"])["rollback"]["params"]["target_cpa"] == 200.0


def test_elicitation_decline_blocks(fake):
    import tools.writes  # noqa: F401
    from safety import tools as st
    _campaign(fake, 100)
    p = engine.propose("update_campaign_budget", CID, {"campaign_id": "111", "new_daily_budget": 110})

    class Res:
        action = "decline"
        data = None

    class Ctx:
        client_capabilities = type("C", (), {"elicitation": object()})()

        async def elicit(self, message, schema):
            assert "proposal #" in message
            return Res()

    out = anyio.run(lambda: st.apply_proposal(p["proposal_id"], user_confirmed=True, ctx=Ctx()))
    assert "declined" in out["message"] and engine._load(p["proposal_id"])["status"] == "validated"
