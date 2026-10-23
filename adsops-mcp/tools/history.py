"""Change history (change_event)."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from core import db
from core.dates import today_in
from core.fmt import compact, from_micros
from gads.client import flatten_row
from tools._registry import tool
from tools.common import account_header, ctx, esc


def _value(v: Any, path: str) -> Any:
    if isinstance(v, (int, float)) and path.endswith("_micros"):
        return from_micros(v)
    if isinstance(v, (dict, list)):
        s = str(v)
        return s if len(s) <= 200 else s[:200] + "…"
    return v


@tool(title="Change history")
def get_change_history(
    customer_id: str,
    days: int = 7,
    resource_types: list[str] | None = None,
    user_email: str | None = None,
    campaign_id: str | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    """Who changed what in the last N days (change_event; Google keeps 30 days). For each change: time, user,
    client (UI / API / Editor / scripts), resource type, operation, changed fields with old -> new values.
    resource_types: e.g. ["CAMPAIGN", "CAMPAIGN_BUDGET", "AD_GROUP_CRITERION", "AD_GROUP_AD", "CAMPAIGN_CRITERION"].
    Changes applied through adsops proposals are marked with proposal_id."""
    days = max(1, min(int(days), 30))
    g, cid, info, _ = ctx(customer_id)
    today = today_in(info.get("time_zone"))
    start = today - timedelta(days=days - 1)
    where = [f"change_event.change_date_time >= '{start.isoformat()}'",
             f"change_event.change_date_time <= '{today.isoformat()} 23:59:59'"]
    if resource_types:
        where.append("change_event.change_resource_type IN (" + ", ".join(f"'{t.upper()}'" for t in resource_types) + ")")
    if user_email:
        where.append(f"change_event.user_email = '{esc(user_email)}'")
    if campaign_id:
        where.append(f"change_event.campaign = 'customers/{cid}/campaigns/{int(campaign_id)}'")
    base_fields = ["change_event.change_date_time", "change_event.user_email", "change_event.client_type",
                   "change_event.change_resource_type", "change_event.resource_change_operation",
                   "change_event.change_resource_name", "change_event.campaign", "change_event.ad_group"]
    q = ("SELECT " + ", ".join(base_fields) + ", change_event.changed_fields, change_event.old_resource, "
         "change_event.new_resource FROM change_event WHERE " + " AND ".join(where) +
         " ORDER BY change_event.change_date_time DESC LIMIT 10000")
    raw = g.search_raw(cid, q)
    names = {f"customers/{cid}/campaigns/{x['campaign.id']}": x["campaign.name"]
             for x in g.cached_search(cid, "SELECT campaign.id, campaign.name FROM campaign")}
    with db.connect() as conn:
        ours = {}
        for row in conn.execute("SELECT resource, proposal_id, ts FROM audit_log WHERE customer_id=? AND dry_run=0", (cid,)):
            if row["resource"]:
                ours.setdefault(row["resource"], row["proposal_id"])
    changes = []
    for row in raw:
        base = flatten_row(row, base_fields)
        rtype = base["change_event.change_resource_type"] or ""
        attr = rtype.lower()
        paths = list(row.change_event.changed_fields.paths) if row.change_event.changed_fields else []
        diff = {}
        for p in paths[:15]:
            old = flatten_row(row, [f"change_event.old_resource.{attr}.{p}"]).get(f"change_event.old_resource.{attr}.{p}")
            new = flatten_row(row, [f"change_event.new_resource.{attr}.{p}"]).get(f"change_event.new_resource.{attr}.{p}")
            diff[p] = {"old": _value(old, p), "new": _value(new, p)}
        item = {
            "time": base["change_event.change_date_time"], "user": base["change_event.user_email"],
            "client": base["change_event.client_type"], "resource_type": rtype,
            "operation": base["change_event.resource_change_operation"],
            "resource": base["change_event.change_resource_name"],
            "campaign": names.get(base["change_event.campaign"] or "", base["change_event.campaign"]),
            "changed": diff,
        }
        pid = ours.get(item["resource"])
        if pid:
            item["adsops_proposal_id"] = pid
        changes.append(item)
    out = compact(changes, limit=int(limit or 50))
    out.update(account_header(cid, info))
    out["window"] = {"start": start.isoformat(), "end": today.isoformat(), "days": days}
    out["by_user"] = dict(Counter(c["user"] for c in changes).most_common(10))
    out["by_resource_type"] = dict(Counter(c["resource_type"] for c in changes).most_common())
    return out
