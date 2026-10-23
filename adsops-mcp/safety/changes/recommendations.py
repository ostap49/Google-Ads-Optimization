"""Apply a Google recommendation by explicit ID (never automatically)."""
from __future__ import annotations

from gads.client import MutateResult
from safety.specs import ChangeSpec, Prepared, register


@register
class ApplyRecommendation(ChangeSpec):
    kind = "apply_recommendation"
    reversible = False

    def _rows(self, g, cid, names):
        inner = ", ".join(f"'{n}'" for n in names)
        return g.search(cid, "SELECT recommendation.resource_name, recommendation.type, recommendation.campaign, "
                             f"recommendation.dismissed FROM recommendation WHERE recommendation.resource_name IN ({inner})")

    def prepare(self, g, cid, params, guard):
        ids = [str(i) for i in params.get("recommendation_ids") or []]
        if not ids:
            raise ValueError("recommendation_ids required (from get_recommendations)")
        names = [i if i.startswith("customers/") else f"customers/{cid}/recommendations/{i}" for i in ids]
        rows = {x["recommendation.resource_name"]: x for x in self._rows(g, cid, names)}
        missing = [n for n in names if n not in rows]
        if missing:
            raise ValueError(f"Recommendations not found / expired: {missing}")
        camp_ids = [x["recommendation.campaign"].split("/")[-1] for x in rows.values() if x.get("recommendation.campaign")]
        guard.warn("Applying a recommendation cannot be rolled back automatically — note the current state first.")
        diff = [{"resource": x.get("recommendation.campaign") or "account", "resource_name": n, "field": "apply_recommendation",
                 "before": None, "after": x["recommendation.type"]} for n, x in rows.items()]
        return Prepared(params={"resource_names": names}, diff=diff, campaign_ids=camp_ids, partial_failure=True,
                        summary=f"Apply {len(names)} Google recommendation(s): {', '.join(sorted({x['recommendation.type'] for x in rows.values()}))}")

    def build(self, g, cid, params):
        return list(params["resource_names"])  # not MutateOperations; counted for guardrails only

    def validate(self, g, cid, params, partial_failure):
        # ApplyRecommendation has no validate_only: confirm they still exist and are not dismissed.
        rows = {x["recommendation.resource_name"]: x for x in self._rows(g, cid, params["resource_names"])}
        fails = [{"operation_index": i, "code": "not_found_or_dismissed", "message": n}
                 for i, n in enumerate(params["resource_names"]) if n not in rows or rows[n].get("recommendation.dismissed")]
        return MutateResult(partial_failures=fails, validate_only=True)

    def execute(self, g, cid, params, partial_failure):
        return g.apply_recommendation(cid, params["resource_names"], partial_failure=True)
