"""Negative keywords (campaign / ad group / shared list / account level) and positive keywords."""
from __future__ import annotations

import re
from typing import Any

from core.fmt import from_micros, to_micros
from memory.brief import targets
from safety.specs import ChangeSpec, Prepared, enum, mop, register, rn_ad_group, rn_campaign
from tools.text import negative_matches

BAD_CHARS = re.compile(r"[!@%^*=;~<>?\\|{},]")
MATCH_TYPES = ("EXACT", "PHRASE", "BROAD")


def clean_keyword(text: str, match_type: str | None) -> tuple[str, str]:
    """Accept '[exact]', '"phrase"' or plain text; normalize whitespace; validate Google keyword rules."""
    t = " ".join(str(text).strip().split())
    mt = (match_type or "").upper() or None
    if t.startswith("[") and t.endswith("]"):
        t, mt = t[1:-1].strip(), mt or "EXACT"
    elif t.startswith('"') and t.endswith('"'):
        t, mt = t[1:-1].strip(), mt or "PHRASE"
    mt = mt or "PHRASE"
    if mt not in MATCH_TYPES:
        raise ValueError(f"match_type must be one of {MATCH_TYPES}")
    if not t:
        raise ValueError("Empty keyword")
    if len(t) > 80 or len(t.split()) > 10:
        raise ValueError(f"Keyword too long (max 80 chars / 10 words): '{t}'")
    if BAD_CHARS.search(t):
        raise ValueError(f"Keyword contains characters Google rejects: '{t}'")
    return t.lower(), mt


def _kw_items(raw: list[Any], default_mt: str | None) -> list[dict[str, Any]]:
    out, seen = [], set()
    for k in raw:
        if isinstance(k, str):
            text, mt = clean_keyword(k, None if k.strip()[:1] in "[\"" else default_mt)
            item = {"text": text, "match_type": mt}
        else:
            text, mt = clean_keyword(k["text"], k.get("match_type") or default_mt)
            item = {**{kk: vv for kk, vv in k.items() if kk not in ("text", "match_type")}, "text": text, "match_type": mt}
        key = (item["text"], item["match_type"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ----------------------------------------------------------------------------- negatives

@register
class AddNegativeKeywords(ChangeSpec):
    kind = "add_negative_keywords"

    def prepare(self, g, cid, params, guard):
        level = params.get("level", "campaign")
        kws = _kw_items(params.get("keywords") or [], params.get("match_type") or "EXACT")
        if not kws:
            raise ValueError("keywords is empty")
        t = targets(cid)
        for k in kws:
            if any(b and b in k["text"] for b in t["brand_terms"]):
                guard.warn(f"Negative '{k['text']}' contains a brand term — it may block brand traffic.")
        diff, targets_out, camp_ids = [], [], set()
        out_params: dict[str, Any] = {"level": level}

        def existing_for(rows, text_f, mt_f):
            return {((x.get(text_f) or "").lower(), x.get(mt_f)) for x in rows}

        if level == "campaign":
            ids = [str(int(c)) for c in params.get("campaign_ids") or ([params["campaign_id"]] if params.get("campaign_id") else [])]
            if not ids:
                raise ValueError("campaign level needs campaign_ids")
            names = {str(x["campaign.id"]): x["campaign.name"] for x in g.search(cid,
                     f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type FROM campaign WHERE campaign.id IN ({', '.join(ids)})")}
            ex = g.search(cid, "SELECT campaign.id, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type, "
                               "campaign_criterion.negative, campaign_criterion.type FROM campaign_criterion WHERE "
                               f"campaign.id IN ({', '.join(ids)}) AND campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'")
            for camp in ids:
                if camp not in names:
                    raise ValueError(f"Campaign {camp} not found")
                have = existing_for([x for x in ex if str(x["campaign.id"]) == camp],
                                    "campaign_criterion.keyword.text", "campaign_criterion.keyword.match_type")
                add = [k for k in kws if (k["text"], k["match_type"]) not in have]
                camp_ids.add(camp)
                targets_out.append({"campaign_id": camp, "keywords": add})
                diff += [{"resource": f"campaign {camp} ({names[camp]})", "field": "negative_keyword", "before": None,
                          "after": f"{k['text']} [{k['match_type']}]"} for k in add]
            out_params["targets"] = targets_out
        elif level == "ad_group":
            ids = [str(int(a)) for a in params.get("ad_group_ids") or []]
            if not ids:
                raise ValueError("ad_group level needs ad_group_ids")
            ags = {str(x["ad_group.id"]): x for x in g.search(cid,
                   f"SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group WHERE ad_group.id IN ({', '.join(ids)})")}
            ex = g.search(cid, "SELECT ad_group.id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                               "ad_group_criterion.negative, ad_group_criterion.type FROM ad_group_criterion WHERE "
                               f"ad_group.id IN ({', '.join(ids)}) AND ad_group_criterion.negative = TRUE AND ad_group_criterion.type = 'KEYWORD'")
            for ag in ids:
                if ag not in ags:
                    raise ValueError(f"Ad group {ag} not found")
                have = existing_for([x for x in ex if str(x["ad_group.id"]) == ag],
                                    "ad_group_criterion.keyword.text", "ad_group_criterion.keyword.match_type")
                add = [k for k in kws if (k["text"], k["match_type"]) not in have]
                camp_ids.add(str(ags[ag]["campaign.id"]))
                targets_out.append({"ad_group_id": ag, "keywords": add})
                diff += [{"resource": f"ad group {ag} ({ags[ag]['ad_group.name']})", "field": "negative_keyword",
                          "before": None, "after": f"{k['text']} [{k['match_type']}]"} for k in add]
            out_params["targets"] = targets_out
        elif level in ("shared_list", "account"):
            set_type = "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS" if level == "account" else "NEGATIVE_KEYWORDS"
            sets = g.search(cid, "SELECT shared_set.id, shared_set.name, shared_set.type, shared_set.status FROM shared_set "
                                 f"WHERE shared_set.type = '{set_type}' AND shared_set.status = 'ENABLED'")
            set_id = params.get("shared_set_id")
            if level == "account" and not set_id and sets:
                set_id = sets[0]["shared_set.id"]
            if set_id:
                match = [s for s in sets if str(s["shared_set.id"]) == str(set_id)]
                if not match:
                    raise ValueError(f"Shared set {set_id} ({set_type}) not found")
                list_name = match[0]["shared_set.name"]
                ex = g.search(cid, "SELECT shared_set.id, shared_criterion.keyword.text, shared_criterion.keyword.match_type, "
                                   f"shared_criterion.type FROM shared_criterion WHERE shared_set.id = {int(set_id)} AND shared_criterion.type = 'KEYWORD'")
                have = existing_for(ex, "shared_criterion.keyword.text", "shared_criterion.keyword.match_type")
                out_params["shared_set_id"] = str(set_id)
            else:
                list_name = params.get("new_list_name") or ("Account negatives (adsops)" if level == "account" else None)
                if not list_name:
                    raise ValueError("shared_list level needs shared_set_id or new_list_name")
                have = set()
                out_params["new_list_name"] = list_name
            if level == "account" and not set_id:
                guard.warn("No account-level negative list exists — it will be created and attached (3-step SharedSet flow).")
            if level == "account":
                cnc = g.search(cid, "SELECT customer_negative_criterion.id, customer_negative_criterion.type "
                                    "FROM customer_negative_criterion WHERE customer_negative_criterion.type = 'NEGATIVE_KEYWORD_LIST'")
                out_params["needs_customer_link"] = not cnc or not set_id
            attach = [str(int(c)) for c in params.get("attach_campaign_ids") or []]
            if attach:
                if level == "account":
                    raise ValueError("Account-level lists apply to all campaigns; attach_campaign_ids is for shared_list.")
                camp_ids.update(attach)
                out_params["attach_campaign_ids"] = attach
                diff += [{"resource": f"campaign {c}", "field": "shared_negative_list", "before": None, "after": list_name} for c in attach]
            add = [k for k in kws if (k["text"], k["match_type"]) not in have]
            out_params["keywords"] = add
            out_params["set_type"] = set_type
            diff += [{"resource": f"{'account' if level == 'account' else 'shared list'} '{list_name}'", "field": "negative_keyword",
                      "before": None, "after": f"{k['text']} [{k['match_type']}]"} for k in add]
            if level == "shared_list" and set_id:
                camp_ids.update(str(x["campaign.id"]) for x in g.search(cid,
                                "SELECT campaign.id, shared_set.id, campaign_shared_set.status FROM campaign_shared_set "
                                f"WHERE shared_set.id = {int(set_id)} AND campaign_shared_set.status = 'ENABLED'"))
        else:
            raise ValueError("level must be campaign | ad_group | shared_list | account")

        # blocked-traffic estimate: last-30d search terms these negatives would have blocked
        impact = self._impact(g, cid, kws, level, out_params)
        creates_set = "new_list_name" in out_params or out_params.get("needs_customer_link")
        return Prepared(params=out_params, diff=diff, impact=impact, campaign_ids=sorted(camp_ids),
                        partial_failure=not creates_set,
                        summary=f"Add {len({d['after'] for d in diff if d['field'] == 'negative_keyword'})} negative keyword(s) at {level} level")

    def _impact(self, g, cid, kws, level, params) -> dict[str, Any]:
        try:
            rows = g.search(cid, "SELECT search_term_view.search_term, campaign.id, metrics.cost_micros, metrics.clicks, "
                                 "metrics.conversions FROM search_term_view WHERE segments.date DURING LAST_30_DAYS")
        except Exception:  # noqa: BLE001 - impact is best-effort
            return {}
        scope = None
        if level == "campaign":
            scope = {t["campaign_id"] for t in params.get("targets", [])}
        cost = clicks = conv = 0.0
        terms = 0
        for x in rows:
            if scope is not None and str(x["campaign.id"]) not in scope:
                continue
            if any(negative_matches(x["search_term_view.search_term"], k["text"], k["match_type"]) for k in kws):
                terms += 1
                cost += from_micros(x.get("metrics.cost_micros") or 0) or 0
                clicks += x.get("metrics.clicks") or 0
                conv += x.get("metrics.conversions") or 0
        return {"blocked_search_terms_30d": terms, "blocked_cost_30d": round(cost, 2), "blocked_clicks_30d": int(clicks),
                "blocked_conversions_30d": round(conv, 2),
                "note": "Past 30d search terms in scope these negatives would have blocked (Search terms only)."}

    def build(self, g, cid, params):
        ops = []
        mt = lambda v: enum(g, "KeywordMatchTypeEnum", v)  # noqa: E731
        level = params["level"]
        if level == "campaign":
            for t in params["targets"]:
                for k in t["keywords"]:
                    m = mop(g)
                    c = m.campaign_criterion_operation.create
                    c.campaign = rn_campaign(cid, t["campaign_id"])
                    c.negative = True
                    c.keyword.text = k["text"]
                    c.keyword.match_type = mt(k["match_type"])
                    ops.append(m)
        elif level == "ad_group":
            for t in params["targets"]:
                for k in t["keywords"]:
                    m = mop(g)
                    c = m.ad_group_criterion_operation.create
                    c.ad_group = rn_ad_group(cid, t["ad_group_id"])
                    c.negative = True
                    c.keyword.text = k["text"]
                    c.keyword.match_type = mt(k["match_type"])
                    ops.append(m)
        else:
            if params.get("shared_set_id"):
                set_rn = f"customers/{cid}/sharedSets/{params['shared_set_id']}"
            else:
                set_rn = f"customers/{cid}/sharedSets/-1"
                m = mop(g)
                s = m.shared_set_operation.create
                s.resource_name = set_rn
                s.name = params["new_list_name"]
                s.type_ = enum(g, "SharedSetTypeEnum", params["set_type"])
                ops.append(m)
            for k in params["keywords"]:
                m = mop(g)
                c = m.shared_criterion_operation.create
                c.shared_set = set_rn
                c.keyword.text = k["text"]
                c.keyword.match_type = mt(k["match_type"])
                ops.append(m)
            if params.get("needs_customer_link"):
                m = mop(g)
                c = m.customer_negative_criterion_operation.create
                c.negative_keyword_list.shared_set = set_rn
                ops.append(m)
            for camp in params.get("attach_campaign_ids") or []:
                m = mop(g)
                link = m.campaign_shared_set_operation.create
                link.campaign = rn_campaign(cid, camp)
                link.shared_set = set_rn
                ops.append(m)
        return ops

    def rollback_from_result(self, cid, params, result):
        removable = [rn for rn in result.resource_names if rn and ("/campaignCriteria/" in rn or "/adGroupCriteria/" in rn
                     or "/sharedCriteria/" in rn or "/campaignSharedSets/" in rn)]
        return {"kind": "remove_created", "params": {"resource_names": removable}} if removable else None


# ----------------------------------------------------------------------------- positive keywords

@register
class AddKeywords(ChangeSpec):
    kind = "add_keywords"

    def prepare(self, g, cid, params, guard):
        ag_id = str(int(params["ad_group_id"]))
        kws = _kw_items(params.get("keywords") or [], params.get("match_type") or "PHRASE")
        ag = g.search(cid, "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, campaign.id, "
                           f"campaign.name, campaign.advertising_channel_type FROM ad_group WHERE ad_group.id = {ag_id}")
        if not ag:
            raise ValueError(f"Ad group {ag_id} not found")
        ag = ag[0]
        if ag["campaign.advertising_channel_type"] != "SEARCH":
            raise ValueError("Keywords can only be added to Search ad groups.")
        existing = g.search(cid, "SELECT ad_group.id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
                                 "ad_group_criterion.status, ad_group_criterion.negative, ad_group_criterion.type FROM ad_group_criterion "
                                 f"WHERE ad_group.id = {ag_id} AND ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED'")
        have = {((x.get("ad_group_criterion.keyword.text") or "").lower(), x.get("ad_group_criterion.keyword.match_type"))
                for x in existing if not x.get("ad_group_criterion.negative")}
        negs = [(x.get("ad_group_criterion.keyword.text") or "", x.get("ad_group_criterion.keyword.match_type"))
                for x in existing if x.get("ad_group_criterion.negative")]
        t = targets(cid)
        default_bid = from_micros(ag.get("ad_group.cpc_bid_micros"))
        status = str(params.get("status") or "ENABLED").upper()
        add, diff = [], []
        for k in kws:
            if (k["text"], k["match_type"]) in have:
                continue
            if any(c in k["text"] for c in t["competitor_terms"]):
                guard.warn(f"'{k['text']}' contains a competitor term — check the brief rules.")
            if any(negative_matches(k["text"], n, mt) for n, mt in negs):
                guard.warn(f"'{k['text']}' is blocked by an ad-group negative.")
            bid = k.get("cpc_bid")
            if bid and default_bid and float(bid) > 3 * default_bid:
                guard.warn(f"'{k['text']}' bid {bid} is >3x the ad group default {default_bid}.")
            add.append({"text": k["text"], "match_type": k["match_type"], "cpc_bid_micros": to_micros(bid) if bid else None,
                        "final_url": k.get("final_url")})
            diff.append({"resource": f"ad group {ag_id} ({ag['ad_group.name']})", "field": "keyword", "before": None,
                         "after": f"{k['text']} [{k['match_type']}]" + (f" @ {bid}" if bid else "")})
        if status == "ENABLED" and ag["ad_group.status"] == "ENABLED":
            guard.warn("Keywords will start serving immediately (ad group is enabled).")
        return Prepared(params={"ad_group_id": ag_id, "status": status, "keywords": add}, diff=diff,
                        campaign_ids=[str(ag["campaign.id"])], partial_failure=True,
                        summary=f"Add {len(add)} keyword(s) to '{ag['campaign.name']} / {ag['ad_group.name']}'")

    def build(self, g, cid, params):
        ops = []
        for k in params["keywords"]:
            m = mop(g)
            c = m.ad_group_criterion_operation.create
            c.ad_group = rn_ad_group(cid, params["ad_group_id"])
            c.status = enum(g, "AdGroupCriterionStatusEnum", params["status"])
            c.keyword.text = k["text"]
            c.keyword.match_type = enum(g, "KeywordMatchTypeEnum", k["match_type"])
            if k.get("cpc_bid_micros"):
                c.cpc_bid_micros = int(k["cpc_bid_micros"])
            if k.get("final_url"):
                c.final_urls.append(k["final_url"])
            ops.append(m)
        return ops

    def rollback_from_result(self, cid, params, result):
        names = [rn for rn in result.resource_names if rn]
        return {"kind": "remove_created", "params": {"resource_names": names}} if names else None
