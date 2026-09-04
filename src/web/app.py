"""FastAPI web application for Google Ads MCC Optimization Tool."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory application state
# ---------------------------------------------------------------------------

_state: Dict[str, Any] = {
    "client": None,       # GoogleAdsClient instance
    "mcc_id": None,       # MCC customer ID string
    "accounts": [],       # List of account dicts
    "recommendations": {},  # Dict[customer_id, List[Recommendation]]
}

DB_PATH = os.getenv("CHANGES_LOG_DB", "/tmp/changes_log.db")
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/tmp/google-ads-config"))

# ---------------------------------------------------------------------------
# Zones: "admin" = the classic MCC zone (env credentials, global _state);
# "user" = self-serve zone — anyone signs in with Google and works with the
# ad accounts THEIR token can reach, no MCC binding.
# ---------------------------------------------------------------------------

SESSION_COOKIE = "gao_session"
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "ostap49.hp@gmail.com").split(",")
    if e.strip()
}
_user_states: Dict[str, Dict[str, Any]] = {}


def _build_user_client(refresh_token: str, login_customer_id: Optional[str] = None):
    from google.ads.googleads.client import GoogleAdsClient

    cfg: Dict[str, Any] = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", ""),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET", ""),
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    if login_customer_id:
        cfg["login_customer_id"] = str(login_customer_id)
    return GoogleAdsClient.load_from_dict(cfg, version="v24")


def _ctx(request: Request) -> Dict[str, Any]:
    """Resolve the working state for this request: user session or admin."""
    from .auth_store import get_session_user

    user = get_session_user(request.cookies.get(SESSION_COOKIE))
    if user and user["zone"] != "admin":
        st = _user_states.get(user["email"])
        if st is None:
            st = {
                "client": None,
                "mcc_id": None,
                "accounts": [],
                "recommendations": {},
                "clients": {},
                "zone": "user",
                "email": user["email"],
                "refresh_token": user["refresh_token"],
            }
            try:
                st["client"] = _build_user_client(user["refresh_token"])
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("User client build failed for %s: %s", user["email"], exc)
            _user_states[user["email"]] = st
        return st

    _state.setdefault("clients", {})
    _state.setdefault("zone", "admin")
    _state["email"] = user["email"] if user else None
    return _state


def _client_for(ctx: Dict[str, Any], customer_id: Optional[str] = None):
    """Client with the right login_customer_id for the target account."""
    if ctx.get("zone") != "user" or not customer_id:
        return ctx["client"]
    login = next(
        (
            a.get("via_manager")
            for a in ctx.get("accounts", [])
            if a["id"] == customer_id
        ),
        None,
    )
    if not login:
        return ctx["client"]
    cl = ctx["clients"].get(login)
    if cl is None:
        cl = _build_user_client(ctx["refresh_token"], login)
        ctx["clients"][login] = cl
    return cl


def _list_user_accounts(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accounts reachable by the user's own token (no MCC binding).

    Direct accounts are queried as-is; any accessible manager account is
    expanded into its children (queries to children carry that manager as
    login_customer_id via _client_for).
    """
    from ..api.mcc_client import MCCClient

    client = ctx["client"]
    names = client.get_service("CustomerService").list_accessible_customers().resource_names
    out: List[Dict[str, Any]] = []
    seen = set()
    for rn in names:
        cid = rn.split("/")[-1]
        try:
            ga = client.get_service("GoogleAdsService")
            meta = None
            for batch in ga.search_stream(
                customer_id=cid,
                query="""
                    SELECT customer.descriptive_name, customer.currency_code,
                           customer.time_zone, customer.manager,
                           customer.test_account
                    FROM customer
                """,
            ):
                for r in batch.results:
                    meta = r.customer
            if meta is None:
                continue
            if meta.manager:
                child_client = _build_user_client(ctx["refresh_token"], cid)
                ctx["clients"][cid] = child_client
                for acc in MCCClient(child_client, cid).get_accounts_with_performance():
                    if acc["id"] in seen:
                        continue
                    seen.add(acc["id"])
                    acc["via_manager"] = cid
                    out.append(acc)
            else:
                if cid in seen:
                    continue
                seen.add(cid)
                perf = MCCClient(client, cid)._fetch_account_performance(cid)
                out.append(
                    {
                        "id": cid,
                        "name": meta.descriptive_name,
                        "currency": meta.currency_code,
                        "timezone": meta.time_zone,
                        "status": "ENABLED",
                        "is_manager": False,
                        "is_test": meta.test_account,
                        "via_manager": None,
                        **perf,
                    }
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Skipping accessible customer %s: %s", cid, exc)
    return out

# ---------------------------------------------------------------------------
# FastAPI app setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Google Ads Optimizer", version="1.0.0")

logging.basicConfig(level=logging.INFO)


@app.on_event("startup")
async def _auto_connect():
    """Auto-connect using environment variables if present."""
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    ]
    if not all(os.getenv(k) for k in required):
        logger.info("Env vars not set — skipping auto-connect.")
        return
    try:
        from ..auth.google_ads_auth import GoogleAdsAuthenticator
        auth = GoogleAdsAuthenticator(use_env=True)
        if auth.test_connection():
            _state["client"] = auth.get_client()
            _state["mcc_id"] = auth.login_customer_id
            logger.info("Auto-connected to Google Ads MCC %s", _state["mcc_id"])
        else:
            logger.warning("Auto-connect: test_connection() returned False")
    except Exception as exc:
        logger.warning("Auto-connect failed (app still starts): %s", exc)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files directory
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    yaml_content: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None


class SettingsSaveRequest(BaseModel):
    yaml_content: str


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_applier_for_recommendation(rec, client, db_path: str):
    """Return the appropriate applier for a given recommendation type."""
    from ..recommendations.recommendation import RecommendationType
    from ..appliers.keyword_applier import KeywordApplier
    from ..appliers.bid_applier import BidApplier
    from ..appliers.budget_applier import BudgetApplier
    from ..appliers.ad_applier import AdApplier

    keyword_types = {
        RecommendationType.KEYWORD_LOW_QUALITY_SCORE,
        RecommendationType.KEYWORD_ADD_NEGATIVE,
        RecommendationType.KEYWORD_OPPORTUNITY,
        RecommendationType.KEYWORD_DUPLICATE,
    }
    bid_types = {
        RecommendationType.BID_INCREASE,
        RecommendationType.BID_DECREASE,
    }
    budget_types = {
        RecommendationType.BUDGET_LIMITED,
        RecommendationType.BUDGET_REALLOCATION,
    }
    ad_types = {
        RecommendationType.AD_PAUSE_LOW_PERFORMER,
        RecommendationType.AD_ADD_RESPONSIVE,
    }

    rec_type = rec.rec_type
    if rec_type in keyword_types:
        return KeywordApplier(client, db_path)
    elif rec_type in bid_types:
        return BidApplier(client, db_path)
    elif rec_type in budget_types:
        return BudgetApplier(client, db_path)
    elif rec_type in ad_types:
        return AdApplier(client, db_path)
    return None


def _find_recommendation_by_id(rec_id: str):
    """Search all cached recommendations for a matching ID."""
    for customer_id, recs in _state["recommendations"].items():
        for rec in recs:
            if rec.id == rec_id:
                return rec
    return None


def _mask_value(value: str) -> str:
    """Mask a credential value, showing only first 4 characters."""
    if not value or len(value) <= 4:
        return "****"
    return value[:4] + "*****"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the single-page application HTML."""
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def get_status(request: Request):
    """Return connection status for the current zone (admin MCC or user)."""
    ctx = _ctx(request)
    return {
        "connected": ctx["client"] is not None,
        "mcc_id": ctx.get("mcc_id"),
        "zone": ctx.get("zone", "admin"),
        "email": ctx.get("email"),
    }


@app.get("/api/logout")
async def logout(request: Request):
    """Drop the session and go back to the dashboard."""
    from .auth_store import delete_session

    delete_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/connect")
async def connect(request: ConnectRequest):
    """Test and establish a Google Ads API connection."""
    from ..auth.google_ads_auth import GoogleAdsAuthenticator

    try:
        if request.yaml_content:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            yaml_path = CONFIG_DIR / "google-ads.yaml"
            yaml_path.write_text(request.yaml_content, encoding="utf-8")
            auth = GoogleAdsAuthenticator(yaml_path=str(yaml_path))
        elif request.env_vars:
            # Set env vars temporarily for the authenticator
            for key, value in request.env_vars.items():
                os.environ[key] = value
            auth = GoogleAdsAuthenticator(use_env=True)
        else:
            # Try default paths
            auth = GoogleAdsAuthenticator()

        connected = auth.test_connection()
        if connected:
            _state["client"] = auth.get_client()
            _state["mcc_id"] = auth.login_customer_id
            return {"connected": True, "error": None}
        else:
            return {"connected": False, "error": "Connection test failed. Check your credentials."}
    except Exception as exc:
        logger.error("Connection failed: %s", exc)
        return {"connected": False, "error": str(exc)}


@app.get("/api/accounts")
async def list_accounts(request: Request):
    """List accounts with performance: MCC children (admin) or the
    accounts the signed-in user's own token can reach (user zone)."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        return {"error": "Not connected. Sign in with Google or configure credentials."}

    from ..api.mcc_client import MCCClient

    try:
        if ctx.get("zone") == "user":
            accounts = _list_user_accounts(ctx)
        else:
            mcc_id = ctx.get("mcc_id")
            if not mcc_id:
                return {"error": "MCC customer ID not configured."}
            accounts = MCCClient(ctx["client"], mcc_id).get_accounts_with_performance()

        ctx["accounts"] = accounts
        for acc in accounts:
            recs = ctx["recommendations"].get(acc["id"], [])
            acc["recommendation_count"] = len(recs)

        return {"accounts": accounts}
    except Exception as exc:
        logger.error("Failed to list accounts: %s", exc)
        return {"error": str(exc)}


@app.post("/api/analyze/{customer_id}")
async def analyze_account(customer_id: str, request: Request):
    """Run all analyzers for a specific account and cache results."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.account_client import AccountClient
    from ..api.campaign_client import CampaignClient
    from ..recommendations.recommendation_engine import RecommendationEngine

    client = _client_for(ctx, customer_id)

    try:
        # Find account name from cached accounts
        account_name = customer_id
        for acc in ctx["accounts"]:
            if acc["id"] == customer_id:
                account_name = acc.get("name", customer_id)
                break

        acc_client = AccountClient(client, customer_id)
        camp_client = CampaignClient(client, customer_id)

        date_range = "LAST_30_DAYS"
        data = {
            "campaigns": acc_client.get_campaigns(date_range),
            "ad_groups": acc_client.get_ad_groups(date_range),
            "keywords": camp_client.get_keywords(date_range),
            "ads": camp_client.get_ads(date_range),
            "search_terms": camp_client.get_search_terms(date_range),
        }

        engine = RecommendationEngine()
        recs = engine.analyze_account(customer_id, account_name, data, date_range)
        ctx["recommendations"][customer_id] = recs

        return {
            "customer_id": customer_id,
            "account_name": account_name,
            "recommendation_count": len(recs),
            "recommendations": [rec.model_dump() for rec in recs],
        }
    except Exception as exc:
        logger.error("Analysis failed for account %s: %s", customer_id, exc, exc_info=True)
        return {"error": str(exc), "customer_id": customer_id}


@app.post("/api/apply/{rec_id}")
async def apply_recommendation(rec_id: str, request: Request):
    """Apply a specific recommendation by its ID."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    rec = None
    for recs in ctx["recommendations"].values():
        for r in recs:
            if r.id == rec_id:
                rec = r
                break
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation '{rec_id}' not found.")

    try:
        applier = _get_applier_for_recommendation(
            rec, _client_for(ctx, rec.customer_id), DB_PATH
        )
        if applier is None:
            return {
                "success": False,
                "error": f"No applier available for recommendation type: {rec.rec_type}",
            }

        success = applier.apply_safe(rec)
        return {
            "success": success,
            "error": rec.error_message if not success else None,
            "status": rec.status,
        }
    except Exception as exc:
        logger.error("Failed to apply recommendation %s: %s", rec_id, exc, exc_info=True)
        return {"success": False, "error": str(exc)}


@app.get("/api/audits")
async def list_audits():
    """Return the audit catalog for the UI."""
    from ..api.audits import catalog

    return {"audits": catalog()}


@app.post("/api/audit/{audit_key}")
async def run_audit(
    audit_key: str,
    request: Request,
    customer_id: Optional[str] = None,
    days: int = 30,
):
    """Run one read-only audit over one account or all reachable accounts."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.audits import AUDITS
    from ..api.mcc_client import MCCClient

    audit = AUDITS.get(audit_key)
    if audit is None:
        raise HTTPException(status_code=404, detail=f"Unknown audit '{audit_key}'")

    days = max(1, min(days, 365))
    client = ctx["client"]

    if customer_id:
        targets = [
            next(
                (a for a in ctx["accounts"] if a["id"] == customer_id),
                {"id": customer_id, "name": customer_id},
            )
        ]
    else:
        accounts = ctx["accounts"]
        if not accounts and ctx.get("zone") == "user":
            accounts = _list_user_accounts(ctx)
            ctx["accounts"] = accounts
        elif not accounts:
            mcc = MCCClient(client, ctx["mcc_id"])
            accounts = [a for a in mcc.list_accounts() if not a["is_manager"]]
        targets = accounts

    results = []
    total_rows = 0
    total_flagged = 0
    errors = 0
    for acc in targets:
        try:
            res = audit["run"](_client_for(ctx, acc["id"]), acc["id"], days)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Audit %s failed for %s: %s", audit_key, acc["id"], exc)
            errors += 1
            continue
        if not res["rows"]:
            continue
        total_rows += res["total_rows"]
        total_flagged += res["total_flagged"]
        results.append(
            {
                "account_id": acc["id"],
                "account_name": acc.get("name", acc["id"]),
                **res,
            }
        )

    return {
        "audit_key": audit_key,
        "label": audit["label"],
        "columns": audit["columns"],
        "days": days,
        "accounts_audited": len(targets) - errors,
        "accounts_with_findings": len(results),
        "total_rows": total_rows,
        "total_flagged": total_flagged,
        "errors": errors,
        "results": results,
    }


@app.get("/api/audit/history")
async def audit_history(request: Request):
    """Recent daily-sweep runs (admin MCC zone only — the cron is global)."""
    if _ctx(request).get("zone") == "user":
        return {"runs": [], "new_findings": []}
    from ..jobs.daily_audit import read_history

    return read_history()


@app.post("/api/insights/{kind}")
async def run_insight(kind: str, customer_id: str, request: Request, days: int = 30):
    """Period-over-period insight for one account (geo / keywords / pmax)."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.insights import INSIGHTS

    fn = INSIGHTS.get(kind)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"Unknown insight '{kind}'")

    days = max(1, min(days, 180))
    try:
        data = fn(_client_for(ctx, customer_id), customer_id, days)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Insight %s failed for %s: %s", kind, customer_id, exc, exc_info=True)
        return {"error": str(exc)}

    account_name = next(
        (a.get("name") for a in ctx["accounts"] if a["id"] == customer_id),
        customer_id,
    )
    return {"kind": kind, "customer_id": customer_id, "account_name": account_name,
            "days": days, **data}


@app.post("/api/score")
async def account_score(customer_id: str, request: Request, days: int = 30):
    """GetProfit-style 0-100 account scorecard (read-only)."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.score import compute_score

    days = max(7, min(days, 90))
    try:
        data = compute_score(_client_for(ctx, customer_id), customer_id, days)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Score failed for %s: %s", customer_id, exc, exc_info=True)
        return {"error": str(exc)}

    account_name = next(
        (a.get("name") for a in ctx["accounts"] if a["id"] == customer_id),
        customer_id,
    )
    return {"customer_id": customer_id, "account_name": account_name, **data}


# ---------------------------------------------------------------------------
# Google OAuth (Ads + Merchant Center) — in-app connect flow
# ---------------------------------------------------------------------------

OAUTH_SCOPES = (
    "openid email "
    "https://www.googleapis.com/auth/adwords "
    "https://www.googleapis.com/auth/content"
)
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI", "https://ads.ostap49.marketing/api/oauth/callback"
)


def _persist_refresh_token(token: str) -> None:
    """Store the new refresh token everywhere the app reads it from."""
    os.environ["GOOGLE_ADS_REFRESH_TOKEN"] = token

    env_path = Path(".env")
    if env_path.exists():
        lines, found = [], False
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith("GOOGLE_ADS_REFRESH_TOKEN="):
                lines.append(f"GOOGLE_ADS_REFRESH_TOKEN={token}")
                found = True
            else:
                lines.append(ln)
        if not found:
            lines.append(f"GOOGLE_ADS_REFRESH_TOKEN={token}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Keep the skills' yaml in sync too, if present
    yaml_path = Path(".claude/skills/account-diagnostic/google-ads.yaml")
    if yaml_path.exists():
        lines = [
            f"refresh_token: {token}"
            if ln.startswith("refresh_token:")
            else ln
            for ln in yaml_path.read_text(encoding="utf-8").splitlines()
        ]
        yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/oauth/start")
async def oauth_start():
    """Redirect to Google's consent screen for Ads + Merchant scopes."""
    import urllib.parse

    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=400, detail="GOOGLE_ADS_CLIENT_ID not set")
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "access_type": "offline",
            "prompt": "consent",  # force a refresh_token every time
        }
    )
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/api/oauth/callback")
async def oauth_callback(code: Optional[str] = None, error: Optional[str] = None):
    """Exchange the auth code, persist the refresh token, reconnect."""
    import json as _json
    import urllib.parse
    import urllib.request

    def page(title: str, body: str, ok: bool = True) -> HTMLResponse:
        color = "#16a34a" if ok else "#dc2626"
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;display:flex;align-items:center;"
            f"justify-content:center;height:100vh;background:#f3f4f6'>"
            f"<div style='background:#fff;padding:40px;border-radius:16px;max-width:480px;"
            f"box-shadow:0 4px 12px rgba(0,0,0,.08)'>"
            f"<h2 style='color:{color};margin-top:0'>{title}</h2>"
            f"<p style='color:#374151'>{body}</p>"
            f"<a href='/' style='color:#1d4ed8'>&larr; Back to the dashboard</a>"
            f"</div></body></html>"
        )

    if error:
        return page("Authorization cancelled", f"Google returned: {error}", ok=False)
    if not code:
        return page("Missing code", "No authorization code in the callback.", ok=False)

    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET", ""),
            "redirect_uri": OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = _json.loads(resp.read().decode())
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("OAuth code exchange failed: %s", exc)
        return page("Token exchange failed", str(exc), ok=False)

    refresh = payload.get("refresh_token")
    if not refresh:
        return page(
            "No refresh token returned",
            "Revoke the app's access at myaccount.google.com/permissions "
            "and try connecting again.",
            ok=False,
        )

    # Identify the user from the id_token (came straight from Google over TLS)
    email = ""
    try:
        import base64

        part = payload.get("id_token", "").split(".")[1]
        part += "=" * (-len(part) % 4)
        email = _json.loads(base64.urlsafe_b64decode(part)).get("email", "").lower()
    except Exception:  # pylint: disable=broad-except
        pass

    from .auth_store import create_session, upsert_user

    is_admin = email in ADMIN_EMAILS

    if is_admin:
        # Admin keeps the classic MCC zone: persist to env + rebuild global client
        _persist_refresh_token(refresh)
        upsert_user(email or "admin", refresh, zone="admin")
        try:
            from ..auth.google_ads_auth import GoogleAdsAuthenticator

            auth = GoogleAdsAuthenticator(use_env=True)
            if auth.test_connection():
                _state["client"] = auth.get_client()
                _state["mcc_id"] = auth.login_customer_id
                logger.info("OAuth admin reconnect OK, MCC %s", _state["mcc_id"])
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Admin reconnect after OAuth failed: %s", exc, exc_info=True)
        title, body = (
            "Connected (admin zone)!",
            f"{email}: MCC access + Merchant Center granted. Token saved on the server.",
        )
    else:
        if not email:
            return page(
                "Could not identify your Google account",
                "No email in the id_token — try again.",
                ok=False,
            )
        upsert_user(email, refresh, zone="user")
        _user_states.pop(email, None)  # force rebuild with the fresh token
        title, body = (
            "Connected!",
            f"{email}: your own ad accounts are now available — open the "
            f"Dashboard and press Load Accounts. Merchant Center bucketing "
            f"works with your merchants too.",
        )

    resp = page(title, body)
    resp.set_cookie(
        SESSION_COOKIE,
        create_session(email or "admin"),
        httponly=True,
        max_age=30 * 86400,
        samesite="lax",
    )
    return resp


@app.get("/api/merchant/accounts")
async def merchant_accounts(request: Request):
    """Merchant Center accounts reachable with the current zone's token."""
    from ..api.merchant_client import MerchantClient, MerchantError

    ctx = _ctx(request)
    try:
        return {"accounts": MerchantClient(ctx.get("refresh_token")).authinfo()}
    except MerchantError as exc:
        return {"error": str(exc)}


@app.post("/api/merchant/bucketize")
async def merchant_bucketize(
    merchant_id: str,
    customer_id: str,
    request: Request,
    days: int = 30,
    target_roas: float = 3.0,
    villain_cost: float = 10.0,
    zombie_impr: int = 10,
):
    """Join the Merchant feed with Ads performance and bucket every product."""
    ctx = _ctx(request)
    if ctx["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.merchant_client import MerchantClient, MerchantError
    from ..api.bucketing import product_performance, bucketize

    days = max(7, min(days, 180))
    try:
        mc = MerchantClient(ctx.get("refresh_token"))
        feed = mc.list_products(merchant_id)
        if not feed:
            return {"error": f"Merchant {merchant_id}: product feed is empty "
                             f"(or no access to this merchant)."}
        statuses = mc.list_statuses(merchant_id)
        perf = product_performance(_client_for(ctx, customer_id), customer_id, days)
        data = bucketize(
            feed, statuses, perf,
            target_roas=target_roas,
            villain_cost=villain_cost,
            zombie_impr=zombie_impr,
        )
    except MerchantError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Bucketize failed: %s", exc, exc_info=True)
        return {"error": str(exc)}

    account_name = next(
        (a.get("name") for a in ctx["accounts"] if a["id"] == customer_id),
        customer_id,
    )
    return {"merchant_id": merchant_id, "customer_id": customer_id,
            "account_name": account_name, "days": days, **data}


@app.get("/api/recommendations")
async def get_all_recommendations(request: Request):
    """Return all cached recommendations across all accounts."""
    ctx = _ctx(request)
    all_recs = []
    for customer_id, recs in ctx["recommendations"].items():
        for rec in recs:
            all_recs.append(rec.model_dump())
    return {"recommendations": all_recs, "total": len(all_recs)}


@app.get("/api/changes-log")
async def get_changes_log():
    """Return last 100 entries from the SQLite audit log."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT id, timestamp, account_id, rec_id, rec_type, action,
                   entity_id, before_value, after_value, status, error_msg
            FROM changes_log
            ORDER BY id DESC
            LIMIT 100
            """
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return {"entries": rows, "total": len(rows)}
    except sqlite3.OperationalError:
        # Table doesn't exist yet (no changes applied)
        return {"entries": [], "total": 0}
    except Exception as exc:
        logger.error("Failed to read changes log: %s", exc)
        return {"error": str(exc), "entries": []}


@app.get("/api/settings")
async def get_settings():
    """Return current configuration (with masked credentials)."""
    yaml_path = CONFIG_DIR / "google-ads.yaml"
    if not yaml_path.exists():
        return {"has_config": False, "yaml_content": ""}

    raw_content = yaml_path.read_text(encoding="utf-8")

    # Mask sensitive values
    masked_lines = []
    sensitive_keys = {"developer_token", "client_secret", "refresh_token", "client_id"}
    for line in raw_content.splitlines():
        stripped = line.strip()
        for key in sensitive_keys:
            if stripped.startswith(key + ":"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    value = parts[1].strip().strip('"').strip("'")
                    masked = _mask_value(value)
                    line = f"{parts[0]}: {masked}"
                break
        masked_lines.append(line)

    return {
        "has_config": True,
        "yaml_content": "\n".join(masked_lines),
    }


@app.post("/api/settings")
async def save_settings(request: SettingsSaveRequest):
    """Save google-ads.yaml content to the config directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yaml_path = CONFIG_DIR / "google-ads.yaml"
        yaml_path.write_text(request.yaml_content, encoding="utf-8")
        return {"saved": True, "path": str(yaml_path)}
    except Exception as exc:
        logger.error("Failed to save settings: %s", exc)
        return {"saved": False, "error": str(exc)}
