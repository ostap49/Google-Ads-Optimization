"""Account discovery and aliases."""
from __future__ import annotations

from typing import Any

from core import accounts
from core.fmt import clean_id, fmt_id
from gads.client import get_gads
from gads.queries.accounts import CUSTOMER_CLIENTS
from tools._registry import tool


@tool(title="List accounts under the MCC")
def list_accounts(include_managers: bool = False, include_inactive: bool = False,
                  include_hidden: bool = False) -> dict[str, Any]:
    """Walk the MCC hierarchy (CustomerClient) from login_customer_id.

    Returns name, ID, currency, timezone, status, level, and the alias you set with set_account_alias.
    By default only ENABLED, non-manager, non-hidden client accounts are returned.
    """
    g = get_gads()
    root = g.login_customer_id
    names = accounts.aliases()
    if not root:
        svc = g.service("CustomerService")
        resp = svc.list_accessible_customers()
        ids = [rn.split("/")[-1] for rn in resp.resource_names]
        rows = []
        for cid in ids:
            try:
                info = g.account_info(cid)
            except Exception as ex:  # inaccessible directly
                rows.append({"customer_id": cid, "error": str(ex)[:200]})
                continue
            rows.append({"customer_id": cid, "id": fmt_id(cid), "name": info["name"], "currency": info["currency"],
                         "time_zone": info["time_zone"], "status": info["status"], "manager": info["manager"],
                         "alias": names.get(cid)})
        return {"root": None, "note": "login_customer_id not set; listing directly accessible accounts.",
                "accounts": rows, "count": len(rows)}

    rows = g.cached_search(root, CUSTOMER_CLIENTS, ttl=600)
    out = []
    for r in rows:
        cid = str(r["customer_client.id"])
        if cid == root and not include_managers:
            continue
        if r["customer_client.manager"] and not include_managers:
            continue
        if r["customer_client.status"] != "ENABLED" and not include_inactive:
            continue
        if r["customer_client.hidden"] and not include_hidden:
            continue
        out.append({
            "customer_id": cid,
            "id": fmt_id(cid),
            "name": r["customer_client.descriptive_name"],
            "currency": r["customer_client.currency_code"],
            "time_zone": r["customer_client.time_zone"],
            "status": r["customer_client.status"],
            "level": r["customer_client.level"],
            "manager": r["customer_client.manager"],
            "test_account": r["customer_client.test_account"],
            "alias": names.get(cid),
        })
    out.sort(key=lambda x: (x["manager"], (x["name"] or "").lower()))
    return {"root": fmt_id(root), "accounts": out, "count": len(out)}


@tool(read_only=False, title="Set account alias")
def set_account_alias(customer_id: str, alias: str) -> dict[str, Any]:
    """Set (or clear with an empty string) a short alias like "stryi" usable instead of the customer ID in every tool.

    Local-only: stored in the adsops SQLite DB, never sent to Google Ads.
    """
    cid = clean_id(customer_id) if clean_id(customer_id).isdigit() else accounts.resolve(customer_id)
    accounts.set_alias(cid, alias or None)
    return {"customer_id": cid, "alias": alias or None, "aliases": accounts.aliases()}
