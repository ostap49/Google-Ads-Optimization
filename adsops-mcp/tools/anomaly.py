"""Statistical anomaly scan: yesterday and last 7 days vs a 28-day baseline."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

from core.config import get_settings
from core.dates import DateRange, today_in
from core.fmt import from_micros, r
from tools._registry import tool
from tools.common import METRICS, account_header, ctx


def _series(rows: list[dict[str, Any]], days: list[str]) -> dict[str, dict[str, float]]:
    s = {d: {"cost": 0.0, "clicks": 0.0, "impressions": 0.0, "conversions": 0.0, "value": 0.0} for d in days}
    for x in rows:
        d = x.get("segments.date")
        if d not in s:
            continue
        s[d]["cost"] += from_micros(x.get("metrics.cost_micros") or 0, 4) or 0
        s[d]["clicks"] += x.get("metrics.clicks") or 0
        s[d]["impressions"] += x.get("metrics.impressions") or 0
        s[d]["conversions"] += x.get("metrics.conversions") or 0
        s[d]["value"] += x.get("metrics.conversions_value") or 0
    return s


def _sum(s: dict[str, dict[str, float]], days: list[str]) -> dict[str, float]:
    out = defaultdict(float)
    for d in days:
        for k, v in s[d].items():
            out[k] += v
    return dict(out)


def _mean_sd(values: list[float]) -> tuple[float, float]:
    n = len(values)
    mu = sum(values) / n if n else 0.0
    var = sum((v - mu) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    return mu, math.sqrt(var)


def _prop_z(k1: float, n1: float, k0: float, n0: float) -> tuple[float | None, float | None, float | None]:
    """z for observed rate k1/n1 vs baseline rate k0/n0 (baseline treated as known)."""
    if n1 <= 0 or n0 <= 0:
        return None, None, None
    p1, p0 = k1 / n1, k0 / n0
    if p0 <= 0 or p0 >= 1:
        return None, p1, p0
    return (p1 - p0) / math.sqrt(p0 * (1 - p0) / n1), p1, p0


def scan(s: dict[str, dict[str, float]], base_days: list[str], recent: list[str], label: str,
         z_thr: float, min_pct: float) -> list[dict[str, Any]]:
    """Compare a window (`recent`: one day or seven) with the baseline days."""
    out: list[dict[str, Any]] = []
    n = len(recent)
    base = _sum(s, base_days)
    cur = _sum(s, recent)

    def add(metric: str, cur_v: float | None, base_v: float | None, z: float | None, note: str = "") -> None:
        if cur_v is None or base_v in (None, 0) or z is None:
            return
        pct = (cur_v / base_v - 1) * 100
        if abs(z) >= z_thr and abs(pct) >= min_pct:
            out.append({"window": label, "metric": metric, "value": r(cur_v, 4 if metric in ("ctr", "conv_rate") else 2),
                        "baseline": r(base_v, 4 if metric in ("ctr", "conv_rate") else 2), "change_pct": r(pct, 1),
                        "z": r(z, 2), "severity": "high" if abs(z) >= 4 else "medium", "direction": "up" if pct > 0 else "down",
                        **({"note": note} if note else {})})

    # spend & conversions per day: z vs daily distribution (mean of n days has sd/sqrt(n))
    for metric in ("cost", "conversions", "clicks"):
        mu, sd = _mean_sd([s[d][metric] for d in base_days])
        avg = cur[metric] / n
        if sd > 0:
            add(metric if metric != "cost" else "spend", avg, mu, (avg - mu) / (sd / math.sqrt(n)), "daily average")
    z, p1, p0 = _prop_z(cur["clicks"], cur["impressions"], base["clicks"], base["impressions"])
    add("ctr", p1 * 100 if p1 is not None else None, p0 * 100 if p0 else None, z)
    z, p1, p0 = _prop_z(cur["conversions"], cur["clicks"], base["conversions"], base["clicks"])
    add("conv_rate", p1 * 100 if p1 is not None else None, p0 * 100 if p0 else None, z)
    if base["conversions"] > 0 and cur["cost"] > 0:
        cpa_b = base["cost"] / base["conversions"]
        expected = cur["cost"] / cpa_b
        if expected > 0 and cur["conversions"] > 0:
            z_conv = (cur["conversions"] - expected) / math.sqrt(expected)
            add("cpa", cur["cost"] / cur["conversions"], cpa_b, -z_conv, "Poisson test on conversions given spend")
    return out


def zero_conv_days(s: dict[str, dict[str, float]], base_days: list[str], recent: list[str]) -> list[dict[str, Any]]:
    mu, _ = _mean_sd([s[d]["conversions"] for d in base_days])
    cost_mu, _ = _mean_sd([s[d]["cost"] for d in base_days])
    out = []
    for d in recent:
        if s[d]["conversions"] == 0 and s[d]["cost"] > 0:
            lam = mu * (s[d]["cost"] / cost_mu) if cost_mu else mu  # expected conversions given that day's spend
            p = math.exp(-lam)
            if p < 0.05:
                out.append({"date": d, "spend": r(s[d]["cost"]), "expected_conversions": r(lam, 1),
                            "p_zero": r(p, 4), "severity": "high" if p < 0.005 else "medium"})
    return out


def scan_account(customer_id: str, by_campaign: bool = True, top_campaigns: int = 10,
                 z_threshold: float | None = None, min_change_pct: float | None = None) -> dict[str, Any]:
    th = get_settings().thresholds()
    z_thr = float(z_threshold if z_threshold is not None else th["anomaly_z"])
    min_pct = float(min_change_pct if min_change_pct is not None else th["anomaly_min_pct"])
    g, cid, info, _ = ctx(customer_id)
    today = today_in(info.get("time_zone"))
    y = today - timedelta(days=1)
    days = [(y - timedelta(days=i)).isoformat() for i in range(34, -1, -1)]  # 35 days, oldest first
    base_days, last7, yday = days[:28], days[28:], days[-1:]
    dr = DateRange(y - timedelta(days=34), y, "LAST_35_DAYS")

    rows = g.search(cid, f"SELECT segments.date, {METRICS} FROM customer WHERE {dr.gaql()}")
    s = _series(rows, days)
    anomalies = scan(s, base_days, yday, "yesterday", z_thr, min_pct) + scan(s, base_days, last7, "last_7d", z_thr, min_pct)
    for a in anomalies:
        a["scope"] = "account"
    zeros = zero_conv_days(s, base_days, last7)

    if by_campaign:
        crow = g.search(cid, f"SELECT campaign.id, campaign.name, segments.date, {METRICS} FROM campaign "
                             f"WHERE {dr.gaql()} AND campaign.status != 'REMOVED'")
        per: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for x in crow:
            per[(str(x["campaign.id"]), x["campaign.name"])].append(x)
        ranked = sorted(per.items(), key=lambda kv: -sum(x.get("metrics.cost_micros") or 0 for x in kv[1]))[:top_campaigns]
        for (camp_id, name), v in ranked:
            cs = _series(v, days)
            for a in scan(cs, base_days, yday, "yesterday", z_thr, min_pct) + scan(cs, base_days, last7, "last_7d", z_thr, min_pct):
                a["scope"] = "campaign"
                a["campaign"] = name
                a["campaign_id"] = camp_id
                anomalies.append(a)
            for zd in zero_conv_days(cs, base_days, last7):
                zeros.append({**zd, "campaign": name, "campaign_id": camp_id})

    sev = {"high": 0, "medium": 1}
    anomalies.sort(key=lambda a: (sev[a["severity"]], -abs(a["z"])))
    bt = _sum(s, base_days)
    out = account_header(cid, info)
    out.update({
        "as_of": y.isoformat(),
        "baseline": {"start": base_days[0], "end": base_days[-1], "days": 28,
                     "daily_spend": r(bt["cost"] / 28), "daily_conversions": r(bt["conversions"] / 28, 2),
                     "cpa": r(bt["cost"] / bt["conversions"]) if bt["conversions"] else None},
        "yesterday": {k: r(v) for k, v in _sum(s, yday).items()},
        "last_7d": {k: r(v) for k, v in _sum(s, last7).items()},
        "anomalies": anomalies[:30],
        "anomaly_count": len(anomalies),
        "zero_conversion_days": zeros[:20],
        "thresholds": {"z": z_thr, "min_change_pct": min_pct},
        "method": "Daily-mean z for spend/clicks/conversions; proportion z for CTR & conv rate; Poisson z for CPA; "
                  "zero-conversion days flagged when P(0 | baseline rate) < 5%. Flag = |z| >= z AND |change| >= min_change_pct.",
    })
    return out


@tool(title="Anomaly scan")
def anomaly_scan(customer_id: str = "all", by_campaign: bool = True, top_campaigns: int = 10,
                 z_threshold: float | None = None, min_change_pct: float | None = None) -> dict[str, Any]:
    """Compare yesterday and the last 7 days against the preceding 28-day baseline for spend, conversions,
    clicks, CPA, CTR and conversion rate; flag zero-conversion days that are improbable given the baseline.
    Only statistically meaningful swings are flagged (|z| >= thresholds.anomaly_z, default 2.5, AND
    |change| >= thresholds.anomaly_min_pct, default 25%). customer_id="all" scans every account (account level)."""
    if customer_id != "all":
        return scan_account(customer_id, by_campaign, top_campaigns, z_threshold, min_change_pct)
    from tools.accounts import list_accounts
    res = []
    for a in list_accounts.raw()["accounts"]:
        try:
            out = scan_account(a["customer_id"], False, 0, z_threshold, min_change_pct)
            res.append({"customer_id": a["customer_id"], "account": a["name"], "alias": a.get("alias"),
                        "anomalies": out["anomalies"][:8], "zero_conversion_days": out["zero_conversion_days"][:5]})
        except Exception as ex:  # noqa: BLE001
            res.append({"customer_id": a["customer_id"], "account": a["name"], "error": str(ex)[:200]})
    res.sort(key=lambda x: -(len(x.get("anomalies", [])) + len(x.get("zero_conversion_days", []))))
    return {"accounts": res, "count": len(res)}
