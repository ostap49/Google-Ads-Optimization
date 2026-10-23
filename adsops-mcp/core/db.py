"""SQLite storage: aliases, change proposals, audit log, snapshots, briefs, decisions, alerts."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_alias (
    customer_id TEXT PRIMARY KEY,
    alias       TEXT UNIQUE NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id      TEXT NOT NULL,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,          -- validated | invalid | applied | partially_applied | failed | rejected | dry_run | rolled_back
    params           TEXT NOT NULL,          -- JSON: normalized input used to build mutate operations
    diff             TEXT NOT NULL,          -- JSON list: [{resource, field, before, after}]
    impact           TEXT,                   -- JSON: estimated impact
    reason           TEXT,
    warnings         TEXT,                   -- JSON list
    validation       TEXT,                   -- JSON: validate_only result / errors
    rollback         TEXT,                   -- JSON: {kind, params} or null when not reversible
    result           TEXT,                   -- JSON: apply result
    parent_id        INTEGER,                -- set for rollback proposals
    created_at       TEXT NOT NULL,
    applied_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposal_customer ON proposal(customer_id, created_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    proposal_id  INTEGER,
    kind         TEXT NOT NULL,
    resource     TEXT,
    old_value    TEXT,
    new_value    TEXT,
    request_id   TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    outcome      TEXT NOT NULL,              -- ok | error | partial
    error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_customer ON audit_log(customer_id, ts);
CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    proposal_id  INTEGER,
    label        TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brief (
    customer_id  TEXT PRIMARY KEY,
    data         TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rationale    TEXT,
    tags         TEXT,
    proposal_id  INTEGER
);
CREATE TABLE IF NOT EXISTS alert_sent (
    key          TEXT PRIMARY KEY,
    ts           TEXT NOT NULL
);
"""

_lock = threading.RLock()
_initialized: set[str] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(path or get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if str(path) not in _initialized:
                conn.executescript(SCHEMA)
                _initialized.add(str(path))
            yield conn
            conn.commit()
        finally:
            conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def row_dict(row: sqlite3.Row | None, json_cols: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_cols:
        if col in out:
            out[col] = loads(out[col])
    return out
