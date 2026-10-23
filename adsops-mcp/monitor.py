"""Scheduled, read-only monitoring for every account under the MCC -> Telegram alerts.

    python monitor.py                      # all checks, all accounts, send to Telegram
    python monitor.py --dry                # print instead of sending
    python monitor.py --checks anomalies,pacing --accounts stryi,abtime

cron (every day 09:15 Kyiv):   15 9 * * *  cd /path/adsops-mcp && .venv/bin/python monitor.py
Windows Task Scheduler: Program = path\\to\\.venv\\Scripts\\python.exe, Arguments = monitor.py, Start in = adsops-mcp folder.

Never mutates anything: it only calls read tools. Alerts are de-duplicated per account/check/day.
"""
from __future__ import annotations

import argparse
import html
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import accounts, db  # noqa: E402
from core.config import get_settings  # noqa: E402

log = logging.getLogger("adsops.monitor")
CHECKS = ("anomalies", "pacing", "conversions")
ICON = {"high": "🔴", "critical": "🔴", "medium": "🟠", "low": "🟡"}


def check_anomalies(cid: str) -> list[dict[str, Any]]:
    from tools.anomaly import scan_account
    out = scan_account(cid, by_campaign=True, top_campaigns=5)
    alerts = []
    for a in out["anomalies"]:
        where = a.get("campaign") or "account"
        alerts.append({"severity": a["severity"], "key": f"anom:{where}:{a['metric']}:{a['window']}",
                       "text": f"{where}: {a['metric']} {a['direction']} {a['change_pct']:+}% ({a['window']}: {a['value']} vs {a['baseline']}, z={a['z']})"})
    for z in out["zero_conversion_days"]:
        where = z.get("campaign") or "account"
        alerts.append({"severity": z["severity"], "key": f"zero:{where}:{z['date']}",
                       "text": f"{where}: 0 conversions on {z['date']} (spend {z['spend']}, expected {z['expected_conversions']})"})
    return alerts


def check_pacing(cid: str) -> list[dict[str, Any]]:
    from tools.pacing import pace_account
    p = pace_account(cid)
    alerts = []
    if p.get("status") in ("overpacing", "underpacing") and abs((p.get("pacing_pct") or 100) - 100) >= 15:
        alerts.append({"severity": "high" if abs(p["pacing_pct"] - 100) >= 25 else "medium", "key": f"pace:{p['status']}",
                       "text": f"{p['status']}: projected {p['projected_eom']} vs target {p['monthly_target']} {p['currency']} "
                               f"({p['pacing_pct']}%); recommended daily {p.get('recommended_daily_spend')}"})
    limited = [c for c in p.get("limited_by_budget", []) if (c.get("lost_is_budget") or 0) >= 20]
    if limited:
        alerts.append({"severity": "low", "key": "pace:limited",
                       "text": "limited by budget: " + ", ".join(f"{c['campaign']} (lost IS {c['lost_is_budget']}%)" for c in limited[:3])})
    return alerts


def check_conversions(cid: str) -> list[dict[str, Any]]:
    from tools.conversions import audit_conversion_tracking
    out = audit_conversion_tracking.raw(cid, lookback_days=30)
    if "error" in out:
        return [{"severity": "medium", "key": "conv:error", "text": f"conversion audit failed: {out['error'][:150]}"}]
    keep = ("no conversions in 7 days", "Auto-tagging is OFF", "No enabled primary", "PRIMARY purchase actions")
    return [{"severity": f["severity"], "key": f"conv:{f['issue'][:60]}", "text": f["issue"]}
            for f in out["findings"] if f["severity"] in ("critical", "high") and any(k in f["issue"] for k in keep)]


def run(account_ids: list[str] | None, checks: list[str], dry: bool, force: bool) -> dict[str, Any]:
    from tools.accounts import list_accounts
    accs = list_accounts.raw()["accounts"]
    if account_ids:
        wanted = {accounts.resolve(a) for a in account_ids}
        accs = [a for a in accs if a["customer_id"] in wanted]
    today = date.today().isoformat()
    report: list[str] = []
    sent = 0
    for a in accs:
        cid = a["customer_id"]
        label = a.get("alias") or a["name"]
        alerts: list[dict[str, Any]] = []
        for chk in checks:
            try:
                alerts += {"anomalies": check_anomalies, "pacing": check_pacing, "conversions": check_conversions}[chk](cid)
            except Exception as ex:  # noqa: BLE001 - keep monitoring the other accounts
                log.exception("%s %s failed", cid, chk)
                alerts.append({"severity": "medium", "key": f"err:{chk}", "text": f"{chk} check failed: {str(ex)[:150]}"})
        fresh = []
        with db.connect() as conn:
            for al in alerts:
                key = f"{today}:{cid}:{al['key']}"
                if not force and conn.execute("SELECT 1 FROM alert_sent WHERE key=?", (key,)).fetchone():
                    continue
                fresh.append(al)
                if not dry:
                    conn.execute("INSERT OR IGNORE INTO alert_sent(key, ts) VALUES(?,?)", (key, db.now_iso()))
        if fresh:
            order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            fresh.sort(key=lambda x: order.get(x["severity"], 9))
            lines = [f"<b>{html.escape(label)}</b> ({a['id']}, {a['currency']})"]
            lines += [f"{ICON.get(x['severity'], '•')} {html.escape(x['text'])}" for x in fresh[:12]]
            if len(fresh) > 12:
                lines.append(f"… +{len(fresh) - 12} more")
            report.append("\n".join(lines))
            sent += len(fresh)
    if not report:
        return {"accounts": len(accs), "alerts": 0}
    header = f"📊 adsops monitor · {today} · {sent} alert(s)"
    message = header + "\n\n" + "\n\n".join(report)
    if dry:
        print(message)
    else:
        send_telegram(message)
    return {"accounts": len(accs), "alerts": sent}


def send_telegram(text: str) -> None:
    import httpx
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env (use --dry to print).")
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for chunk in chunks:
        r = httpx.post(url, data={"chat_id": s.telegram_chat_id, "text": chunk, "parse_mode": "HTML",
                                  "disable_web_page_preview": "true"}, timeout=30)
        r.raise_for_status()


def main() -> None:
    p = argparse.ArgumentParser(description="adsops read-only monitor")
    p.add_argument("--accounts", help="comma-separated IDs/aliases (default: all enabled accounts)")
    p.add_argument("--checks", default=",".join(CHECKS), help=f"comma-separated subset of {CHECKS}")
    p.add_argument("--dry", action="store_true", help="print instead of sending to Telegram")
    p.add_argument("--force", action="store_true", help="ignore today's de-duplication")
    a = p.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    checks = [c.strip() for c in a.checks.split(",") if c.strip() in CHECKS]
    out = run([x.strip() for x in a.accounts.split(",")] if a.accounts else None, checks, a.dry, a.force)
    log.info("done: %s", out)


if __name__ == "__main__":
    main()
