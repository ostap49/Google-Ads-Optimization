"""ChangeSpec contract + registry.

A spec turns validated, JSON-serializable params into Google Ads MutateOperations. `prepare` reads the
current state (the "before" values), checks guardrails and returns the normalized params that `build`
turns into operations. Because `build` only depends on stored params, the operations validated at
propose time are exactly the ones executed at apply time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gads.client import Gads, MutateResult
from safety.guard import Guard


@dataclass
class Prepared:
    params: dict[str, Any]
    diff: list[dict[str, Any]]
    impact: dict[str, Any] = field(default_factory=dict)
    rollback: dict[str, Any] | None = None       # {"kind": ..., "params": ...} when known up-front
    campaign_ids: list[str] = field(default_factory=list)
    partial_failure: bool = False
    summary: str = ""


class ChangeSpec:
    kind: str = ""
    reversible: bool = True
    internal: bool = False  # not exposed as a propose_* tool (rollback helpers)

    def prepare(self, g: Gads, cid: str, params: dict[str, Any], guard: Guard) -> Prepared:  # pragma: no cover
        raise NotImplementedError

    def build(self, g: Gads, cid: str, params: dict[str, Any]) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def current(self, g: Gads, cid: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Fresh values for drift detection: [{resource, field, value}] matching the diff's `before`."""
        return None

    def validate(self, g: Gads, cid: str, params: dict[str, Any], partial_failure: bool) -> MutateResult:
        return g.mutate(cid, self.build(g, cid, params), validate_only=True, partial_failure=partial_failure)

    def execute(self, g: Gads, cid: str, params: dict[str, Any], partial_failure: bool) -> MutateResult:
        return g.mutate(cid, self.build(g, cid, params), validate_only=False, partial_failure=partial_failure)

    def rollback_from_result(self, cid: str, params: dict[str, Any], result: MutateResult) -> dict[str, Any] | None:
        return None

    def operation_count(self, g: Gads, cid: str, params: dict[str, Any]) -> int:
        return len(self.build(g, cid, params))


REGISTRY: dict[str, ChangeSpec] = {}


def register(spec_cls: type[ChangeSpec]) -> type[ChangeSpec]:
    REGISTRY[spec_cls.kind] = spec_cls()
    return spec_cls


# ----------------------------------------------------------------------------- op helpers

def mop(g: Gads) -> Any:
    return g.type("MutateOperation")


def rn_campaign(cid: str, campaign_id: str | int) -> str:
    return f"customers/{cid}/campaigns/{int(campaign_id)}"


def rn_ad_group(cid: str, ad_group_id: str | int) -> str:
    return f"customers/{cid}/adGroups/{int(ad_group_id)}"


def split_pair(value: str) -> tuple[str, str]:
    """'adGroupId~criterionId' -> (ad_group_id, criterion_id)."""
    a, b = str(value).split("~", 1)
    return str(int(a)), str(int(b))


def enum(g: Gads, enum_name: str, value: str) -> Any:
    e = g.enum(enum_name)
    try:
        return getattr(e, str(value).upper())
    except AttributeError:
        raise ValueError(f"Invalid {enum_name} value '{value}'") from None
