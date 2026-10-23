"""Brief + decision log tools."""
from __future__ import annotations

from typing import Any

import yaml

from core import accounts, db
from memory.brief import get_brief, update_brief
from tools._registry import tool


@tool(title="Get account brief")
def get_account_brief(customer_id: str, as_yaml: bool = False) -> dict[str, Any]:
    """The account's brand brief: business type, margins, target CPA / ROAS, monthly budget, products, brand and
    competitor terms, languages, geo, seasonality, rules. Analysis tools use its targets as thresholds — read it
    before judging performance, and respect its rules in every proposal."""
    cid = accounts.resolve(customer_id)
    b = get_brief(cid)
    out = {"customer_id": cid, "alias": accounts.alias_for(cid), "brief": {k: v for k, v in b.items() if not k.startswith("_")},
           "exists": b["_exists"], "updated_at": b["_updated_at"]}
    if b.get("_yaml_file"):
        out["yaml_file"] = b["_yaml_file"]
    if not b["_exists"]:
        out["hint"] = ("No brief yet. Ask the user for business type, target CPA or ROAS, monthly budget, brand / "
                       "competitor terms and rules, then call update_account_brief.")
    if as_yaml:
        out["yaml"] = yaml.safe_dump(out["brief"], allow_unicode=True, sort_keys=False)
    return out


@tool(read_only=False, title="Update account brief")
def update_account_brief(customer_id: str, patch: dict[str, Any], append_to: list[str] | None = None) -> dict[str, Any]:
    """Merge `patch` into the brief (local SQLite, never sent to Google). Known keys: business_type, description,
    margin_pct, kpi (cpa|roas), target_cpa, target_roas, monthly_budget, campaign_budgets {campaign_id: monthly},
    main_products, brand_terms, competitor_terms, irrelevant_terms, languages, geo, seasonality,
    brand_impression_share_min (0-1), brand_campaign_match, rules, notes. Lists are replaced unless their key is in append_to (e.g. append_to=["rules", "competitor_terms"]).
    target_roas is a ratio (4.0 = 400%); money in account currency."""
    cid = accounts.resolve(customer_id)
    for k in ("target_cpa", "target_roas", "monthly_budget", "margin_pct"):
        if k in patch and patch[k] is not None:
            patch[k] = float(patch[k])
    if patch.get("target_roas") and patch["target_roas"] > 50:
        raise ValueError("target_roas looks like a percentage — pass a ratio (e.g. 4.0 for 400%).")
    b = update_brief(cid, patch, append_lists=tuple(append_to or ()))
    return {"customer_id": cid, "brief": {k: v for k, v in b.items() if not k.startswith("_")},
            "custom_keys": b.get("_custom_keys", [])}


@tool(read_only=False, title="Log a decision")
def log_decision(customer_id: str, decision: str, rationale: str | None = None, tags: list[str] | None = None,
                 proposal_id: int | None = None) -> dict[str, Any]:
    """Record an optimization decision (what and why) so future sessions and client reports know the history.
    Example: decision="Paused DSA campaign", rationale="CPA 2.4x target for 60 days", tags=["structure"]."""
    cid = accounts.resolve(customer_id)
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO decision(ts, customer_id, decision, rationale, tags, proposal_id) VALUES(?,?,?,?,?,?)",
                           (db.now_iso(), cid, decision, rationale, db.dumps(tags or []), proposal_id))
    return {"decision_id": cur.lastrowid, "customer_id": cid, "logged": decision}


@tool(title="Decision log")
def get_decision_log(customer_id: str, days: int = 90, tag: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Past decisions for the account (newest first), optionally filtered by tag."""
    from datetime import datetime, timedelta, timezone
    cid = accounts.resolve(customer_id)
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        rows = [db.row_dict(r, ("tags",)) for r in conn.execute(
            "SELECT * FROM decision WHERE customer_id=? AND ts>=? ORDER BY id DESC LIMIT ?", (cid, since, int(limit) * 3))]
    if tag:
        rows = [r for r in rows if tag in (r["tags"] or [])]
    rows = rows[: int(limit)]
    return {"customer_id": cid, "decisions": rows, "count": len(rows)}
