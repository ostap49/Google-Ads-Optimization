"""Responsive search ads and sitelink / callout / structured snippet assets."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from memory.brief import targets
from safety.specs import ChangeSpec, Prepared, enum, mop, register, rn_ad_group, rn_campaign
from tools.text import ad_text_length

H_MAX, D_MAX, PATH_MAX = 30, 90, 15
SNIPPET_HEADERS = ("Amenities", "Brands", "Courses", "Degree programs", "Destinations", "Featured hotels",
                   "Insurance coverage", "Models", "Neighborhoods", "Service catalog", "Shows", "Styles", "Types")
_DYN = re.compile(r"\{[^{}:]+:([^{}]*)\}")


def visible_length(text: str) -> int:
    """Length as served: dynamic insertion {KeyWord:default} / customizers count their default text."""
    return ad_text_length(_DYN.sub(lambda m: m.group(1), text))


def _policy_issues(text: str, kind: str) -> list[str]:
    issues = []
    if kind == "headline" and "!" in text:
        issues.append("headlines cannot contain '!'")
    if kind == "description" and text.count("!") > 1:
        issues.append("max one '!' per description")
    if re.search(r"([!?.,])\1", text):
        issues.append("repeated punctuation")
    if any(unicodedata.category(ch) == "So" and ch not in "®™©" for ch in text):
        issues.append("emoji/symbols are not allowed")
    if "  " in text or text != text.strip():
        issues.append("extra spaces")
    return issues


def _norm_items(items: list[Any], kind: str, max_len: int, pin_prefix: str, max_pin: int) -> tuple[list[dict[str, Any]], list[str]]:
    out, errors, seen = [], [], set()
    for i, it in enumerate(items or []):
        text = it if isinstance(it, str) else it.get("text", "")
        pin = None if isinstance(it, str) else it.get("pin")
        text = unicodedata.normalize("NFC", str(text))
        n = visible_length(text)
        if n == 0:
            errors.append(f"{kind} {i + 1} is empty")
        if n > max_len:
            errors.append(f"{kind} {i + 1} '{text}' is {n} chars (max {max_len})")
        for issue in _policy_issues(text, kind):
            errors.append(f"{kind} {i + 1} '{text}': {issue}")
        key = text.lower()
        if key in seen:
            errors.append(f"duplicate {kind} '{text}'")
        seen.add(key)
        if pin is not None and int(pin) not in range(1, max_pin + 1):
            errors.append(f"{kind} {i + 1}: pin must be 1..{max_pin}")
        out.append({"text": text, "chars": n, **({"pin": f"{pin_prefix}_{int(pin)}"} if pin else {})})
    return out, errors


def validate_rsa(headlines: list[Any], descriptions: list[Any], path1: str | None, path2: str | None,
                 final_url: str | None, require_full: bool = False) -> tuple[dict[str, Any], list[str], list[str]]:
    hl, errors = _norm_items(headlines, "headline", H_MAX, "HEADLINE", 3)
    ds, e2 = _norm_items(descriptions, "description", D_MAX, "DESCRIPTION", 2)
    errors += e2
    warnings = []
    if not 3 <= len(hl) <= 15:
        errors.append(f"RSA needs 3-15 headlines (got {len(hl)})")
    if not 2 <= len(ds) <= 4:
        errors.append(f"RSA needs 2-4 descriptions (got {len(ds)})")
    if require_full and (len(hl) != 15 or len(ds) != 4):
        errors.append("require_full: provide exactly 15 headlines and 4 descriptions")
    elif len(hl) < 15 or len(ds) < 4:
        warnings.append(f"{len(hl)}/15 headlines, {len(ds)}/4 descriptions — fill all slots for best ad strength")
    for p, name in ((path1, "path1"), (path2, "path2")):
        if p and visible_length(p) > PATH_MAX:
            errors.append(f"{name} '{p}' is {visible_length(p)} chars (max {PATH_MAX})")
        if p and (" " in p or "/" in p):
            errors.append(f"{name} '{p}' cannot contain spaces or '/'")
    if path2 and not path1:
        errors.append("path2 requires path1")
    if not final_url or not re.match(r"^https?://", final_url):
        errors.append("final_url must be an http(s) URL")
    pinned = sum(1 for h in hl if h.get("pin"))
    if pinned >= 3:
        warnings.append(f"{pinned} pinned headlines — heavy pinning lowers ad strength")
    return {"headlines": hl, "descriptions": ds, "path1": path1, "path2": path2, "final_url": final_url}, errors, warnings


def add_rsa_to(g, ad_group_ad: Any, rsa: dict[str, Any]) -> None:
    ad = ad_group_ad.ad
    ad.final_urls.append(rsa["final_url"])
    for kind, items in (("headlines", rsa["headlines"]), ("descriptions", rsa["descriptions"])):
        for it in items:
            a = g.type("AdTextAsset")
            a.text = it["text"]
            if it.get("pin"):
                a.pinned_field = enum(g, "ServedAssetFieldTypeEnum", it["pin"])
            getattr(ad.responsive_search_ad, kind).append(a)
    if rsa.get("path1"):
        ad.responsive_search_ad.path1 = rsa["path1"]
    if rsa.get("path2"):
        ad.responsive_search_ad.path2 = rsa["path2"]


@register
class CreateRsa(ChangeSpec):
    kind = "create_rsa"

    def prepare(self, g, cid, params, guard):
        ag_id = str(int(params["ad_group_id"]))
        ag = g.search(cid, "SELECT ad_group.id, ad_group.name, campaign.id, campaign.name, campaign.advertising_channel_type "
                           f"FROM ad_group WHERE ad_group.id = {ag_id}")
        if not ag:
            raise ValueError(f"Ad group {ag_id} not found")
        ag = ag[0]
        if ag["campaign.advertising_channel_type"] != "SEARCH":
            raise ValueError("RSAs belong in Search campaigns.")
        rsa, errors, warnings = validate_rsa(params.get("headlines") or [], params.get("descriptions") or [],
                                             params.get("path1"), params.get("path2"), params.get("final_url"),
                                             bool(params.get("require_full")))
        if errors:
            raise ValueError("RSA validation failed:\n- " + "\n- ".join(errors))
        for w in warnings:
            guard.warn(w)
        t = targets(cid)
        copy = " ".join(h["text"] for h in rsa["headlines"] + rsa["descriptions"]).lower()
        for c in t["competitor_terms"]:
            if c in copy:
                guard.warn(f"Ad copy mentions competitor term '{c}' (trademark policy risk).")
        status = str(params.get("status") or "PAUSED").upper()
        diff = [{"resource": f"ad group {ag_id} ({ag['campaign.name']} / {ag['ad_group.name']})", "field": "new_rsa",
                 "before": None, "after": {"status": status, "headlines": [f"{h['text']} ({h['chars']})" + (f" [{h['pin']}]" if h.get('pin') else "") for h in rsa["headlines"]],
                                           "descriptions": [f"{d['text']} ({d['chars']})" for d in rsa["descriptions"]],
                                           "path": "/".join(p for p in (rsa["path1"], rsa["path2"]) if p), "final_url": rsa["final_url"]}}]
        return Prepared(params={"ad_group_id": ag_id, "status": status, "rsa": rsa}, diff=diff,
                        campaign_ids=[str(ag["campaign.id"])],
                        summary=f"New RSA ({status}) in '{ag['campaign.name']} / {ag['ad_group.name']}': "
                                f"{len(rsa['headlines'])} headlines, {len(rsa['descriptions'])} descriptions")

    def build(self, g, cid, params):
        m = mop(g)
        aga = m.ad_group_ad_operation.create
        aga.ad_group = rn_ad_group(cid, params["ad_group_id"])
        aga.status = enum(g, "AdGroupAdStatusEnum", params["status"])
        add_rsa_to(g, aga, params["rsa"])
        return [m]

    def rollback_from_result(self, cid, params, result):
        rn = next((x for x in result.resource_names if x), None)
        if not rn:
            return None
        return {"kind": "set_status", "params": {"entity_type": "ad", "ids": [rn.split("/")[-1]], "status": "PAUSED"}}


# ----------------------------------------------------------------------------- assets

ASSET_FIELD = {"sitelink": "SITELINK", "callout": "CALLOUT", "structured_snippet": "STRUCTURED_SNIPPET"}


def _validate_assets(asset_type: str, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    errs, out = [], []
    for i, it in enumerate(items):
        if asset_type == "sitelink":
            text, d1, d2, url = it.get("text") or it.get("link_text"), it.get("description1"), it.get("description2"), it.get("final_url")
            if not text or visible_length(text) > 25:
                errs.append(f"sitelink {i + 1}: link text required, max 25 chars ('{text}')")
            for d in (d1, d2):
                if d and visible_length(d) > 35:
                    errs.append(f"sitelink {i + 1}: description '{d}' > 35 chars")
            if bool(d1) != bool(d2):
                errs.append(f"sitelink {i + 1}: provide both description lines or neither")
            if not url or not re.match(r"^https?://", url):
                errs.append(f"sitelink {i + 1}: final_url required")
            out.append({"text": text, "description1": d1, "description2": d2, "final_url": url})
        elif asset_type == "callout":
            text = it if isinstance(it, str) else it.get("text")
            if not text or visible_length(text) > 25:
                errs.append(f"callout {i + 1}: max 25 chars ('{text}')")
            for issue in _policy_issues(text or "", "description"):
                errs.append(f"callout '{text}': {issue}")
            out.append({"text": text})
        elif asset_type == "structured_snippet":
            header, values = it.get("header"), it.get("values") or []
            if header not in SNIPPET_HEADERS:
                errs.append(f"snippet {i + 1}: header must be one of {SNIPPET_HEADERS} (English names; Google localizes)")
            if not 3 <= len(values) <= 10:
                errs.append(f"snippet {i + 1}: needs 3-10 values")
            for v in values:
                if visible_length(v) > 25:
                    errs.append(f"snippet value '{v}' > 25 chars")
            out.append({"header": header, "values": values})
        else:
            raise ValueError(f"asset_type must be one of {sorted(ASSET_FIELD)}")
    return out, errs


@register
class CreateAssets(ChangeSpec):
    kind = "create_assets"

    def prepare(self, g, cid, params, guard):
        asset_type = params["asset_type"]
        level = params.get("level", "campaign")
        items, errs = _validate_assets(asset_type, params.get("items") or [])
        if errs:
            raise ValueError("Asset validation failed:\n- " + "\n- ".join(errs))
        if not items:
            raise ValueError("items is empty")
        field = ASSET_FIELD[asset_type]
        if level == "campaign":
            ids = [str(int(c)) for c in params.get("campaign_ids") or []]
            rows = g.search(cid, f"SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id IN ({', '.join(ids)})") if ids else []
            names = {str(x["campaign.id"]): f"campaign '{x['campaign.name']}'" for x in rows}
            camp_ids = ids
        elif level == "ad_group":
            ids = [str(int(a)) for a in params.get("ad_group_ids") or []]
            rows = g.search(cid, f"SELECT ad_group.id, ad_group.name, campaign.id FROM ad_group WHERE ad_group.id IN ({', '.join(ids)})") if ids else []
            names = {str(x["ad_group.id"]): f"ad group '{x['ad_group.name']}'" for x in rows}
            camp_ids = sorted({str(x["campaign.id"]) for x in rows})
        elif level == "account":
            ids, names, camp_ids = ["account"], {"account": "account"}, []
        else:
            raise ValueError("level must be account | campaign | ad_group")
        missing = [i for i in ids if i not in names]
        if missing or not ids:
            raise ValueError(f"Targets not found / empty: {missing}")
        if asset_type == "sitelink" and len(items) < 2:
            guard.warn("Sitelinks need at least 2 to show (4+ recommended).")
        diff = []
        for i in ids:
            for it in items:
                label = it.get("text") or f"{it.get('header')}: {', '.join(it.get('values') or [])}"
                diff.append({"resource": names[i], "field": asset_type, "before": None, "after": label})
        return Prepared(params={"asset_type": asset_type, "field_type": field, "level": level, "targets": ids, "items": items},
                        diff=diff, campaign_ids=camp_ids,
                        summary=f"Create {len(items)} {asset_type}(s) and link to {len(ids)} {level} target(s)")

    def build(self, g, cid, params):
        ops, temp = [], -1
        asset_rns = []
        for it in params["items"]:
            m = mop(g)
            a = m.asset_operation.create
            a.resource_name = f"customers/{cid}/assets/{temp}"
            asset_rns.append(a.resource_name)
            temp -= 1
            if params["asset_type"] == "sitelink":
                a.sitelink_asset.link_text = it["text"]
                if it.get("description1"):
                    a.sitelink_asset.description1 = it["description1"]
                    a.sitelink_asset.description2 = it["description2"]
                a.final_urls.append(it["final_url"])
            elif params["asset_type"] == "callout":
                a.callout_asset.callout_text = it["text"]
            else:
                a.structured_snippet_asset.header = it["header"]
                a.structured_snippet_asset.values.extend(it["values"])
            ops.append(m)
        field = enum(g, "AssetFieldTypeEnum", params["field_type"])
        for target in params["targets"]:
            for rn in asset_rns:
                m = mop(g)
                if params["level"] == "campaign":
                    link = m.campaign_asset_operation.create
                    link.campaign = rn_campaign(cid, target)
                elif params["level"] == "ad_group":
                    link = m.ad_group_asset_operation.create
                    link.ad_group = rn_ad_group(cid, target)
                else:
                    link = m.customer_asset_operation.create
                link.asset = rn
                link.field_type = field
                ops.append(m)
        return ops

    def rollback_from_result(self, cid, params, result):
        links = [rn for rn in result.resource_names if rn and ("/campaignAssets/" in rn or "/adGroupAssets/" in rn or "/customerAssets/" in rn)]
        return {"kind": "remove_created", "params": {"resource_names": links}} if links else None
