"""MCP tools for the proposal lifecycle."""
from __future__ import annotations

from typing import Any

import anyio
from pydantic import BaseModel, Field

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Context
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context  # type: ignore[no-redef]

from core.config import get_settings
from safety import engine
from tools._registry import tool


class Approval(BaseModel):
    approve: bool = Field(description="Apply this change to the live Google Ads account?")


def _client_supports_elicitation(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities if hasattr(ctx, "client_capabilities") else ctx.session.client_params.capabilities
        return bool(caps and getattr(caps, "elicitation", None))
    except Exception:  # noqa: BLE001
        return False


def _approval_text(p: dict[str, Any]) -> str:
    lines = [f"adsops-mcp: apply proposal #{p['proposal_id']} ({p['kind']}) on {p.get('alias') or p['customer_id']}?"]
    if p.get("summary"):
        lines.append(p["summary"])
    for d in p["diff"][:8]:
        lines.append(f"• {d.get('resource')}: {d.get('field')} {d.get('before')} → {d.get('after')}")
    if p["diff_count"] > 8:
        lines.append(f"… and {p['diff_count'] - 8} more")
    for w in p.get("warnings") or []:
        lines.append(f"⚠ {w}")
    return "\n".join(lines)


@tool(read_only=False, destructive=True, title="Apply an approved proposal")
async def apply_proposal(proposal_id: int, user_confirmed: bool = False, force: bool = False,
                         ctx: Context | None = None) -> dict[str, Any]:
    """Execute a validated ChangeProposal against the live account.

    ONLY call this after the user has seen the proposal and explicitly said yes in this conversation —
    then pass user_confirmed=True. If the client supports MCP elicitation, the server additionally asks the
    user directly. force=True applies even if values drifted since proposing (confirm with the user first).
    In dry-run mode this only validates."""
    if not user_confirmed:
        return {"error": "Refused: user_confirmed must be True — show the proposal and get an explicit 'yes' first."}
    p = await anyio.to_thread.run_sync(lambda: engine.present(engine._load(proposal_id)))
    mode = get_settings().require_elicitation
    if _client_supports_elicitation(ctx):
        try:
            res = await ctx.elicit(_approval_text(p), schema=Approval)
        except Exception as ex:  # noqa: BLE001 - client advertised but failed
            if mode:
                return {"error": f"Elicitation failed ({ex}); ADSOPS_REQUIRE_ELICITATION is on, refusing to apply."}
        else:
            if res.action != "accept" or not res.data.approve:
                return {"proposal_id": proposal_id, "status": p["status"], "message": "User declined in the approval dialog. Nothing applied."}
    elif mode:
        return {"error": "ADSOPS_REQUIRE_ELICITATION is on but this client does not support elicitation. Nothing applied."}
    return await anyio.to_thread.run_sync(lambda: engine.apply(proposal_id, force=force))


@tool(read_only=False, title="Propose a rollback")
def rollback_proposal(proposal_id: int, reason: str | None = None) -> dict[str, Any]:
    """Create a NEW proposal that restores the values from before proposal_id was applied (bids, budgets,
    statuses, bid strategy) or removes/pauses what it created (negatives, keywords, assets links -> removed;
    ads/campaigns -> paused, never removed). Apply it with apply_proposal after user approval."""
    return engine.rollback(proposal_id, reason)


@tool(read_only=False, title="Reject a proposal")
def reject_proposal(proposal_id: int, reason: str | None = None) -> dict[str, Any]:
    """Mark a proposal as rejected so it can no longer be applied."""
    return engine.reject(proposal_id, reason)


@tool(title="Get proposal")
def get_proposal(proposal_id: int, full_diff: bool = False) -> dict[str, Any]:
    """Show a proposal: diff (before -> after), estimated impact, warnings, validation result, status."""
    return engine.present(engine._load(proposal_id), full=full_diff)


@tool(title="List proposals")
def list_proposals(customer_id: str | None = None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Recent proposals (optionally per account / status: validated, invalid, applied, partially_applied,
    failed, rejected, dry_run, rolled_back, expired)."""
    rows = engine.list_proposals(customer_id, status, limit)
    return {"proposals": rows, "count": len(rows)}


@tool(title="Audit log")
def get_audit_log(customer_id: str | None = None, days: int = 30, limit: int = 100) -> dict[str, Any]:
    """Every mutate executed (or dry-run validated) by adsops: timestamp, account, resource, old/new value,
    Google request_id, outcome."""
    rows = engine.audit_log(customer_id, days, limit)
    return {"entries": rows, "count": len(rows), "dry_run_server": get_settings().dry_run}
