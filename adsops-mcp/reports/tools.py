"""Client report tool."""
from __future__ import annotations

from typing import Any

from reports.builder import build_report
from tools._registry import tool


@tool(title="Generate client report")
def generate_report(customer_id: str, period: str = "LAST_MONTH", lang: str = "uk", xlsx: bool = False,
                    include_next_steps: bool = True, out_dir: str | None = None) -> dict[str, Any]:
    """Client-ready report for a period (preset like LAST_MONTH / LAST_30_DAYS or "YYYY-MM-DD..YYYY-MM-DD"):
    KPIs vs previous period and brief targets, charts (daily cost, conversions, rolling CPA/ROAS vs target, cost by
    campaign), channel + campaign tables, wins / losses, actions taken (adsops audit log + decision log) and next
    steps. Writes report.md + self-contained report.html (+ report.xlsx) and returns their paths plus the data.
    lang: uk | en."""
    return build_report(customer_id, period, lang, xlsx, include_next_steps, out_dir)
