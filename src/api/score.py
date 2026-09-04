"""Account score: 0-100 rating against Google Shopping / Ads best practices.

GetProfit-style scorecard built entirely from our read-only audits and
insights. Four pillars:

    Products  (40) — is spend turning into conversions (search terms + PMax products)
    Structure (30) — automation hygiene: PMax/DGen auto-assets off, keywords serving
    Changes   (15) — is the account actively managed
    Data      (15) — is conversion tracking alive

Also estimates the money behind the gap (wasted spend/month) and produces
a prioritized what-to-do list. Read-only.
"""

import logging
from typing import Any, Dict, List

from .audits import (
    run_change_history,
    run_conversion_health,
    run_dgen_automation,
    run_mcc_links,
    run_non_serving_keywords,
    run_pmax_assets,
    run_search_term_waste,
    _search,
    _date_clause,
)
from .insights import pmax_products_insight

logger = logging.getLogger(__name__)


def _num(value) -> float:
    """Parse a float out of display strings like '⚠ 12.34'."""
    try:
        return float(str(value).lstrip("⚠✓ ").replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _account_cost(client, customer_id: str, days: int) -> float:
    query = f"""
        SELECT metrics.cost_micros
        FROM customer
        WHERE {_date_clause(days)}
    """
    total = 0.0
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            total += r.metrics.cost_micros / 1_000_000
    return total


def compute_score(client, customer_id: str, days: int = 30) -> Dict[str, Any]:
    """Run the relevant audits and fold them into a 0-100 score."""

    def safe(runner, *args):
        try:
            return runner(client, customer_id, days, *args)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Score sub-check failed for %s: %s", customer_id, exc)
            return None

    total_cost = 0.0
    try:
        total_cost = _account_cost(client, customer_id, days)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Cost fetch failed for %s: %s", customer_id, exc)

    sw = safe(run_search_term_waste)
    pmax_settings = safe(run_pmax_assets)
    dgen = safe(run_dgen_automation)
    dead_kw = safe(run_non_serving_keywords)
    conv = safe(run_conversion_health)
    changes = safe(run_change_history)
    mcc = safe(run_mcc_links)
    products = None
    try:
        products = pmax_products_insight(client, customer_id, days)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("PMax products failed for %s: %s", customer_id, exc)

    actions: List[Dict[str, Any]] = []

    # ── Pillar 1: Products (40) — wasted spend share ──────────────
    st_waste = sum(_num(r.get("cost")) for r in (sw["rows"] if sw else []))
    pm_waste = sum(
        r["cost"] for r in (products["rows"] if products else []) if r.get("_flag")
    )
    waste = st_waste + pm_waste
    waste_share = waste / total_cost if total_cost > 0 else 0.0
    products_score = round(40 * max(0.0, 1 - 2.5 * waste_share), 1)
    if sw and sw["total_flagged"]:
        actions.append(
            {
                "impact": round(st_waste, 2),
                "title": f"{sw['total_flagged']} search terms spent {st_waste:.2f} "
                f"with 0 conversions — add negatives (Recommendations)",
                "section": "Products",
            }
        )
    if products and products.get("flagged"):
        actions.append(
            {
                "impact": round(pm_waste, 2),
                "title": f"{products['flagged']} PMax products spent {pm_waste:.2f} "
                f"with 0 conversions — review or pause (Insights → PMax Products)",
                "section": "Products",
            }
        )

    # ── Pillar 2: Structure (30) ──────────────────────────────────
    structure_score = 30.0
    if pmax_settings and pmax_settings["total_rows"]:
        bad = pmax_settings["total_flagged"]
        total = pmax_settings["total_rows"]
        structure_score -= 15 * (bad / total)
        if bad:
            actions.append(
                {
                    "impact": 0,
                    "title": f"{bad}/{total} PMax campaigns have Google auto-assets ON "
                    f"— opt out (Audits → PMax Asset Automation)",
                    "section": "Structure",
                }
            )
    if dgen and dgen["total_rows"]:
        bad = dgen["total_flagged"]
        total = dgen["total_rows"]
        structure_score -= 7.5 * (bad / total)
        if bad:
            actions.append(
                {
                    "impact": 0,
                    "title": f"{bad}/{total} Demand Gen ads have ad-level automation ON "
                    f"— opt out (Audits → Demand Gen Ad Automation)",
                    "section": "Structure",
                }
            )
    if dead_kw and dead_kw["total_rows"]:
        # total_rows here = dead keywords; penalize gently when many
        n_dead = dead_kw["total_flagged"]
        if n_dead > 20:
            structure_score -= min(7.5, 7.5 * (n_dead / 200))
            actions.append(
                {
                    "impact": 0,
                    "title": f"{n_dead} enabled keywords with 0 impressions in {days}d "
                    f"— clean up (Audits → Non-Serving Keywords)",
                    "section": "Structure",
                }
            )
    structure_score = round(max(0.0, structure_score), 1)

    # ── Pillar 3: Changes (15) — account is being managed ─────────
    n_changes = 0
    if changes and changes["rows"]:
        first = changes["rows"][0]
        # summary row: "TOTAL: N changes in Xd"
        try:
            n_changes = int(str(first.get("changed_at", "")).split(":")[1].split()[0])
        except (IndexError, ValueError):
            n_changes = 0
    if n_changes >= 10:
        changes_score = 15.0
    elif n_changes > 0:
        changes_score = 10.0
    else:
        changes_score = 0.0
        actions.append(
            {
                "impact": 0,
                "title": f"No changes recorded in {days}d — account looks unmanaged",
                "section": "Changes",
            }
        )

    # ── Pillar 4: Data (15) — conversion tracking alive ───────────
    data_score = 15.0
    if conv:
        primary = [r for r in conv["rows"] if r.get("primary") == "Yes"]
        stale = [r for r in primary if r.get("_flag")]
        if not primary:
            data_score = 0.0
            actions.append(
                {
                    "impact": 0,
                    "title": "No enabled primary conversion actions — bidding is blind",
                    "section": "Data",
                }
            )
        elif stale:
            data_score = round(15 * (1 - len(stale) / len(primary)), 1)
            actions.append(
                {
                    "impact": 0,
                    "title": f"{len(stale)}/{len(primary)} primary conversion actions "
                    f"recorded 0 conversions in {days}d — check tracking",
                    "section": "Data",
                }
            )

    # Security flag (not scored, always surfaced)
    external_mcc = mcc["total_flagged"] if mcc else 0
    if external_mcc:
        actions.append(
            {
                "impact": 0,
                "title": f"{external_mcc} ACTIVE manager link(s) outside your MCC "
                f"— verify who has access (Audits → Manager Links)",
                "section": "Security",
            }
        )

    score = round(products_score + structure_score + changes_score + data_score)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 55 else "D"

    actions.sort(key=lambda a: -a["impact"])

    return {
        "score": score,
        "grade": grade,
        "pillars": [
            {
                "key": "products",
                "label": "Products",
                "score": products_score,
                "max": 40,
                "detail": f"{waste:.2f} spent with no conversions "
                f"({waste_share * 100:.0f}% of {total_cost:.2f} total)",
            },
            {
                "key": "structure",
                "label": "Structure",
                "score": structure_score,
                "max": 30,
                "detail": "Auto-asset hygiene and keyword serving",
            },
            {
                "key": "changes",
                "label": "Changes",
                "score": changes_score,
                "max": 15,
                "detail": f"{n_changes} changes in {days}d",
            },
            {
                "key": "data",
                "label": "Data",
                "score": data_score,
                "max": 15,
                "detail": "Primary conversion actions health",
            },
        ],
        "waste_month": round(waste * 30 / days, 2),
        "waste_year": round(waste * 365 / days, 2),
        "total_cost": round(total_cost, 2),
        "external_mcc": external_mcc,
        "actions": actions[:12],
        "days": days,
    }
