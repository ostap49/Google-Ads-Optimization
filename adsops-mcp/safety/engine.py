"""Two-step change flow: propose (store + validate_only) -> human approval -> apply -> audit -> rollback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core import accounts, db
from core.config import get_settings
from gads.client import GadsError, MutateResult, get_gads
from safety.guard import Guard, GuardrailError
from safety.specs import REGISTRY

JSON_COLS = ("params", "diff", "impact", "warnings", "validation", "rollback", "result")
DIFF_PREVIEW = 60


def _spec(kind: str):
    if kind not in REGISTRY:
        raise ValueError(f"Unknown change kind '{kind}'")
    return REGISTRY[kind]


def _load(proposal_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = db.row_dict(conn.execute("SELECT * FROM proposal WHERE id=?", (int(proposal_id),)).fetchone(), JSON_COLS)
    if not row:
        raise ValueError(f"Proposal #{proposal_id} not found")
    return row


def _update(proposal_id: int, **fields: Any) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(db.dumps(v) if k in JSON_COLS else v)
    with db.connect() as conn:
        conn.execute(f"UPDATE proposal SET {', '.join(sets)} WHERE id=?", (*vals, int(proposal_id)))


def present(p: dict[str, Any], full: bool = False) -> dict[str, Any]:
    """What the model shows the user before asking for approval."""
    ttl = get_settings().guardrails(p["customer_id"])["proposal_ttl_hours"]
    created = datetime.fromisoformat(p["created_at"])
    diff = p["diff"] or []
    out = {
        "proposal_id": p["id"], "kind": p["kind"], "status": p["status"], "customer_id": p["customer_id"],
        "alias": accounts.alias_for(p["customer_id"]), "reason": p["reason"],
        "summary": (p.get("impact") or {}).get("summary"),
        "diff": diff if full else diff[:DIFF_PREVIEW], "diff_count": len(diff),
        "diff_truncated": (not full) and len(diff) > DIFF_PREVIEW,
        "impact": {k: v for k, v in (p.get("impact") or {}).items() if k != "summary" and not k.startswith("_")},
        "warnings": p.get("warnings") or [], "validation": p.get("validation"),
        "reversible": bool(p.get("rollback")) or _spec(p["kind"]).reversible,
        "created_at": p["created_at"], "expires_at": (created + timedelta(hours=ttl)).isoformat(),
        "dry_run_server": get_settings().dry_run,
    }
    if p.get("parent_id"):
        out["rollback_of"] = p["parent_id"]
    if p.get("result"):
        out["result"] = p["result"]
    if p["status"] == "validated":
        out["next_step"] = ("Show this proposal (diff, impact, warnings) to the user and wait for an explicit 'yes'. "
                            f"Only then call apply_proposal(proposal_id={p['id']}).")
    elif p["status"] == "invalid":
        out["next_step"] = "Google rejected the validate_only mutate — fix the inputs and propose again."
    return out


def propose(kind: str, customer_id: str, params: dict[str, Any], reason: str | None = None,
            parent_id: int | None = None) -> dict[str, Any]:
    spec = _spec(kind)
    g = get_gads()
    cid = accounts.resolve(customer_id)
    guard = Guard(cid, is_rollback=parent_id is not None)
    guard.check_kind(kind)
    prepared = spec.prepare(g, cid, params, guard)
    guard.check_campaigns(prepared.campaign_ids)
    ops = spec.operation_count(g, cid, prepared.params)
    guard.check_ops(ops)
    if not prepared.diff:
        return {"status": "noop", "message": "Nothing to change (all items already in the requested state).",
                "warnings": guard.warnings}

    validation: dict[str, Any]
    status = "validated"
    try:
        res = spec.validate(g, cid, prepared.params, prepared.partial_failure)
        validation = {"ok": not res.partial_failures, "operations": ops, "request_id": res.request_id,
                      "partial_failures": res.partial_failures}
        if res.partial_failures:
            if len(res.partial_failures) >= ops:
                status = "invalid"
            else:
                guard.warn(f"{len(res.partial_failures)} of {ops} operations fail validation and will be skipped on apply.")
    except GadsError as ex:
        status = "invalid"
        validation = {"ok": False, "operations": ops, **ex.as_dict()}

    impact = dict(prepared.impact)
    if prepared.summary:
        impact["summary"] = prepared.summary
    impact["_campaign_ids"] = sorted({str(c) for c in prepared.campaign_ids})
    impact["_partial_failure"] = prepared.partial_failure
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO proposal(customer_id, kind, status, params, diff, impact, reason, warnings, validation, "
            "rollback, parent_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, kind, status, db.dumps(prepared.params), db.dumps(prepared.diff), db.dumps(impact), reason,
             db.dumps(guard.warnings), db.dumps(validation), db.dumps(prepared.rollback), parent_id, db.now_iso()),
        )
        pid = cur.lastrowid
    return present(_load(pid))


def _drift(spec, g, cid: str, p: dict[str, Any]) -> list[dict[str, Any]]:
    fresh = spec.current(g, cid, p["params"])
    if fresh is None:
        return []
    before = {(d["resource"], d["field"]): d.get("before") for d in p["diff"]}
    out = []
    for item in fresh:
        key = (item["resource"], item["field"])
        if key in before and before[key] != item["value"]:
            out.append({"resource": key[0], "field": key[1], "proposed_from": before[key], "now": item["value"]})
    return out


def apply(proposal_id: int, force: bool = False) -> dict[str, Any]:
    p = _load(proposal_id)
    if p["status"] != "validated":
        raise ValueError(f"Proposal #{proposal_id} is '{p['status']}', only 'validated' proposals can be applied.")
    settings = get_settings()
    cid = p["customer_id"]
    guard = Guard(cid, is_rollback=p.get("parent_id") is not None)
    ttl = guard.rules["proposal_ttl_hours"]
    if datetime.now(timezone.utc) - datetime.fromisoformat(p["created_at"]) > timedelta(hours=ttl):
        _update(proposal_id, status="expired")
        raise ValueError(f"Proposal #{proposal_id} is older than {ttl}h — propose again to refresh the before-values.")
    guard.check_kind(p["kind"])
    spec = _spec(p["kind"])
    g = get_gads()
    # re-check protected campaigns (config may have changed since proposing)
    guard.check_campaigns((p.get("impact") or {}).get("_campaign_ids", []))
    drift = _drift(spec, g, cid, p)
    if drift and not force:
        return {"proposal_id": p["id"], "status": "stale", "drift": drift,
                "message": "Values changed since the proposal was made. Re-propose, or apply with force=True after "
                           "confirming with the user."}

    partial = bool((p.get("impact") or {}).get("_partial_failure"))
    with db.connect() as conn:
        conn.execute("INSERT INTO snapshot(ts, customer_id, proposal_id, label, data) VALUES(?,?,?,?,?)",
                     (db.now_iso(), cid, p["id"], "before_apply", db.dumps({"diff": p["diff"]})))

    if settings.dry_run:
        res = spec.validate(g, cid, p["params"], partial)
        _audit(p, res, dry_run=True)
        _update(proposal_id, status="dry_run", applied_at=db.now_iso(), result={**res.as_dict(), "dry_run": True})
        out = present(_load(proposal_id))
        out["message"] = "DRY-RUN: validated only, nothing changed in Google Ads."
        return out

    try:
        res = spec.execute(g, cid, p["params"], partial)
    except GadsError as ex:
        _audit_error(p, ex)
        _update(proposal_id, status="failed", applied_at=db.now_iso(), result=ex.as_dict())
        raise
    status = "applied"
    if res.partial_failures:
        status = "partially_applied" if any(res.resource_names) else "failed"
    rollback = p.get("rollback") or spec.rollback_from_result(cid, p["params"], res)
    _audit(p, res, dry_run=False)
    _update(proposal_id, status=status, applied_at=db.now_iso(), result=res.as_dict(), rollback=rollback)
    if p.get("parent_id") and status != "failed":
        _update(p["parent_id"], status="rolled_back")
    g.cache.invalidate(("q", cid))
    out = present(_load(proposal_id))
    out["message"] = f"{status}: {sum(1 for r in res.resource_names if r)} operations succeeded" + (
        f", {len(res.partial_failures)} failed" if res.partial_failures else "") + "."
    out["can_rollback"] = bool(rollback)
    return out


def _audit(p: dict[str, Any], res: MutateResult, dry_run: bool) -> None:
    """One row per diff entry (old -> new) + one row per resource the mutate returned."""
    failed = {f.get("operation_index") for f in res.partial_failures}
    outcome = "partial" if res.partial_failures else "ok"
    now = db.now_iso()
    rows = [(now, p["customer_id"], p["id"], p["kind"], d.get("resource_name") or d.get("resource"),
             db.dumps(d.get("before")), db.dumps(d.get("after")), res.request_id, int(dry_run), outcome, None)
            for d in p["diff"]]
    rows += [(now, p["customer_id"], p["id"], p["kind"], name, None, None, res.request_id, int(dry_run),
              "error" if i in failed else "ok", None)
             for i, name in enumerate(res.resource_names) if name]
    rows += [(now, p["customer_id"], p["id"], p["kind"], f"operation[{f.get('operation_index')}]", None, None,
              res.request_id, int(dry_run), "error", f"{f.get('code')}: {f.get('message')}") for f in res.partial_failures]
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO audit_log(ts, customer_id, proposal_id, kind, resource, old_value, new_value, request_id, "
            "dry_run, outcome, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)


def _audit_error(p: dict[str, Any], ex: GadsError) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(ts, customer_id, proposal_id, kind, resource, request_id, dry_run, outcome, error) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (db.now_iso(), p["customer_id"], p["id"], p["kind"], None, ex.request_id, 0, "error", str(ex)[:1000]))


def rollback(proposal_id: int, reason: str | None = None) -> dict[str, Any]:
    p = _load(proposal_id)
    if p["status"] not in ("applied", "partially_applied"):
        raise ValueError(f"Proposal #{proposal_id} is '{p['status']}' — only applied proposals can be rolled back.")
    rb = p.get("rollback")
    if not rb:
        raise ValueError(f"Proposal #{proposal_id} ({p['kind']}) is not reversible automatically.")
    out = propose(rb["kind"], p["customer_id"], rb["params"], reason or f"Rollback of proposal #{proposal_id}",
                  parent_id=p["id"])
    out["note"] = "This is a NEW proposal that restores the previous values. Show it to the user and apply it after approval."
    return out


def reject(proposal_id: int, reason: str | None = None) -> dict[str, Any]:
    p = _load(proposal_id)
    if p["status"] not in ("validated", "invalid", "stale"):
        raise ValueError(f"Proposal #{proposal_id} is '{p['status']}' and cannot be rejected.")
    _update(proposal_id, status="rejected", result={"rejected_reason": reason})
    return {"proposal_id": proposal_id, "status": "rejected"}


def list_proposals(customer_id: str | None = None, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM proposal WHERE 1=1", []
    if customer_id:
        q += " AND customer_id=?"
        args.append(accounts.resolve(customer_id))
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db.connect() as conn:
        rows = [db.row_dict(r, JSON_COLS) for r in conn.execute(q, args)]
    return [{"proposal_id": r["id"], "kind": r["kind"], "status": r["status"], "customer_id": r["customer_id"],
             "summary": (r.get("impact") or {}).get("summary"), "diff_count": len(r["diff"] or []),
             "created_at": r["created_at"], "applied_at": r["applied_at"], "reason": r["reason"]} for r in rows]


def audit_log(customer_id: str | None = None, days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(microsecond=0).isoformat()
    q, args = "SELECT * FROM audit_log WHERE ts >= ?", [since]
    if customer_id:
        q += " AND customer_id=?"
        args.append(accounts.resolve(customer_id))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db.connect() as conn:
        return [db.row_dict(r, ("old_value", "new_value")) for r in conn.execute(q, args)]


__all__ = ["propose", "apply", "rollback", "reject", "list_proposals", "audit_log", "present", "GuardrailError"]
