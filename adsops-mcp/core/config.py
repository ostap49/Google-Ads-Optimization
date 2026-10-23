"""Runtime configuration: .env, google-ads.yaml location, config.yaml guardrails."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(env: str, default: Path) -> Path:
    raw = os.getenv(env)
    p = Path(raw).expanduser() if raw else default
    return p if p.is_absolute() else ROOT / p


DEFAULT_GUARDRAILS: dict[str, Any] = {
    # Hard ceiling for any single campaign daily budget, in account currency. None = no ceiling.
    "max_daily_budget": None,
    # Max relative change of a daily budget in one apply.
    "max_budget_change_pct": 30,
    # Max relative change of a CPC bid in one apply.
    "max_bid_change_pct": 30,
    # Max relative change of a tCPA / tROAS target in one apply.
    "max_target_change_pct": 20,
    # Proposal kinds that are refused for this account (e.g. ["update_bid_strategy"]).
    "forbidden_actions": [],
    # Campaigns that no write tool may touch.
    "protected_campaign_ids": [],
    # Hard cap on mutate operations per proposal.
    "max_operations_per_proposal": 1000,
    # Proposals older than this cannot be applied (re-propose to refresh "before" values).
    "proposal_ttl_hours": 24,
}


@dataclass
class Settings:
    google_ads_yaml: Path
    db_path: Path
    config_yaml: Path
    reports_dir: Path
    dry_run: bool
    api_version: str
    http_token: str | None
    http_host: str
    http_port: int
    http_path: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    require_elicitation: bool
    default_limit: int
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- guardrails -------------------------------------------------
    def guardrails(self, customer_id: str, alias: str | None = None) -> dict[str, Any]:
        """Effective guardrails: defaults <- config defaults <- account override (by id or alias)."""
        merged = copy.deepcopy(DEFAULT_GUARDRAILS)
        merged.update(self.raw.get("defaults") or {})
        accounts = self.raw.get("accounts") or {}
        for key in (customer_id, customer_id and f"{customer_id[:3]}-{customer_id[3:6]}-{customer_id[6:]}", alias):
            if key and str(key) in {str(k) for k in accounts}:
                override = next(v for k, v in accounts.items() if str(k) == str(key))
                merged.update(override or {})
        merged["protected_campaign_ids"] = [str(x) for x in merged.get("protected_campaign_ids") or []]
        merged["forbidden_actions"] = list(merged.get("forbidden_actions") or [])
        return merged

    def thresholds(self) -> dict[str, Any]:
        base = {
            "waste_cpa_multiplier": 2.0,
            "min_waste_spend": 10.0,
            "anomaly_z": 2.5,
            "anomaly_min_pct": 25.0,
        }
        base.update(self.raw.get("thresholds") or {})
        return base


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_yaml = _path("ADSOPS_CONFIG", ROOT / "config.yaml")
    raw: dict[str, Any] = {}
    if config_yaml.exists():
        raw = yaml.safe_load(config_yaml.read_text(encoding="utf-8")) or {}
    server = raw.get("server") or {}
    return Settings(
        google_ads_yaml=_path("GOOGLE_ADS_YAML", ROOT / "google-ads.yaml"),
        db_path=_path("ADSOPS_DB", ROOT / "data" / "adsops.db"),
        config_yaml=config_yaml,
        reports_dir=_path("ADSOPS_REPORTS_DIR", ROOT / "data" / "reports"),
        dry_run=_bool(os.getenv("ADSOPS_DRY_RUN"), bool(server.get("dry_run", False))),
        api_version=os.getenv("GOOGLE_ADS_API_VERSION", server.get("api_version", "v26")),
        http_token=os.getenv("ADSOPS_HTTP_TOKEN") or None,
        http_host=os.getenv("ADSOPS_HTTP_HOST", "127.0.0.1"),
        http_port=int(os.getenv("ADSOPS_HTTP_PORT", "8765")),
        http_path=os.getenv("ADSOPS_HTTP_PATH", "/mcp"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        require_elicitation=_bool(os.getenv("ADSOPS_REQUIRE_ELICITATION"),
                                  bool(server.get("require_elicitation", False))),
        default_limit=int(server.get("default_limit", 25)),
        raw=raw,
    )
