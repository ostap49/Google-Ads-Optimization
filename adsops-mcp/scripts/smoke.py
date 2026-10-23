"""Read-only smoke checks against your real MCC, one stage at a time.

    python scripts/smoke.py 1                    # auth + list_accounts + run_gaql
    python scripts/smoke.py 2 --account stryi    # read tools
    python scripts/smoke.py all --account 1234567890

Only read tools and propose_* in validate-only mode are exercised; nothing is ever applied.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402  (registers all tools)
from tools._registry import TOOLS  # noqa: E402


def call(name: str, **kwargs):
    t = time.time()
    out = TOOLS[name].fn(**kwargs)
    dt = time.time() - t
    text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
    print(f"\n=== {name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})  [{dt:.1f}s]")
    print(text[:4000] + ("\n... (truncated for display)" if len(text) > 4000 else ""))
    return out


def stage1(acc):
    out = call("list_accounts")
    acc = acc or (out.get("accounts") or [{}])[0].get("customer_id")
    if not acc:
        sys.exit("No accounts returned — check login_customer_id / access.")
    call("run_gaql", customer_id=acc, query=(
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, metrics.cost_micros "
        "FROM campaign WHERE segments.date DURING LAST_30_DAYS"), sort_by="metrics.cost", limit=10)
    return acc


def stage2(acc):
    acc = acc or stage1(None)
    call("get_account_overview", customer_id=acc, date_range="LAST_30_DAYS")
    call("get_campaign_performance", customer_id=acc, limit=10)
    call("get_campaign_performance", customer_id=acc, segment="device", limit=10)
    call("get_adgroup_and_keyword_performance", customer_id=acc, limit=10)
    call("get_search_terms", customer_id=acc, limit=10)
    call("ngram_analysis", customer_id=acc, limit=10, zero_conv_only=True)
    call("pmax_insights", customer_id=acc, limit=5)
    call("get_shopping_products", customer_id=acc, limit=10)
    call("analyze_wasted_spend", customer_id=acc, limit=15)
    call("budget_pacing", customer_id=acc)
    call("audit_conversion_tracking", customer_id=acc)
    call("account_audit", customer_id=acc)
    call("get_change_history", customer_id=acc, days=7, limit=10)
    call("get_recommendations", customer_id=acc, limit=10)
    call("keyword_research", customer_id=acc, seed_keywords=["купити диван"], limit=15)
    call("forecast", customer_id=acc, keywords=["купити диван", "[кутовий диван]"], max_cpc=15)
    call("anomaly_scan", customer_id=acc)
    call("budget_pacing", customer_id="all")
    return acc


def stage3(acc):
    """Propose (validate_only) a +5% budget change on the top-spend campaign, show it, then reject it."""
    acc = acc or stage1(None)
    perf = call("get_campaign_performance", customer_id=acc, limit=1, compare=False, include_paused=False)
    if not perf.get("rows"):
        print("no campaigns with spend — skipping")
        return acc
    top = perf["rows"][0]
    p = call("propose_update_campaign_budget", customer_id=acc, campaign_id=top["campaign_id"], change_pct=5,
             reason="smoke test — will be rejected")
    if p.get("proposal_id"):
        call("reject_proposal", proposal_id=p["proposal_id"], reason="smoke test")
    call("list_proposals", customer_id=acc, limit=5)
    return acc


def stage4(acc):
    """validate_only proposals for write tools (rejected afterwards). Nothing is applied."""
    acc = acc or stage1(None)
    camps = call("get_campaign_performance", customer_id=acc, channel_types=["SEARCH"], limit=1, compare=False)
    if not camps.get("rows"):
        print("no Search campaign — skipping write validation")
        return acc
    c = camps["rows"][0]
    kws = call("get_adgroup_and_keyword_performance", customer_id=acc, campaign_id=c["campaign_id"], limit=1)
    pids = []
    for name, kw in [
        ("propose_add_negative_keywords", {"keywords": ["adsops smoke test zzzz"], "level": "campaign", "campaign_ids": [c["campaign_id"]]}),
        ("propose_set_status", {"entity_type": "campaign", "ids": [c["campaign_id"]], "status": "PAUSED"}),
        ("propose_create_callouts", {"callouts": ["Adsops test 1", "Adsops test 2"], "campaign_ids": [c["campaign_id"]]}),
    ]:
        out = call(name, customer_id=acc, reason="smoke test — will be rejected", **kw)
        pids.append(out.get("proposal_id"))
    if kws.get("rows"):
        k = kws["rows"][0]
        out = call("propose_create_rsa", customer_id=acc, ad_group_id=k["ad_group_id"], final_url="https://example.com",
                   headlines=["Adsops тест 1", "Adsops тест 2", "Adsops тест 3"], descriptions=["Тестовий опис один.", "Тестовий опис два."])
        pids.append(out.get("proposal_id"))
    for pid in filter(None, pids):
        call("reject_proposal", proposal_id=pid, reason="smoke test")
    return acc


def stage5(acc):
    acc = acc or stage1(None)
    call("get_account_brief", customer_id=acc)
    call("get_decision_log", customer_id=acc)
    return acc


STAGES = {"1": [stage1], "2": [stage2], "3": [stage3], "4": [stage4], "5": [stage5]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage", help="1..8 or all")
    p.add_argument("--account", help="customer ID or alias (default: first account)")
    a = p.parse_args()
    server.build_server()
    acc = a.account
    for key in (sorted(STAGES) if a.stage == "all" else [a.stage]):
        for fn in STAGES[key]:
            acc = fn(acc) or acc


if __name__ == "__main__":
    main()
