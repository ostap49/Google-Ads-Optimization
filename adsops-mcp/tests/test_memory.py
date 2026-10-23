from memory.brief import get_brief, targets
from memory.tools import get_account_brief, get_decision_log, log_decision, update_account_brief

CID = "1234567890"


def test_brief_roundtrip_and_append():
    assert get_account_brief(CID)["exists"] is False
    update_account_brief(CID, {"target_cpa": "250", "rules": ["no competitor bidding"], "competitor_terms": ["Rozetka"]})
    out = update_account_brief(CID, {"rules": ["brand IS > 90%"]}, append_to=["rules"])
    assert out["brief"]["rules"] == ["no competitor bidding", "brand IS > 90%"]
    assert out["brief"]["target_cpa"] == 250.0
    t = targets(CID)
    assert t["target_cpa"] == 250.0 and t["competitor_terms"] == ["rozetka"] and t["kpi"] == "cpa"
    assert "yaml" in get_account_brief(CID, as_yaml=True)
    assert "error" in update_account_brief(CID, {"target_roas": 400})


def test_yaml_file_brief(monkeypatch, tmp_path):
    import memory.brief as B
    monkeypatch.setattr(B, "BRIEFS_DIR", tmp_path)
    (tmp_path / f"{CID}.yaml").write_text("target_roas: 5\nbrand_terms: [stryi]\n", encoding="utf-8")
    b = get_brief(CID)
    assert b["target_roas"] == 5 and b["_exists"]
    update_account_brief(CID, {"target_roas": 6})
    assert get_brief(CID)["target_roas"] == 6.0  # DB wins over file


def test_decision_log():
    log_decision(CID, "Paused DSA", "CPA 2.4x target", tags=["structure"])
    log_decision(CID, "Raised brand budget", tags=["budget"])
    assert get_decision_log(CID)["count"] == 2
    assert get_decision_log(CID, tag="budget")["decisions"][0]["decision"] == "Raised brand budget"
