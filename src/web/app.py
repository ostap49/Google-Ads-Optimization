"""FastAPI web application for Google Ads MCC Optimization Tool."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
async def get_status():
    """Return connection status."""
    return {
        "connected": _state["client"] is not None,
        "mcc_id": _state["mcc_id"],
    }


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
async def list_accounts():
    """List all MCC accounts with performance metrics."""
    if _state["client"] is None:
        return {"error": "Not connected. Please configure credentials first."}

    from ..api.mcc_client import MCCClient

    try:
        mcc_id = _state["mcc_id"]
        if not mcc_id:
            return {"error": "MCC customer ID not configured."}

        mcc_client = MCCClient(_state["client"], mcc_id)
        accounts = mcc_client.get_accounts_with_performance()
        _state["accounts"] = accounts

        # Attach recommendation counts
        for acc in accounts:
            cid = acc["id"]
            recs = _state["recommendations"].get(cid, [])
            acc["recommendation_count"] = len(recs)

        return {"accounts": accounts}
    except Exception as exc:
        logger.error("Failed to list accounts: %s", exc)
        return {"error": str(exc)}


@app.post("/api/analyze/{customer_id}")
async def analyze_account(customer_id: str):
    """Run all analyzers for a specific account and cache results."""
    if _state["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.account_client import AccountClient
    from ..api.campaign_client import CampaignClient
    from ..recommendations.recommendation_engine import RecommendationEngine

    client = _state["client"]

    try:
        # Find account name from cached accounts
        account_name = customer_id
        for acc in _state["accounts"]:
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
        _state["recommendations"][customer_id] = recs

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
async def apply_recommendation(rec_id: str):
    """Apply a specific recommendation by its ID."""
    if _state["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    rec = _find_recommendation_by_id(rec_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation '{rec_id}' not found.")

    try:
        applier = _get_applier_for_recommendation(rec, _state["client"], DB_PATH)
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


@app.post("/api/audit/pmax-assets")
async def audit_pmax_assets(customer_id: Optional[str] = None):
    """Audit PMax asset automation settings (read-only).

    With customer_id: audit that single account.
    Without: audit every non-manager account under the MCC.
    """
    if _state["client"] is None:
        raise HTTPException(status_code=400, detail="Not connected.")

    from ..api.pmax_audit import audit_account, AUTOMATION_TYPES
    from ..api.mcc_client import MCCClient

    client = _state["client"]

    if customer_id:
        targets = [
            next(
                (a for a in _state["accounts"] if a["id"] == customer_id),
                {"id": customer_id, "name": customer_id},
            )
        ]
    else:
        accounts = _state["accounts"]
        if not accounts:
            mcc = MCCClient(client, _state["mcc_id"])
            accounts = [a for a in mcc.list_accounts() if not a["is_manager"]]
        targets = accounts

    results = []
    total_campaigns = 0
    non_compliant = 0
    errors = 0
    for acc in targets:
        try:
            campaigns = audit_account(client, acc["id"])
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("PMax audit failed for %s: %s", acc["id"], exc)
            errors += 1
            continue
        if not campaigns:
            continue
        total_campaigns += len(campaigns)
        non_compliant += sum(1 for c in campaigns if not c["compliant"])
        results.append(
            {
                "account_id": acc["id"],
                "account_name": acc.get("name", acc["id"]),
                "campaigns": campaigns,
            }
        )

    return {
        "setting_labels": AUTOMATION_TYPES,
        "accounts_audited": len(targets) - errors,
        "accounts_with_pmax": len(results),
        "total_campaigns": total_campaigns,
        "non_compliant_campaigns": non_compliant,
        "errors": errors,
        "results": results,
    }


@app.get("/api/recommendations")
async def get_all_recommendations():
    """Return all cached recommendations across all accounts."""
    all_recs = []
    for customer_id, recs in _state["recommendations"].items():
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
