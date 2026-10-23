"""Alias <-> customer ID resolution. Every tool accepts an ID (with or without dashes) or an alias."""
from __future__ import annotations

import re

from core import db
from core.fmt import clean_id

_ID_RE = re.compile(r"^\d{10}$")


def resolve(customer: str | int) -> str:
    raw = str(customer).strip()
    cid = clean_id(raw)
    if _ID_RE.match(cid):
        return cid
    with db.connect() as conn:
        row = conn.execute("SELECT customer_id FROM account_alias WHERE lower(alias)=lower(?)", (raw,)).fetchone()
    if row:
        return row["customer_id"]
    raise ValueError(f"Unknown account '{customer}'. Pass a 10-digit customer ID or an alias set via set_account_alias.")


def aliases() -> dict[str, str]:
    with db.connect() as conn:
        return {r["customer_id"]: r["alias"] for r in conn.execute("SELECT customer_id, alias FROM account_alias")}


def alias_for(customer_id: str) -> str | None:
    return aliases().get(clean_id(customer_id))


def set_alias(customer_id: str, alias: str | None) -> None:
    cid = clean_id(customer_id)
    with db.connect() as conn:
        if not alias:
            conn.execute("DELETE FROM account_alias WHERE customer_id=?", (cid,))
            return
        if _ID_RE.match(clean_id(alias)):
            raise ValueError("Alias cannot look like a customer ID.")
        conn.execute("DELETE FROM account_alias WHERE lower(alias)=lower(?) AND customer_id<>?", (alias, cid))
        conn.execute(
            "INSERT INTO account_alias(customer_id, alias, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(customer_id) DO UPDATE SET alias=excluded.alias, updated_at=excluded.updated_at",
            (cid, alias.strip(), db.now_iso()),
        )
