"""Daily audit sweep: run every audit across the MCC and track drift.

Stores flagged findings per run in SQLite and marks findings that were not
present in the previous run as NEW — that's the drift signal ("a sneaky
setting flipped back on", "a new external MCC appeared", ...).

Run from cron:
    0 6 * * * cd /opt/app && ./venv/bin/python -m src.jobs.daily_audit \
        >> /var/log/adsopt-daily.log 2>&1
"""

import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_audit")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
AUDIT_DB = DATA_DIR / "audit_history.db"

# Which row fields identify a finding (for day-over-day matching).
ID_FIELDS: Dict[str, List[str]] = {
    "pmax-assets": ["campaign"],
    "dgen-automation": ["ad_id"],
    "non-serving-keywords": ["campaign", "ad_group", "keyword"],
    "conversion-health": ["action"],
    "search-term-waste": ["search_term", "campaign"],
    "change-history": [],  # flagged row = "no changes at all"; identity is static
    "mcc-links": ["manager_id"],
}


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            accounts INTEGER,
            total_flagged INTEGER,
            new_flagged INTEGER,
            errors INTEGER
        );
        CREATE TABLE IF NOT EXISTS findings (
            run_id INTEGER NOT NULL,
            audit_key TEXT NOT NULL,
            account_id TEXT NOT NULL,
            account_name TEXT,
            fingerprint TEXT NOT NULL,
            row_json TEXT,
            is_new INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
        """
    )
    conn.commit()


def _fingerprint(audit_key: str, account_id: str, row: dict) -> str:
    fields = ID_FIELDS.get(audit_key)
    if fields:
        ident = "|".join(str(row.get(f, "")) for f in fields)
    else:
        ident = "static"
    raw = f"{audit_key}|{account_id}|{ident}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def run_sweep() -> int:
    from ..auth.google_ads_auth import GoogleAdsAuthenticator
    from ..api.mcc_client import MCCClient
    from ..api.audits import AUDITS

    auth = GoogleAdsAuthenticator()
    client = auth.get_client()
    mcc_id = auth.login_customer_id
    if not mcc_id:
        logger.error("No GOOGLE_ADS_LOGIN_CUSTOMER_ID configured")
        return 1

    accounts = [
        a for a in MCCClient(client, mcc_id).list_accounts() if not a["is_manager"]
    ]
    logger.info("Sweeping %d accounts x %d audits", len(accounts), len(AUDITS))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB)
    _init_db(conn)

    prev_run = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    prev_fps = set()
    if prev_run:
        prev_fps = {
            r[0]
            for r in conn.execute(
                "SELECT fingerprint FROM findings WHERE run_id = ?", (prev_run[0],)
            )
        }

    cur = conn.execute(
        "INSERT INTO runs (ts, accounts, total_flagged, new_flagged, errors) "
        "VALUES (?, ?, 0, 0, 0)",
        (datetime.utcnow().isoformat(timespec="seconds"), len(accounts)),
    )
    run_id = cur.lastrowid

    total_flagged = 0
    new_flagged = 0
    errors = 0

    for audit_key, audit in AUDITS.items():
        for acc in accounts:
            try:
                res = audit["run"](client, acc["id"], 30)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("%s failed for %s: %s", audit_key, acc["id"], exc)
                errors += 1
                continue
            for row in res["rows"]:
                if not row.get("_flag"):
                    continue
                fp = _fingerprint(audit_key, acc["id"], row)
                is_new = 0 if fp in prev_fps else 1 if prev_run else 0
                total_flagged += 1
                new_flagged += is_new
                conn.execute(
                    "INSERT INTO findings "
                    "(run_id, audit_key, account_id, account_name, fingerprint, "
                    " row_json, is_new) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        audit_key,
                        acc["id"],
                        acc.get("name", ""),
                        fp,
                        json.dumps(row, ensure_ascii=False),
                        is_new,
                    ),
                )
        conn.commit()

    conn.execute(
        "UPDATE runs SET total_flagged = ?, new_flagged = ?, errors = ? WHERE id = ?",
        (total_flagged, new_flagged, errors, run_id),
    )
    conn.commit()
    conn.close()

    logger.info(
        "Sweep done: %d flagged (%d NEW), %d errors",
        total_flagged,
        new_flagged,
        errors,
    )
    return 0


def read_history(limit: int = 14) -> dict:
    """Recent runs + the latest run's NEW findings (for the web UI)."""
    if not AUDIT_DB.exists():
        return {"runs": [], "new_findings": []}
    conn = sqlite3.connect(AUDIT_DB)
    conn.row_factory = sqlite3.Row
    runs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, ts, accounts, total_flagged, new_flagged, errors "
            "FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]
    new_findings = []
    if runs:
        new_findings = [
            {
                "audit_key": r["audit_key"],
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "row": json.loads(r["row_json"] or "{}"),
            }
            for r in conn.execute(
                "SELECT audit_key, account_id, account_name, row_json "
                "FROM findings WHERE run_id = ? AND is_new = 1 LIMIT 50",
                (runs[0]["id"],),
            )
        ]
    conn.close()
    return {"runs": runs, "new_findings": new_findings}


if __name__ == "__main__":
    sys.exit(run_sweep())
