"""propose_* tools. Each one only CREATES a validated ChangeProposal (validate_only mutate) —
nothing changes in Google Ads until apply_proposal is called after explicit user approval."""
from __future__ import annotations

import importlib
from typing import Any

from safety import engine
from tools._registry import tool

for _m in ("bidding", "keywords", "status", "ads", "campaigns", "exclusions", "recommendations"):
    try:
        importlib.import_module(f"safety.changes.{_m}")
    except ModuleNotFoundError as ex:  # pragma: no cover - partial installs
        if ex.name != f"safety.changes.{_m}":
            raise


@tool(read_only=False, title="Propose: campaign daily budget")
def propose_update_campaign_budget(customer_id: str, campaign_id: str, new_daily_budget: float | None = None,
                                   change_pct: float | None = None, reason: str | None = None) -> dict[str, Any]:
    """Propose a new daily budget (account currency) or a % change. Guardrails: max_daily_budget and
    max_budget_change_pct (default 30%) per apply. Warns on shared budgets / low lost-IS(budget)."""
    return engine.propose("update_campaign_budget", customer_id,
                          {"campaign_id": campaign_id, "new_daily_budget": new_daily_budget, "change_pct": change_pct}, reason)


@tool(read_only=False, title="Propose: keyword CPC bids")
def propose_update_keyword_bids(customer_id: str, changes: list[dict[str, Any]], reason: str | None = None) -> dict[str, Any]:
    """changes: [{"id": "adGroupId~criterionId" (or ad_group_id + criterion_id), "cpc_bid": 12.5 | "change_pct": -20 | "clear": true}].
    Guardrail: max_bid_change_pct (default 30%) vs the current effective bid."""
    return engine.propose("update_keyword_bids", customer_id, {"changes": changes}, reason)


@tool(read_only=False, title="Propose: bid strategy / targets")
def propose_update_bid_strategy(customer_id: str, campaign_id: str, strategy: str, target_cpa: float | None = None,
                                target_roas: float | None = None, cpc_bid_ceiling: float | None = None,
                                location: str | None = None, fraction: float | None = None,
                                reason: str | None = None) -> dict[str, Any]:
    """strategy: MAXIMIZE_CONVERSIONS (optional target_cpa) | MAXIMIZE_CONVERSION_VALUE (optional target_roas, e.g. 4.0 = 400%)
    | TARGET_CPA | TARGET_ROAS (stored as Max Conv / Max Conv Value with target) | MANUAL_CPC | MAXIMIZE_CLICKS
    (optional cpc_bid_ceiling) | TARGET_IMPRESSION_SHARE (location + fraction 0-1, optional cpc_bid_ceiling).
    Guardrail: max_target_change_pct (default 20%) for tCPA/tROAS changes within the same strategy."""
    return engine.propose("update_bid_strategy", customer_id,
                          {"campaign_id": campaign_id, "strategy": strategy, "target_cpa": target_cpa,
                           "target_roas": target_roas, "cpc_bid_ceiling": cpc_bid_ceiling, "location": location,
                           "fraction": fraction}, reason)
