"""Per-account "brand brief": business context + targets that analysis tools use as thresholds."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from core import db
from core.config import ROOT
from core.fmt import clean_id

BRIEFS_DIR = ROOT / "briefs"

BRIEF_TEMPLATE: dict[str, Any] = {
    "business_type": None,          # "ecommerce" | "services" | "leadgen" | "local" | free text
    "description": None,
    "margin_pct": None,             # gross margin, e.g. 35
    "kpi": None,                    # "cpa" | "roas" — which target drives judgements
    "target_cpa": None,             # account currency
    "target_roas": None,            # ratio, e.g. 4.0 = 400%
    "monthly_budget": None,         # account currency
    "campaign_budgets": {},         # {"<campaign_id>": monthly amount} optional per-campaign pacing targets
    "main_products": [],
    "brand_terms": [],
    "competitor_terms": [],
    "irrelevant_terms": [],         # account-specific junk words (n-gram flagging)
    "languages": [],                # ["uk", "ru"]
    "geo": [],                      # ["UA"] / ["Kyiv"] ...
    "seasonality": None,            # free text or {"peak": [...], "low": [...]}
    "brand_impression_share_min": None,  # e.g. 0.9 — brand campaigns must keep IS above this
    "brand_campaign_match": "brand",     # substring identifying brand campaigns by name
    "rules": [],                    # free-text rules, e.g. "never bid on competitor X"
    "notes": None,
}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _yaml_brief(cid: str) -> tuple[dict[str, Any], Path | None]:
    """Optional hand-written briefs/<customer_id>.yaml or briefs/<alias>.yaml (DB values win)."""
    from core.accounts import alias_for
    for name in (cid, alias_for(cid)):
        if not name:
            continue
        for ext in (".yaml", ".yml"):
            p = BRIEFS_DIR / f"{name}{ext}"
            if p.exists():
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}, p
    return {}, None


def get_brief(customer_id: str) -> dict[str, Any]:
    cid = clean_id(customer_id)
    with db.connect() as conn:
        row = conn.execute("SELECT data, updated_at FROM brief WHERE customer_id=?", (cid,)).fetchone()
    from_file, path = _yaml_brief(cid)
    data = _deep_merge(_deep_merge(BRIEF_TEMPLATE, from_file), db.loads(row["data"]) if row else {})
    data["_updated_at"] = row["updated_at"] if row else None
    data["_exists"] = bool(row) or bool(from_file)
    if path:
        data["_yaml_file"] = str(path)
    return data


def update_brief(customer_id: str, patch: dict[str, Any], replace_lists: bool = True,
                 append_lists: tuple[str, ...] = ()) -> dict[str, Any]:
    """Merge `patch` into the stored brief. Keys in `append_lists` are appended (deduped) instead of replaced."""
    cid = clean_id(customer_id)
    current = {k: v for k, v in get_brief(cid).items() if not k.startswith("_")}
    patch = {k: v for k, v in patch.items() if not k.startswith("_")}
    for key in append_lists:
        if key in patch and isinstance(patch[key], list):
            existing = current.get(key) or []
            patch[key] = existing + [x for x in patch[key] if x not in existing]
    unknown = sorted(set(patch) - set(BRIEF_TEMPLATE))
    merged = _deep_merge(current, patch)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO brief(customer_id, data, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(customer_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (cid, db.dumps(merged), db.now_iso()),
        )
    out = get_brief(cid)
    if unknown:
        out["_custom_keys"] = unknown
    return out


def targets(customer_id: str) -> dict[str, Any]:
    """Compact view of the thresholds analysis tools need."""
    b = get_brief(customer_id)
    return {
        "target_cpa": b.get("target_cpa"),
        "target_roas": b.get("target_roas"),
        "monthly_budget": b.get("monthly_budget"),
        "kpi": b.get("kpi") or ("roas" if b.get("target_roas") and not b.get("target_cpa") else "cpa"),
        "business_type": b.get("business_type"),
        "brand_terms": [t.lower() for t in b.get("brand_terms") or []],
        "competitor_terms": [t.lower() for t in b.get("competitor_terms") or []],
        "irrelevant_terms": [t.lower() for t in b.get("irrelevant_terms") or []],
        "brand_impression_share_min": b.get("brand_impression_share_min"),
        "brand_campaign_match": (b.get("brand_campaign_match") or "brand").lower(),
        "campaign_budgets": b.get("campaign_budgets") or {},
        "brief_exists": b.get("_exists"),
    }
