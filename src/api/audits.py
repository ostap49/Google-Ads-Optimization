"""Read-only account audits based on the ppc-ai-skills methodologies.

Each audit is a registry entry with display metadata and a runner:
    run(client, customer_id, days) -> {"rows": [...], "total_rows": int,
                                       "total_flagged": int}

Rows are display-ready dicts keyed by the audit's column keys, plus a
"_flag" bool. Values starting with "⚠" render red in the UI, "✓" green.
All audits are GAQL SELECT only — nothing is ever mutated.
"""

import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, List

from google.ads.googleads.client import GoogleAdsClient

logger = logging.getLogger(__name__)

MAX_ROWS = 100  # per-account row cap in responses

# The five PMax asset automation settings (Google default: OPTED_IN)
PMAX_AUTOMATION_TYPES = {
    "TEXT_ASSET_AUTOMATION": "Auto text",
    "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "URL expansion",
    "GENERATE_ENHANCED_YOUTUBE_VIDEOS": "Auto videos",
    "GENERATE_IMAGE_ENHANCEMENT": "Image crop",
    "GENERATE_IMAGE_EXTRACTION": "Image extraction",
}


def _date_clause(days: int) -> str:
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    return f"segments.date BETWEEN '{start}' AND '{end}'"


def _search(client: GoogleAdsClient, customer_id: str, query: str):
    service = client.get_service("GoogleAdsService")
    return service.search_stream(
        customer_id=customer_id.replace("-", ""), query=query
    )


def _result(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    flagged = sum(1 for r in rows if r.get("_flag"))
    return {
        "rows": rows[:MAX_ROWS],
        "total_rows": len(rows),
        "total_flagged": flagged,
    }


def _enum_name(value, default: str = "") -> str:
    """Enum .name, tolerating values newer than the client library (raw ints)."""
    return getattr(value, "name", default)


# ── PMax asset automation ─────────────────────────────────────────


def run_pmax_assets(client, customer_id, days) -> Dict[str, Any]:
    query = """
        SELECT campaign.id, campaign.name, campaign.asset_automation_settings
        FROM campaign
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          AND campaign.status = 'ENABLED'
    """
    rows = []
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            settings = {t: "OPTED_IN" for t in PMAX_AUTOMATION_TYPES}
            for s in r.campaign.asset_automation_settings:
                t = _enum_name(s.asset_automation_type)
                st = _enum_name(s.asset_automation_status)
                if t in settings and st:
                    settings[t] = st
            flagged = [t for t, st in settings.items() if st != "OPTED_OUT"]
            row = {"campaign": r.campaign.name, "_flag": bool(flagged)}
            for t, label in PMAX_AUTOMATION_TYPES.items():
                row[t] = "✓ OFF" if settings[t] == "OPTED_OUT" else "⚠ ON"
            rows.append(row)
    return _result(rows)


# ── Demand Gen ad-level automation ────────────────────────────────


def run_dgen_automation(client, customer_id, days) -> Dict[str, Any]:
    query = """
        SELECT campaign.name, ad_group.name, ad_group_ad.ad.id,
               ad_group_ad.ad_group_ad_asset_automation_settings
        FROM ad_group_ad
        WHERE campaign.advertising_channel_type = 'DEMAND_GEN'
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND ad_group_ad.status = 'ENABLED'
    """
    rows = []
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            on = []
            for s in r.ad_group_ad.ad_group_ad_asset_automation_settings:
                t = _enum_name(s.asset_automation_type)
                st = _enum_name(s.asset_automation_status)
                if t and st == "OPTED_IN":
                    on.append(t.replace("_", " ").title())
            rows.append(
                {
                    "campaign": r.campaign.name,
                    "ad_group": r.ad_group.name,
                    "ad_id": str(r.ad_group_ad.ad.id),
                    "automation": "⚠ " + ", ".join(on) if on else "✓ All off",
                    "_flag": bool(on),
                }
            )
    return _result(rows)


# ── Non-serving keywords ──────────────────────────────────────────


def run_non_serving_keywords(client, customer_id, days) -> Dict[str, Any]:
    all_kw_query = """
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.criterion_id,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status = 'ENABLED'
          AND ad_group_criterion.negative = FALSE
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
    """
    serving_query = f"""
        SELECT ad_group_criterion.criterion_id, ad_group.id
        FROM keyword_view
        WHERE {_date_clause(days)}
          AND metrics.impressions > 0
    """
    all_kws = {}
    for batch in _search(client, customer_id, all_kw_query):
        for r in batch.results:
            key = f"{r.ad_group.name}~{r.ad_group_criterion.criterion_id}"
            all_kws[key] = {
                "campaign": r.campaign.name,
                "ad_group": r.ad_group.name,
                "keyword": r.ad_group_criterion.keyword.text,
                "match_type": _enum_name(
                    r.ad_group_criterion.keyword.match_type, "?"
                ),
                "_flag": True,
            }
    served = set()
    for batch in _search(client, customer_id, serving_query):
        for r in batch.results:
            served.add(str(r.ad_group_criterion.criterion_id))

    rows = [
        row
        for key, row in all_kws.items()
        if key.split("~")[1] not in served
    ]
    return _result(rows)


# ── Conversion tracking health ────────────────────────────────────


def run_conversion_health(client, customer_id, days) -> Dict[str, Any]:
    actions_query = """
        SELECT conversion_action.name, conversion_action.type,
               conversion_action.status, conversion_action.primary_for_goal
        FROM conversion_action
        WHERE conversion_action.status = 'ENABLED'
    """
    volume_query = f"""
        SELECT segments.conversion_action_name, metrics.all_conversions
        FROM customer
        WHERE {_date_clause(days)}
    """
    volumes: Dict[str, float] = {}
    for batch in _search(client, customer_id, volume_query):
        for r in batch.results:
            name = r.segments.conversion_action_name
            volumes[name] = volumes.get(name, 0.0) + r.metrics.all_conversions

    rows = []
    for batch in _search(client, customer_id, actions_query):
        for r in batch.results:
            ca = r.conversion_action
            conv = volumes.get(ca.name, 0.0)
            primary = bool(ca.primary_for_goal)
            stale = primary and conv == 0
            rows.append(
                {
                    "action": ca.name,
                    "type": _enum_name(ca.type_, "?"),
                    "primary": "Yes" if primary else "No",
                    "conversions": f"⚠ 0 in {days}d" if stale else f"{conv:.1f}",
                    "_flag": stale,
                }
            )
    rows.sort(key=lambda r: (not r["_flag"], r["action"]))
    return _result(rows)


# ── Search term waste ─────────────────────────────────────────────


def run_search_term_waste(client, customer_id, days) -> Dict[str, Any]:
    query = f"""
        SELECT campaign.name, ad_group.name, search_term_view.search_term,
               metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE {_date_clause(days)}
          AND metrics.clicks > 0
          AND campaign.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
    """
    rows = []
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            cost = r.metrics.cost_micros / 1_000_000
            if r.metrics.conversions > 0 or cost < 5.0:
                continue
            rows.append(
                {
                    "search_term": r.search_term_view.search_term,
                    "campaign": r.campaign.name,
                    "clicks": r.metrics.clicks,
                    "cost": f"⚠ {cost:.2f}",
                    "_cost": cost,
                    "_flag": True,
                }
            )
    rows.sort(key=lambda r: -r["_cost"])
    for r in rows:
        r.pop("_cost", None)
    return _result(rows)


# ── Change history ────────────────────────────────────────────────


def run_change_history(client, customer_id, days) -> Dict[str, Any]:
    start = (date.today() - timedelta(days=min(days, 90))).isoformat()
    query = f"""
        SELECT change_status.last_change_date_time,
               change_status.resource_type,
               change_status.resource_status
        FROM change_status
        WHERE change_status.last_change_date_time >= '{start} 00:00:00'
        ORDER BY change_status.last_change_date_time DESC
        LIMIT 10000
    """
    counts: Dict[str, int] = {}
    latest: List[Dict[str, Any]] = []
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            rtype = _enum_name(r.change_status.resource_type, "OTHER")
            counts[rtype] = counts.get(rtype, 0) + 1
            if len(latest) < MAX_ROWS:
                latest.append(
                    {
                        "changed_at": r.change_status.last_change_date_time[:19],
                        "resource_type": rtype,
                        "status": _enum_name(
                            r.change_status.resource_status, "?"
                        ),
                        "_flag": False,
                    }
                )
    total = sum(counts.values())
    summary_row = {
        "changed_at": f"TOTAL: {total} changes in {days}d",
        "resource_type": ", ".join(
            f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])
        )[:200],
        "status": "",
        "_flag": total == 0,
    }
    return _result([summary_row] + latest)


# ── External MCC links ────────────────────────────────────────────


def run_mcc_links(client, customer_id, days) -> Dict[str, Any]:
    own_mcc = str(getattr(client, "login_customer_id", "") or "")
    query = """
        SELECT customer_manager_link.manager_customer,
               customer_manager_link.status
        FROM customer_manager_link
    """
    rows = []
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            link = r.customer_manager_link
            manager_id = link.manager_customer.split("/")[-1]
            status = _enum_name(link.status, "?")
            external = manager_id != own_mcc and status == "ACTIVE"
            rows.append(
                {
                    "manager_id": manager_id,
                    "status": status,
                    "verdict": "⚠ EXTERNAL manager" if external else "✓ Internal",
                    "_flag": external,
                }
            )
    return _result(rows)


# ── Registry ──────────────────────────────────────────────────────

AUDITS: Dict[str, Dict[str, Any]] = {
    "pmax-assets": {
        "label": "PMax Asset Automation",
        "description": (
            "Five auto-asset settings per PMax campaign (auto text, URL "
            "expansion, auto videos, image crop, image extraction). "
            "Standard: all opted out."
        ),
        "uses_dates": False,
        "columns": [
            {"key": "campaign", "label": "Campaign"},
            *[
                {"key": t, "label": lbl}
                for t, lbl in PMAX_AUTOMATION_TYPES.items()
            ],
        ],
        "run": run_pmax_assets,
    },
    "dgen-automation": {
        "label": "Demand Gen Ad Automation",
        "description": (
            "Ad-level asset automation on Demand Gen ads (image design "
            "versions, auto videos, etc.) — settings Google enables by "
            "default and campaign-level audits can't see."
        ),
        "uses_dates": False,
        "columns": [
            {"key": "campaign", "label": "Campaign"},
            {"key": "ad_group", "label": "Ad Group"},
            {"key": "ad_id", "label": "Ad ID"},
            {"key": "automation", "label": "Automation"},
        ],
        "run": run_dgen_automation,
    },
    "non-serving-keywords": {
        "label": "Non-Serving Keywords",
        "description": (
            "Enabled keywords with zero impressions over the selected "
            "period — dead weight that clutters the account."
        ),
        "uses_dates": True,
        "columns": [
            {"key": "keyword", "label": "Keyword"},
            {"key": "match_type", "label": "Match"},
            {"key": "campaign", "label": "Campaign"},
            {"key": "ad_group", "label": "Ad Group"},
        ],
        "run": run_non_serving_keywords,
    },
    "conversion-health": {
        "label": "Conversion Tracking Health",
        "description": (
            "Enabled conversion actions and their volume over the period. "
            "Flags primary actions that recorded zero conversions — "
            "possible broken tracking."
        ),
        "uses_dates": True,
        "columns": [
            {"key": "action", "label": "Conversion Action"},
            {"key": "type", "label": "Type"},
            {"key": "primary", "label": "Primary"},
            {"key": "conversions", "label": "Conversions"},
        ],
        "run": run_conversion_health,
    },
    "search-term-waste": {
        "label": "Search Term Waste",
        "description": (
            "Search terms that spent money (≥5 in account currency) with "
            "zero conversions over the period, sorted by spend. Review "
            "before adding negatives — brand terms can look like waste."
        ),
        "uses_dates": True,
        "columns": [
            {"key": "search_term", "label": "Search Term"},
            {"key": "campaign", "label": "Campaign"},
            {"key": "clicks", "label": "Clicks"},
            {"key": "cost", "label": "Cost"},
        ],
        "run": run_search_term_waste,
    },
    "change-history": {
        "label": "Change History",
        "description": (
            "What changed in the account over the period (campaigns, ads, "
            "keywords, budgets...). First row is the summary; flags "
            "accounts with zero changes — possibly unmanaged."
        ),
        "uses_dates": True,
        "columns": [
            {"key": "changed_at", "label": "Changed At"},
            {"key": "resource_type", "label": "Resource"},
            {"key": "status", "label": "Status"},
        ],
        "run": run_change_history,
    },
    "mcc-links": {
        "label": "Manager (MCC) Links",
        "description": (
            "Every manager account linked to each client account. Flags "
            "ACTIVE managers outside your MCC — check any you don't "
            "recognize (hack indicator)."
        ),
        "uses_dates": False,
        "columns": [
            {"key": "manager_id", "label": "Manager ID"},
            {"key": "status", "label": "Status"},
            {"key": "verdict", "label": "Verdict"},
        ],
        "run": run_mcc_links,
    },
}


def catalog() -> List[Dict[str, Any]]:
    """Audit list for the UI (no runner functions)."""
    return [
        {
            "key": key,
            "label": a["label"],
            "description": a["description"],
            "uses_dates": a["uses_dates"],
            "columns": a["columns"],
        }
        for key, a in AUDITS.items()
    ]
