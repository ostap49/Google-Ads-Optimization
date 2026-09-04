"""User + session storage for the self-serve (non-MCC) zone.

Every Google sign-in stores the user's own refresh token; a session cookie
maps requests back to that user. Admin emails keep the classic MCC zone.
"""

import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "users.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            refresh_token TEXT NOT NULL,
            zone TEXT NOT NULL DEFAULT 'user',
            created_at TEXT,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TEXT
        );
        """
    )
    return conn


def upsert_user(email: str, refresh_token: str, zone: str = "user") -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _conn()
    conn.execute(
        """
        INSERT INTO users (email, refresh_token, zone, created_at, last_login)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            refresh_token = excluded.refresh_token,
            zone = excluded.zone,
            last_login = excluded.last_login
        """,
        (email, refresh_token, zone, now, now),
    )
    conn.commit()
    conn.close()


def create_session(email: str) -> str:
    token = secrets.token_hex(32)
    conn = _conn()
    conn.execute(
        "INSERT INTO sessions (token, email, created_at) VALUES (?, ?, ?)",
        (token, email, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return token


def get_session_user(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    conn = _conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT u.email, u.refresh_token, u.zone
        FROM sessions s JOIN users u ON u.email = s.email
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    conn = _conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()
