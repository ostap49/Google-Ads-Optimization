"""adsops-mcp — self-hosted, single-user MCP server for Google Ads.

    python server.py                          # stdio (Claude Desktop / Claude Code)
    python server.py --transport http         # streamable HTTP, bearer-token protected
"""
from __future__ import annotations

import argparse
import hmac
import importlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp_types import ToolAnnotations
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.types import ToolAnnotations  # type: ignore[no-redef]

from core.config import get_settings
from tools._registry import TOOLS

TOOL_MODULES = [
    "tools.accounts",
    "tools.gaql",
    "tools.performance",
    "tools.search_terms",
    "tools.pmax",
    "tools.shopping",
    "tools.waste",
    "tools.pacing",
    "tools.conversions",
    "tools.audit",
    "tools.history",
    "tools.recommendations",
    "tools.planning",
    "tools.anomaly",
    "safety.tools",
    "tools.writes",
    "memory.tools",
    "reports.tools",
    "skills.prompts",
]

INSTRUCTIONS = """\
adsops-mcp manages Google Ads accounts under the user's MCC with live API data.

Accounts: every tool takes `customer_id` (10 digits, dashes optional) or an alias. Start with list_accounts.
Money: all amounts are in the account currency (returned as `currency`), never micros.

Before analysing an account, call get_account_brief: its targets (target_cpa, target_roas, monthly_budget),
brand/competitor terms and rules are the thresholds for every judgement. Analysis tools already read it.

WRITES ARE TWO-STEP. Every change is created with a propose_* tool, which stores a ChangeProposal, shows a
before -> after diff and runs the mutate with validate_only=True. You MUST show the user the proposal
(id, diff, impact, warnings) and wait for an explicit "yes" before calling apply_proposal(proposal_id).
Never apply a proposal the user has not explicitly approved in this conversation. Never batch-approve on
the user's behalf. Campaigns are always created PAUSED. There is no tool that removes campaigns.
After apply, offer rollback_proposal if results look wrong. Record important choices with log_decision.
"""

log = logging.getLogger("adsops")


def build_server() -> _Server:
    settings = get_settings()
    instructions = INSTRUCTIONS + ("\nSERVER IS IN DRY-RUN MODE: apply_proposal only validates.\n" if settings.dry_run else "")
    mcp = _Server(name="adsops-mcp", instructions=instructions)
    for mod in TOOL_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as ex:
            if ex.name != mod:
                raise
            log.debug("tool module %s not present", mod)
    for spec in TOOLS.values():
        mcp.tool(
            name=spec.name,
            title=spec.title,
            annotations=ToolAnnotations(
                title=spec.title,
                read_only_hint=spec.read_only,
                destructive_hint=spec.destructive,
                open_world_hint=True,
            ),
        )(spec.fn)
    from skills.prompts import register_prompts
    register_prompts(mcp)
    return mcp


class BearerAuth:
    """Minimal ASGI middleware: require `Authorization: Bearer <token>` on every HTTP request."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            ok = auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token)
            if not ok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="adsops-mcp"')]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="adsops-mcp server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.getenv("ADSOPS_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="Extra Host header values to accept in HTTP mode (e.g. your tunnel hostname).")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run: apply_proposal only validates.")
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("ADSOPS_LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.dry_run:
        os.environ["ADSOPS_DRY_RUN"] = "true"
        get_settings.cache_clear()
    settings = get_settings()
    mcp = build_server()

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    if not settings.http_token:
        sys.exit("HTTP mode requires ADSOPS_HTTP_TOKEN in .env (long random string).")
    import uvicorn

    host = args.host or settings.http_host
    port = args.port or settings.http_port
    kwargs = {"streamable_http_path": settings.http_path, "host": host}
    if args.allowed_host:
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = ["127.0.0.1:*", "localhost:*", *args.allowed_host]
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in args.allowed_host] + ["http://127.0.0.1:*", "http://localhost:*"],
        )
    app = BearerAuth(mcp.streamable_http_app(**kwargs), settings.http_token)
    log.info("adsops-mcp listening on http://%s:%s%s (dry_run=%s)", host, port, settings.http_path, settings.dry_run)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
