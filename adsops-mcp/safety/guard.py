"""Guardrails from config.yaml, evaluated at propose time and again at apply time."""
from __future__ import annotations

from typing import Any

from core import accounts
from core.config import get_settings


class GuardrailError(PermissionError):
    """Raised when a proposal violates a guardrail. Message is shown to the model/user."""


class Guard:
    def __init__(self, customer_id: str, is_rollback: bool = False):
        self.customer_id = customer_id
        self.rules: dict[str, Any] = get_settings().guardrails(customer_id, accounts.alias_for(customer_id))
        self.is_rollback = is_rollback
        self.warnings: list[str] = []

    def check_kind(self, kind: str) -> None:
        if kind in self.rules["forbidden_actions"]:
            raise GuardrailError(f"'{kind}' is a forbidden action for this account (config.yaml forbidden_actions).")

    def check_campaigns(self, campaign_ids: list[str] | set[str]) -> None:
        protected = set(self.rules["protected_campaign_ids"])
        hit = sorted({str(c) for c in campaign_ids} & protected)
        if hit:
            raise GuardrailError(f"Campaign(s) {hit} are protected (config.yaml protected_campaign_ids); no write tool may touch them.")

    def check_ops(self, n: int) -> None:
        if n > int(self.rules["max_operations_per_proposal"]):
            raise GuardrailError(f"{n} operations exceed max_operations_per_proposal={self.rules['max_operations_per_proposal']}. Split the change.")

    def check_change_pct(self, label: str, before: float | None, after: float | None, rule: str) -> None:
        """Relative change limit; rollbacks restoring previous values are exempt."""
        if self.is_rollback or before in (None, 0) or after is None:
            return
        limit = float(self.rules[rule])
        pct = abs(float(after) - float(before)) / abs(float(before)) * 100
        if pct > limit + 1e-9:
            raise GuardrailError(f"{label}: change {before} -> {after} is {pct:.1f}%, above {rule}={limit:g}%. "
                                 f"Propose a smaller step (max {limit:g}% per apply) or change config.yaml.")

    def check_daily_budget(self, label: str, amount: float) -> None:
        cap = self.rules.get("max_daily_budget")
        if cap is not None and float(amount) > float(cap):
            raise GuardrailError(f"{label}: daily budget {amount} exceeds max_daily_budget={cap} for this account.")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
