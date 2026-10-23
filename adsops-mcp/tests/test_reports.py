import random
import re
from datetime import date, timedelta

from memory.brief import update_brief

CID = "1234567890"


def _daily(query):
    m = re.search(r"BETWEEN '(\S+)' AND '(\S+)'", query)
    a, b = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    rnd = random.Random(a.toordinal())
    out = []
    for i in range((b - a).days + 1):
        out.append({"segments.date": (a + timedelta(days=i)).isoformat(), "metrics.cost_micros": rnd.randint(3000, 6000) * 1_000_000,
                    "metrics.clicks": rnd.randint(300, 600), "metrics.impressions": rnd.randint(8000, 12000),
                    "metrics.conversions": float(rnd.randint(8, 25)), "metrics.conversions_value": float(rnd.randint(30000, 90000))})
    return out


def _campaigns(query):
    rnd = random.Random(len(query))
    names = [("Search | Бренд", "SEARCH"), ("Search | Дивани", "SEARCH"), ("PMax | Всі товари", "PERFORMANCE_MAX"),
             ("Demand Gen | Ремаркетинг", "DEMAND_GEN"), ("Search | Ліжка", "SEARCH")]
    return [{"campaign.id": i + 1, "campaign.name": n, "campaign.status": "ENABLED", "campaign.advertising_channel_type": ch,
             "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE", "campaign_budget.amount_micros": 2_000_000_000,
             "campaign.primary_status": "ELIGIBLE", "metrics.cost_micros": rnd.randint(10_000, 60_000) * 1_000_000,
             "metrics.clicks": 1000, "metrics.impressions": 30000, "metrics.conversions": float(rnd.randint(10, 120)),
             "metrics.conversions_value": float(rnd.randint(50_000, 400_000))} for i, (n, ch) in enumerate(names)]


def test_generate_report_files(auto, tmp_path):
    auto.route(r"segments.date, metrics.cost_micros.*FROM customer", _daily)
    auto.route(r"FROM campaign WHERE segments.date BETWEEN", _campaigns)
    update_brief(CID, {"target_roas": 4.0, "kpi": "roas", "monthly_budget": 150000})
    from reports.tools import generate_report
    out = generate_report(CID, period="2026-09-01..2026-09-30", lang="uk", xlsx=True, out_dir=str(tmp_path / "r"))
    assert "error" not in out, out
    html = open(out["files"]["html"], encoding="utf-8").read()
    md = open(out["files"]["markdown"], encoding="utf-8").read()
    assert "data:image/png;base64" in html and "Ключові показники" in md and "Search | Бренд" in md
    assert len(out["files"]["charts"]) == 4 and out["files"]["xlsx"].endswith(".xlsx")
    out_en = generate_report(CID, period="LAST_MONTH", lang="en", out_dir=str(tmp_path / "e"), include_next_steps=False)
    assert "Key metrics" in open(out_en["files"]["markdown"], encoding="utf-8").read()
