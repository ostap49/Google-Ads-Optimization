"""Budgets, keyword bids, bid strategies."""
from __future__ import annotations

from typing import Any

from core.fmt import from_micros, r, to_micros
from gads.client import Gads
from safety.guard import Guard, GuardrailError
from safety.specs import ChangeSpec, Prepared, mop, register, rn_campaign, split_pair

SMART_MIN_CONV = {"TARGET_CPA": 15, "TARGET_ROAS": 30, "MAXIMIZE_CONVERSION_VALUE": 15, "MAXIMIZE_CONVERSIONS": 0}


def _campaign_basics(g: Gads, cid: str, campaign_id: str) -> dict[str, Any]:
    rows = g.search(cid, (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign.bidding_strategy_type, campaign.bidding_strategy, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared, campaign_budget.reference_count, "
        "campaign_budget.name, campaign.maximize_conversions.target_cpa_micros, "
        "campaign.maximize_conversion_value.target_roas, campaign.target_cpa.target_cpa_micros, "
        "campaign.target_roas.target_roas, campaign.target_spend.cpc_bid_ceiling_micros, "
        "campaign.manual_cpc.enhanced_cpc_enabled, campaign.target_impression_share.location, "
        "campaign.target_impression_share.location_fraction_micros, "
        "campaign.target_impression_share.cpc_bid_ceiling_micros "
        f"FROM campaign WHERE campaign.id = {int(campaign_id)}"))
    if not rows:
        raise ValueError(f"Campaign {campaign_id} not found")
    return rows[0]


def _campaign_30d(g: Gads, cid: str, campaign_id: str) -> dict[str, Any]:
    rows = g.search(cid, (
        "SELECT campaign.id, metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
        "metrics.search_budget_lost_impression_share, metrics.search_impression_share FROM campaign "
        f"WHERE segments.date DURING LAST_30_DAYS AND campaign.id = {int(campaign_id)}"))
    x = rows[0] if rows else {}
    cost = from_micros(x.get("metrics.cost_micros") or 0) or 0
    conv = x.get("metrics.conversions") or 0
    return {"cost_30d": cost, "conversions_30d": r(conv), "cpa_30d": r(cost / conv) if conv else None,
            "roas_30d": r((x.get("metrics.conversions_value") or 0) / cost) if cost else None,
            "lost_is_budget_pct": r((x.get("metrics.search_budget_lost_impression_share") or 0) * 100, 1)
            if isinstance(x.get("metrics.search_budget_lost_impression_share"), (int, float)) else None}


# ----------------------------------------------------------------------------- budget

@register
class UpdateCampaignBudget(ChangeSpec):
    kind = "update_campaign_budget"

    def prepare(self, g, cid, params, guard: Guard) -> Prepared:
        camp_id = str(int(params["campaign_id"]))
        c = _campaign_basics(g, cid, camp_id)
        before = from_micros(c["campaign_budget.amount_micros"])
        if params.get("new_daily_budget") is not None:
            after = round(float(params["new_daily_budget"]), 2)
        elif params.get("change_pct") is not None:
            after = round(before * (1 + float(params["change_pct"]) / 100), 2)
        else:
            raise ValueError("Pass new_daily_budget or change_pct.")
        if after <= 0:
            raise ValueError("Budget must be > 0.")
        guard.check_daily_budget(c["campaign.name"], after)
        guard.check_change_pct(f"Budget of '{c['campaign.name']}'", before, after, "max_budget_change_pct")
        if c.get("campaign_budget.explicitly_shared"):
            guard.warn(f"Shared budget '{c['campaign_budget.name']}' is used by {c.get('campaign_budget.reference_count')} campaigns — all of them are affected.")
        perf = _campaign_30d(g, cid, camp_id)
        if perf["lost_is_budget_pct"] is not None and perf["lost_is_budget_pct"] < 5 and after > before:
            guard.warn(f"Lost IS (budget) is only {perf['lost_is_budget_pct']}% — a higher budget may not be spent.")
        impact = {"daily_delta": r(after - before), "monthly_delta_approx": r((after - before) * 30.4), **perf}
        return Prepared(
            params={"budget_resource_name": c["campaign_budget.resource_name"], "amount_micros": to_micros(after),
                    "campaign_id": camp_id},
            diff=[{"resource": f"campaign {camp_id} ({c['campaign.name']})", "resource_name": c["campaign_budget.resource_name"],
                   "field": "daily_budget", "before": before, "after": after}],
            impact=impact,
            rollback={"kind": self.kind, "params": {"campaign_id": camp_id, "new_daily_budget": before}},
            campaign_ids=[camp_id],
            summary=f"Daily budget '{c['campaign.name']}': {before} -> {after} ({r((after / before - 1) * 100, 1) if before else '-'}%)",
        )

    def build(self, g, cid, params):
        m = mop(g)
        op = m.campaign_budget_operation
        op.update.resource_name = params["budget_resource_name"]
        op.update.amount_micros = int(params["amount_micros"])
        op.update_mask.paths.append("amount_micros")
        return [m]

    def current(self, g, cid, params):
        c = _campaign_basics(g, cid, params["campaign_id"])
        return [{"resource": f"campaign {params['campaign_id']} ({c['campaign.name']})", "field": "daily_budget",
                 "value": from_micros(c["campaign_budget.amount_micros"])}]


# ----------------------------------------------------------------------------- keyword bids

@register
class UpdateKeywordBids(ChangeSpec):
    kind = "update_keyword_bids"

    def _fetch(self, g, cid, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        ag_ids = sorted({a for a, _ in keys})
        rows = g.search(cid, (
            "SELECT campaign.id, campaign.name, campaign.bidding_strategy_type, ad_group.id, ad_group.name, "
            "ad_group.cpc_bid_micros, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros, "
            "ad_group_criterion.effective_cpc_bid_micros FROM ad_group_criterion "
            f"WHERE ad_group.id IN ({', '.join(ag_ids)}) AND ad_group_criterion.type = 'KEYWORD'"))
        return {(str(x["ad_group.id"]), str(x["ad_group_criterion.criterion_id"])): x for x in rows}

    def prepare(self, g, cid, params, guard):
        changes = params.get("changes") or []
        if not changes:
            raise ValueError("changes is empty")
        keys = []
        for ch in changes:
            if "id" in ch:
                ch["ad_group_id"], ch["criterion_id"] = split_pair(ch["id"])
            keys.append((str(int(ch["ad_group_id"])), str(int(ch["criterion_id"]))))
        cur = self._fetch(g, cid, keys)
        diff, ops, rb, camp_ids, smart = [], [], [], set(), set()
        for ch, key in zip(changes, keys):
            x = cur.get(key)
            if not x:
                raise ValueError(f"Keyword {key[0]}~{key[1]} not found")
            own = from_micros(x.get("ad_group_criterion.cpc_bid_micros")) or None
            eff = own or from_micros(x.get("ad_group_criterion.effective_cpc_bid_micros")) or from_micros(x.get("ad_group.cpc_bid_micros"))
            if ch.get("cpc_bid") is not None:
                new = round(float(ch["cpc_bid"]), 2)
            elif ch.get("change_pct") is not None:
                if not eff:
                    raise ValueError(f"Keyword {key[0]}~{key[1]} has no current bid to apply change_pct to")
                new = round(eff * (1 + float(ch["change_pct"]) / 100), 2)
            elif ch.get("clear"):
                new = None
            else:
                raise ValueError("Each change needs cpc_bid, change_pct or clear=True")
            label = f"'{x['ad_group_criterion.keyword.text']}' [{x['ad_group_criterion.keyword.match_type']}] in {x['ad_group.name']}"
            if new is not None:
                guard.check_change_pct(label, eff, new, "max_bid_change_pct")
            if x["campaign.bidding_strategy_type"] not in ("MANUAL_CPC", "ENHANCED_CPC"):
                smart.add(x["campaign.name"])
            if own == new:
                continue
            camp_ids.add(str(x["campaign.id"]))
            diff.append({"resource": label, "resource_name": f"customers/{cid}/adGroupCriteria/{key[0]}~{key[1]}",
                         "field": "cpc_bid", "before": own, "after": new, "effective_before": eff})
            ops.append({"ad_group_id": key[0], "criterion_id": key[1], "cpc_bid_micros": to_micros(new)})
            rb.append({"ad_group_id": key[0], "criterion_id": key[1], **({"cpc_bid": own} if own else {"clear": True})})
        for name in sorted(smart):
            guard.warn(f"'{name}' uses Smart Bidding — keyword CPC bids are ignored there.")
        return Prepared(params={"ops": ops}, diff=diff, rollback={"kind": self.kind, "params": {"changes": rb}} if rb else None,
                        campaign_ids=sorted(camp_ids), partial_failure=True,
                        summary=f"Change CPC bids on {len(ops)} keyword(s)")

    def build(self, g, cid, params):
        out = []
        for o in params["ops"]:
            m = mop(g)
            op = m.ad_group_criterion_operation
            op.update.resource_name = f"customers/{cid}/adGroupCriteria/{o['ad_group_id']}~{o['criterion_id']}"
            if o["cpc_bid_micros"] is not None:
                op.update.cpc_bid_micros = int(o["cpc_bid_micros"])
            op.update_mask.paths.append("cpc_bid_micros")  # unset value + mask = inherit ad group bid
            out.append(m)
        return out

    def current(self, g, cid, params):
        keys = [(o["ad_group_id"], o["criterion_id"]) for o in params["ops"]]
        cur = self._fetch(g, cid, keys)
        out = []
        for k in keys:
            x = cur.get(k)
            if x:
                label = f"'{x['ad_group_criterion.keyword.text']}' [{x['ad_group_criterion.keyword.match_type']}] in {x['ad_group.name']}"
                out.append({"resource": label, "field": "cpc_bid", "value": from_micros(x.get("ad_group_criterion.cpc_bid_micros")) or None})
        return out


# ----------------------------------------------------------------------------- bid strategy

STRATEGIES = ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "TARGET_CPA", "TARGET_ROAS",
              "MANUAL_CPC", "MAXIMIZE_CLICKS", "TARGET_IMPRESSION_SHARE")


def _strategy_state(c: dict[str, Any]) -> dict[str, Any]:
    """Normalized view of the current strategy (what rollback restores)."""
    st = c["campaign.bidding_strategy_type"]
    out: dict[str, Any] = {"strategy": st}
    if c.get("campaign.bidding_strategy"):
        out = {"strategy": "PORTFOLIO", "portfolio": c["campaign.bidding_strategy"], "portfolio_type": st}
    elif st == "MAXIMIZE_CONVERSIONS":
        out["target_cpa"] = from_micros(c.get("campaign.maximize_conversions.target_cpa_micros")) or None
    elif st == "MAXIMIZE_CONVERSION_VALUE":
        out["target_roas"] = c.get("campaign.maximize_conversion_value.target_roas") or None
    elif st == "TARGET_CPA":
        out["target_cpa"] = from_micros(c.get("campaign.target_cpa.target_cpa_micros"))
    elif st == "TARGET_ROAS":
        out["target_roas"] = c.get("campaign.target_roas.target_roas")
    elif st == "TARGET_SPEND":
        out = {"strategy": "MAXIMIZE_CLICKS", "cpc_bid_ceiling": from_micros(c.get("campaign.target_spend.cpc_bid_ceiling_micros")) or None}
    elif st == "TARGET_IMPRESSION_SHARE":
        out.update({"location": c.get("campaign.target_impression_share.location"),
                    "fraction": (c.get("campaign.target_impression_share.location_fraction_micros") or 0) / 1e6,
                    "cpc_bid_ceiling": from_micros(c.get("campaign.target_impression_share.cpc_bid_ceiling_micros"))})
    return {k: v for k, v in out.items() if v is not None}


@register
class UpdateBidStrategy(ChangeSpec):
    kind = "update_bid_strategy"

    def prepare(self, g, cid, params, guard):
        camp_id = str(int(params["campaign_id"]))
        c = _campaign_basics(g, cid, camp_id)
        before = _strategy_state(c)
        want = {k: v for k, v in params.items() if k in ("strategy", "target_cpa", "target_roas", "cpc_bid_ceiling",
                                                          "location", "fraction", "portfolio") and v is not None}
        st = str(want.get("strategy", "")).upper()
        if st not in STRATEGIES + ("PORTFOLIO",):
            raise ValueError(f"strategy must be one of {STRATEGIES}")
        # modern representation: tCPA / tROAS live inside Maximize Conversions / Conversion Value
        if st == "TARGET_CPA":
            st = "MAXIMIZE_CONVERSIONS"
            if not want.get("target_cpa"):
                raise ValueError("TARGET_CPA needs target_cpa")
        if st == "TARGET_ROAS":
            st = "MAXIMIZE_CONVERSION_VALUE"
            if not want.get("target_roas"):
                raise ValueError("TARGET_ROAS needs target_roas")
        want["strategy"] = st
        if st == "TARGET_IMPRESSION_SHARE" and not (want.get("location") and want.get("fraction")):
            raise ValueError("TARGET_IMPRESSION_SHARE needs location (ANYWHERE_ON_PAGE|TOP_OF_PAGE|ABSOLUTE_TOP_OF_PAGE) and fraction (0-1)")
        same_family = before.get("strategy") in (st, {"MAXIMIZE_CONVERSIONS": "TARGET_CPA", "MAXIMIZE_CONVERSION_VALUE": "TARGET_ROAS"}.get(st))
        if same_family:
            for key in ("target_cpa", "target_roas"):
                if before.get(key) and want.get(key):
                    guard.check_change_pct(f"{key} of '{c['campaign.name']}'", before[key], want[key], "max_target_change_pct")
        perf = _campaign_30d(g, cid, camp_id)
        need = SMART_MIN_CONV.get("TARGET_CPA" if want.get("target_cpa") else "TARGET_ROAS" if want.get("target_roas") else st, 0)
        if need and (perf["conversions_30d"] or 0) < need:
            guard.warn(f"Only {perf['conversions_30d']} conversions in 30d — {st} with a target usually needs ~{need}+.")
        if not same_family:
            guard.warn("Strategy switch restarts the learning period (~1-2 weeks of volatility).")
        if before.get("strategy") == "PORTFOLIO":
            guard.warn("Campaign leaves its portfolio bid strategy.")
        if want == before:
            return Prepared(params={}, diff=[])
        diff = [{"resource": f"campaign {camp_id} ({c['campaign.name']})", "resource_name": rn_campaign(cid, camp_id),
                 "field": "bid_strategy", "before": before, "after": want}]
        return Prepared(params={"campaign_id": camp_id, **want}, diff=diff, impact=perf,
                        rollback={"kind": self.kind, "params": {"campaign_id": camp_id, **before}},
                        campaign_ids=[camp_id], summary=f"Bid strategy '{c['campaign.name']}': {before} -> {want}")

    def build(self, g, cid, params):
        m = mop(g)
        op = m.campaign_operation
        camp = op.update
        camp.resource_name = rn_campaign(cid, params["campaign_id"])
        paths = op.update_mask.paths
        st = params["strategy"]
        if st == "PORTFOLIO":
            camp.bidding_strategy = params["portfolio"]
            paths.append("bidding_strategy")
        elif st == "MAXIMIZE_CONVERSIONS":
            camp.maximize_conversions.target_cpa_micros = to_micros(params.get("target_cpa")) or 0
            paths.append("maximize_conversions.target_cpa_micros")
        elif st == "MAXIMIZE_CONVERSION_VALUE":
            camp.maximize_conversion_value.target_roas = float(params.get("target_roas") or 0)
            paths.append("maximize_conversion_value.target_roas")
        elif st == "MANUAL_CPC":
            camp.manual_cpc.enhanced_cpc_enabled = False
            paths.append("manual_cpc.enhanced_cpc_enabled")
        elif st == "MAXIMIZE_CLICKS":
            camp.target_spend.cpc_bid_ceiling_micros = to_micros(params.get("cpc_bid_ceiling")) or 0
            paths.append("target_spend.cpc_bid_ceiling_micros")
        elif st == "TARGET_IMPRESSION_SHARE":
            tis = camp.target_impression_share
            tis.location = getattr(g.enum("TargetImpressionShareLocationEnum"), str(params["location"]).upper())
            tis.location_fraction_micros = int(float(params["fraction"]) * 1_000_000)
            paths.extend(["target_impression_share.location", "target_impression_share.location_fraction_micros"])
            if params.get("cpc_bid_ceiling"):
                tis.cpc_bid_ceiling_micros = to_micros(params["cpc_bid_ceiling"])
                paths.append("target_impression_share.cpc_bid_ceiling_micros")
        else:
            raise GuardrailError(f"Unsupported strategy {st}")
        return [m]

    def current(self, g, cid, params):
        c = _campaign_basics(g, cid, params["campaign_id"])
        return [{"resource": f"campaign {params['campaign_id']} ({c['campaign.name']})", "field": "bid_strategy",
                 "value": _strategy_state(c)}]
