"""Month-to-date budget pacing."""
from __future__ import annotations

import calendar
from datetime import timedelta
from typing import Any

from core.dates import DateRange, today_in
from core.fmt import from_micros, r
from memory.brief import targets
from tools._registry import tool
from tools.common import IS_METRICS, METRICS, account_header, ctx, is_metrics, m

ON_TRACK_PCT = 10.0


def _campaign_query(dr: DateRange) -> str:
    return (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign.primary_status, campaign.primary_status_reasons, campaign_budget.amount_micros, "
        f"campaign_budget.explicitly_shared, {METRICS}, {IS_METRICS} FROM campaign "
        f"WHERE {dr.gaql()} AND campaign.status != 'REMOVED'"
    )


def pace_account(customer_id: str, monthly_target: float | None = None) -> dict[str, Any]:
    g, cid, info, _ = ctx(customer_id)
    t = targets(cid)
    today = today_in(info.get("time_zone"))
    dim = calendar.monthrange(today.year, today.month)[1]
    first = today.replace(day=1)
    remaining = dim - today.day + 1  # incl. today
    mtd = DateRange(first, today, "MTD")
    last7 = DateRange(today - timedelta(days=7), today - timedelta(days=1), "LAST_7_DAYS")

    cur = g.search(cid, _campaign_query(mtd))
    daily_today = g.search(cid, "SELECT campaign.id, metrics.cost_micros FROM campaign "
                                f"WHERE segments.date = '{today.isoformat()}' AND campaign.status != 'REMOVED'")
    seven = g.search(cid, f"SELECT campaign.id, metrics.cost_micros FROM campaign WHERE {last7.gaql()} AND campaign.status != 'REMOVED'")
    today_cost = {str(x["campaign.id"]): from_micros(x["metrics.cost_micros"], 4) or 0 for x in daily_today}
    avg7 = {str(x["campaign.id"]): (from_micros(x["metrics.cost_micros"], 4) or 0) / 7 for x in seven}

    campaigns = []
    for x in cur:
        cid_ = str(x["campaign.id"])
        k = m(x)
        spent_ex_today = (k["cost"] or 0) - today_cost.get(cid_, 0)
        budget = from_micros(x.get("campaign_budget.amount_micros")) or 0
        run_rate = avg7.get(cid_, 0)
        if x["campaign.status"] != "ENABLED":
            run_rate = 0
        projected = spent_ex_today + run_rate * remaining
        reasons = x.get("campaign.primary_status_reasons") or []
        isx = is_metrics(x)
        limited = "BUDGET_CONSTRAINED" in reasons or (isx.get("lost_is_budget") or 0) >= 10
        item = {
            "campaign_id": cid_, "campaign": x["campaign.name"], "status": x["campaign.status"],
            "channel": x["campaign.advertising_channel_type"], "daily_budget": budget,
            "shared_budget": x.get("campaign_budget.explicitly_shared"),
            "mtd_cost": k["cost"], "avg_daily_7d": r(run_rate), "projected_eom": r(projected),
            "max_possible_eom": r(spent_ex_today + budget * remaining) if x["campaign.status"] == "ENABLED" else r(spent_ex_today),
            "limited_by_budget": limited, **isx, "conversions": k["conversions"], "cpa": k["cpa"], "roas": k["roas"],
        }
        ct = (t.get("campaign_budgets") or {}).get(cid_)
        if ct:
            item["monthly_target"] = ct
            item["pacing_pct"] = r(projected / float(ct) * 100, 1)
        if (k["cost"] or 0) > 0 or x["campaign.status"] == "ENABLED":
            campaigns.append(item)
    campaigns.sort(key=lambda c: -(c["mtd_cost"] or 0))

    mtd_cost = sum(c["mtd_cost"] or 0 for c in campaigns)
    spent_ex_today = mtd_cost - sum(today_cost.values())
    run_rate = sum(avg7.get(c["campaign_id"], 0) for c in campaigns if c["status"] == "ENABLED")
    projected = spent_ex_today + run_rate * remaining
    target = monthly_target or t.get("monthly_budget")
    out = account_header(cid, info)
    out.update({
        "as_of": today.isoformat(), "day_of_month": today.day, "days_in_month": dim, "days_remaining_incl_today": remaining,
        "mtd_cost": r(mtd_cost), "avg_daily_7d": r(run_rate), "projected_eom": r(projected),
        "sum_daily_budgets_enabled": r(sum(c["daily_budget"] or 0 for c in campaigns if c["status"] == "ENABLED")),
        "monthly_target": target,
    })
    if target:
        target = float(target)
        pacing = projected / target * 100
        out["pacing_pct"] = r(pacing, 1)
        out["status"] = "on_track" if abs(pacing - 100) <= ON_TRACK_PCT else ("overpacing" if pacing > 100 else "underpacing")
        out["ideal_mtd_cost"] = r(target * (today.day - 1) / dim)
        out["recommended_daily_spend"] = r(max(0.0, target - spent_ex_today) / remaining)
        out["recommended_vs_current_daily_pct"] = r((out["recommended_daily_spend"] / run_rate - 1) * 100, 1) if run_rate else None
    else:
        out["status"] = "no_target"
        out["hint"] = "Set monthly_budget in the brief (update_account_brief) or pass monthly_target."
    out["limited_by_budget"] = [
        {"campaign": c["campaign"], "lost_is_budget": c["lost_is_budget"], "lost_is_rank": c["lost_is_rank"],
         "cpa": c["cpa"], "roas": c["roas"]} for c in campaigns if c["limited_by_budget"]]
    out["campaigns"] = campaigns[:25]
    out["truncated"] = len(campaigns) > 25
    return out


@tool(title="Budget pacing")
def budget_pacing(customer_id: str = "all", monthly_target: float | None = None) -> dict[str, Any]:
    """Month-to-date spend vs monthly target (brief.monthly_budget or monthly_target), projected end-of-month
    (MTD excl. today + 7-day run rate x days left), recommended daily spend, campaigns limited by budget, and
    lost IS (budget) vs lost IS (rank). customer_id="all" paces every enabled account under the MCC."""
    if customer_id != "all":
        return pace_account(customer_id, monthly_target)
    from tools.accounts import list_accounts
    accounts = list_accounts.raw()["accounts"]
    rows = []
    for a in accounts:
        try:
            p = pace_account(a["customer_id"])
        except Exception as ex:  # noqa: BLE001 - one failing account should not hide the others
            rows.append({"customer_id": a["customer_id"], "account": a["name"], "error": str(ex)[:200]})
            continue
        rows.append({k: p.get(k) for k in ("customer_id", "account", "alias", "currency", "mtd_cost", "projected_eom",
                                           "monthly_target", "pacing_pct", "status", "recommended_daily_spend")}
                    | {"limited_by_budget": len(p["limited_by_budget"])})
    order = {"overpacing": 0, "underpacing": 1, "on_track": 2, "no_target": 3}
    rows.sort(key=lambda x: (order.get(x.get("status"), 4), -(abs((x.get("pacing_pct") or 100) - 100))))
    return {"accounts": rows, "count": len(rows)}
