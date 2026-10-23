"""Pause / enable entities, and the internal remove-what-we-created rollback helper.

There is deliberately no REMOVE for campaigns, ad groups or ads anywhere in adsops.
"""
from __future__ import annotations

from typing import Any

from core.fmt import from_micros
from safety.guard import GuardrailError
from safety.specs import ChangeSpec, Prepared, enum, mop, register, split_pair

ENTITIES = {
    "campaign": {"op": "campaign_operation", "enum": "CampaignStatusEnum"},
    "ad_group": {"op": "ad_group_operation", "enum": "AdGroupStatusEnum"},
    "ad": {"op": "ad_group_ad_operation", "enum": "AdGroupAdStatusEnum"},
    "keyword": {"op": "ad_group_criterion_operation", "enum": "AdGroupCriterionStatusEnum"},
    "asset_group": {"op": "asset_group_operation", "enum": "AssetGroupStatusEnum"},
}


def _fetch(g, cid: str, entity: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """id -> {resource_name, label, status, campaign_id, daily_budget?}"""
    out: dict[str, dict[str, Any]] = {}
    if entity == "campaign":
        rows = g.search(cid, "SELECT campaign.id, campaign.name, campaign.status, campaign_budget.amount_micros "
                             f"FROM campaign WHERE campaign.id IN ({', '.join(str(int(i)) for i in ids)})")
        for x in rows:
            out[str(x["campaign.id"])] = {"resource_name": f"customers/{cid}/campaigns/{x['campaign.id']}",
                                          "label": f"campaign '{x['campaign.name']}'", "status": x["campaign.status"],
                                          "campaign_id": str(x["campaign.id"]),
                                          "daily_budget": from_micros(x.get("campaign_budget.amount_micros"))}
    elif entity == "ad_group":
        rows = g.search(cid, "SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id, campaign.name FROM ad_group "
                             f"WHERE ad_group.id IN ({', '.join(str(int(i)) for i in ids)})")
        for x in rows:
            out[str(x["ad_group.id"])] = {"resource_name": f"customers/{cid}/adGroups/{x['ad_group.id']}",
                                          "label": f"ad group '{x['campaign.name']} / {x['ad_group.name']}'",
                                          "status": x["ad_group.status"], "campaign_id": str(x["campaign.id"])}
    elif entity in ("ad", "keyword"):
        pairs = [split_pair(i) for i in ids]
        ag_ids = ", ".join(sorted({a for a, _ in pairs}))
        if entity == "ad":
            rows = g.search(cid, "SELECT ad_group.id, ad_group.name, campaign.id, ad_group_ad.ad.id, ad_group_ad.status, "
                                 f"ad_group_ad.ad.type FROM ad_group_ad WHERE ad_group.id IN ({ag_ids})")
            for x in rows:
                key = f"{x['ad_group.id']}~{x['ad_group_ad.ad.id']}"
                out[key] = {"resource_name": f"customers/{cid}/adGroupAds/{key}",
                            "label": f"ad {x['ad_group_ad.ad.id']} ({x.get('ad_group_ad.ad.type')}) in '{x['ad_group.name']}'",
                            "status": x["ad_group_ad.status"], "campaign_id": str(x["campaign.id"])}
        else:
            rows = g.search(cid, "SELECT ad_group.id, ad_group.name, campaign.id, ad_group_criterion.criterion_id, "
                                 "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status "
                                 f"FROM ad_group_criterion WHERE ad_group.id IN ({ag_ids}) AND ad_group_criterion.type = 'KEYWORD'")
            for x in rows:
                key = f"{x['ad_group.id']}~{x['ad_group_criterion.criterion_id']}"
                out[key] = {"resource_name": f"customers/{cid}/adGroupCriteria/{key}",
                            "label": f"keyword '{x['ad_group_criterion.keyword.text']}' [{x['ad_group_criterion.keyword.match_type']}] in '{x['ad_group.name']}'",
                            "status": x["ad_group_criterion.status"], "campaign_id": str(x["campaign.id"])}
        out = {k: v for k, v in out.items() if k in {f"{a}~{b}" for a, b in pairs}}
    elif entity == "asset_group":
        rows = g.search(cid, "SELECT asset_group.id, asset_group.name, asset_group.status, campaign.id, campaign.name "
                             f"FROM asset_group WHERE asset_group.id IN ({', '.join(str(int(i)) for i in ids)})")
        for x in rows:
            out[str(x["asset_group.id"])] = {"resource_name": f"customers/{cid}/assetGroups/{x['asset_group.id']}",
                                             "label": f"asset group '{x['campaign.name']} / {x['asset_group.name']}'",
                                             "status": x["asset_group.status"], "campaign_id": str(x["campaign.id"])}
    return out


@register
class SetStatus(ChangeSpec):
    kind = "set_status"

    def prepare(self, g, cid, params, guard):
        entity = params.get("entity_type")
        if entity not in ENTITIES:
            raise ValueError(f"entity_type must be one of {sorted(ENTITIES)}")
        status = str(params.get("status", "")).upper()
        if status not in ("ENABLED", "PAUSED"):
            raise ValueError("status must be ENABLED or PAUSED (removing is not supported by adsops).")
        ids = [str(i) for i in params.get("ids") or []]
        if not ids:
            raise ValueError("ids is empty")
        cur = _fetch(g, cid, entity, ids)
        missing = [i for i in ids if i not in cur]
        if missing:
            raise ValueError(f"{entity} not found: {missing[:10]}")
        changes, diff, rb = [], [], {}
        for i in ids:
            x = cur[i]
            if x["status"] == "REMOVED":
                raise ValueError(f"{x['label']} is REMOVED and cannot be re-enabled")
            if x["status"] == status:
                continue
            changes.append({"resource_name": x["resource_name"]})
            rb.setdefault(x["status"], []).append(i)
            diff.append({"resource": x["label"], "resource_name": x["resource_name"], "field": "status",
                         "before": x["status"], "after": status})
        if entity == "campaign" and status == "ENABLED":
            total = sum(cur[i].get("daily_budget") or 0 for i in ids if cur[i]["status"] != status)
            guard.warn(f"Enabling starts spending immediately: up to {total:.2f}/day in total budgets.")
        rollback = None
        if rb:
            # restore each previous status (only ENABLED/PAUSED are restorable)
            back = [{"entity_type": entity, "ids": v, "status": k} for k, v in rb.items() if k in ("ENABLED", "PAUSED")]
            rollback = {"kind": "set_status_multi", "params": {"groups": back}} if len(back) > 1 else (
                {"kind": self.kind, "params": back[0]} if back else None)
        return Prepared(params={"entity_type": entity, "status": status, "changes": changes}, diff=diff,
                        rollback=rollback, campaign_ids=sorted({cur[i]["campaign_id"] for i in ids}),
                        partial_failure=True, summary=f"Set {len(changes)} {entity}(s) to {status}")

    def build(self, g, cid, params):
        meta = ENTITIES[params["entity_type"]]
        ops = []
        for ch in params["changes"]:
            m = mop(g)
            op = getattr(m, meta["op"])
            op.update.resource_name = ch["resource_name"]
            op.update.status = enum(g, meta["enum"], params["status"])
            op.update_mask.paths.append("status")
            ops.append(m)
        return ops

    def current(self, g, cid, params):
        ids = [ch["resource_name"].split("/")[-1] for ch in params["changes"]]
        cur = _fetch(g, cid, params["entity_type"], ids)
        return [{"resource": v["label"], "field": "status", "value": v["status"]} for v in cur.values()]


@register
class SetStatusMulti(ChangeSpec):
    """Rollback helper: restore mixed previous statuses."""
    kind = "set_status_multi"
    internal = True

    def prepare(self, g, cid, params, guard):
        parts = [SetStatus().prepare(g, cid, grp, guard) for grp in params["groups"]]
        return Prepared(params={"parts": [p.params for p in parts]}, diff=[d for p in parts for d in p.diff],
                        campaign_ids=sorted({c for p in parts for c in p.campaign_ids}), partial_failure=True,
                        summary="Restore previous statuses")

    def build(self, g, cid, params):
        return [op for p in params["parts"] for op in SetStatus().build(g, cid, p)]


REMOVABLE = {
    "campaignCriteria": "campaign_criterion_operation",
    "adGroupCriteria": "ad_group_criterion_operation",
    "sharedCriteria": "shared_criterion_operation",
    "campaignSharedSets": "campaign_shared_set_operation",
    "campaignAssets": "campaign_asset_operation",
    "adGroupAssets": "ad_group_asset_operation",
    "customerAssets": "customer_asset_operation",
    "customerNegativeCriteria": "customer_negative_criterion_operation",
    "assetGroupSignals": "asset_group_signal_operation",
}


@register
class RemoveCreated(ChangeSpec):
    """Rollback helper: remove criteria / links that an adsops proposal created. Never campaigns, ad groups or ads."""
    kind = "remove_created"
    internal = True

    def prepare(self, g, cid, params, guard):
        names = list(dict.fromkeys(params.get("resource_names") or []))
        for rn in names:
            parts = rn.split("/")
            if len(parts) < 4 or parts[1] != cid or parts[2] not in REMOVABLE:
                raise GuardrailError(f"remove_created refuses '{rn}': only criteria / asset links created by adsops can be removed.")
        diff = [{"resource": rn, "resource_name": rn, "field": "exists", "before": True, "after": False} for rn in names]
        camp_ids = set()
        for rn in names:
            tail = rn.split("/")[-1]
            if rn.split("/")[2] in ("campaignCriteria", "campaignSharedSets", "campaignAssets"):
                camp_ids.add(tail.split("~")[0])
        return Prepared(params={"resource_names": names}, diff=diff, campaign_ids=sorted(camp_ids), partial_failure=True,
                        summary=f"Remove {len(names)} item(s) created by an earlier proposal")

    def build(self, g, cid, params):
        ops = []
        for rn in params["resource_names"]:
            m = mop(g)
            getattr(m, REMOVABLE[rn.split("/")[2]]).remove = rn
            ops.append(m)
        return ops
