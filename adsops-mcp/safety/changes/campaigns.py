"""Campaign builders: Search, Performance Max, Demand Gen. Always created PAUSED, in one atomic mutate."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.fmt import to_micros
from safety.changes.ads import add_rsa_to, validate_rsa, visible_length
from safety.changes.keywords import _kw_items
from safety.changes.media import inspect_image, load_verified
from safety.specs import ChangeSpec, Prepared, enum, mop, register

EU_NO = "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"


class _Temp:
    def __init__(self, cid: str):
        self.cid, self.n = cid, 0

    def __call__(self, kind: str) -> str:
        self.n -= 1
        return f"customers/{self.cid}/{kind}/{self.n}"


def _targeting(g, cid: str, params: dict[str, Any]) -> tuple[list[str], list[str]]:
    from tools.planning import geo_resources, lang_resources
    geos = geo_resources(g, params.get("geo") or ["UA"], locale=(params.get("languages") or ["uk"])[0])
    langs = lang_resources(params.get("languages") or ["uk", "ru"])
    return geos, langs


def _bidding(params: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    b = dict(params.get("bidding") or {})
    st = str(b.get("strategy") or allowed[0]).upper()
    if st == "TARGET_CPA":
        st = "MAXIMIZE_CONVERSIONS"
    if st == "TARGET_ROAS":
        st = "MAXIMIZE_CONVERSION_VALUE"
    if st not in allowed:
        raise ValueError(f"bidding.strategy must be one of {allowed}")
    return {"strategy": st, "target_cpa": b.get("target_cpa"), "target_roas": b.get("target_roas"),
            "cpc_bid_ceiling": b.get("cpc_bid_ceiling")}


def _apply_bidding(camp: Any, b: dict[str, Any]) -> None:
    st = b["strategy"]
    if st == "MANUAL_CPC":
        camp.manual_cpc.enhanced_cpc_enabled = False
    elif st == "MAXIMIZE_CONVERSIONS":
        camp.maximize_conversions.target_cpa_micros = to_micros(b.get("target_cpa")) or 0
    elif st == "MAXIMIZE_CONVERSION_VALUE":
        camp.maximize_conversion_value.target_roas = float(b.get("target_roas") or 0)
    elif st == "MAXIMIZE_CLICKS":
        camp.target_spend.cpc_bid_ceiling_micros = to_micros(b.get("cpc_bid_ceiling")) or 0


def _budget_op(g, temp: _Temp, name: str, amount: float) -> tuple[Any, str]:
    m = mop(g)
    b = m.campaign_budget_operation.create
    b.resource_name = temp("campaignBudgets")
    b.name = f"{name} budget {datetime.now().strftime('%Y%m%d%H%M%S')}"
    b.amount_micros = to_micros(amount)
    b.delivery_method = enum(g, "BudgetDeliveryMethodEnum", "STANDARD")
    b.explicitly_shared = False
    return m, b.resource_name


def _criteria_ops(g, camp_rn: str, geos: list[str], langs: list[str], negatives: list[dict[str, Any]] | None = None) -> list[Any]:
    ops = []
    for geo in geos:
        m = mop(g)
        c = m.campaign_criterion_operation.create
        c.campaign = camp_rn
        c.location.geo_target_constant = geo
        ops.append(m)
    for lang in langs:
        m = mop(g)
        c = m.campaign_criterion_operation.create
        c.campaign = camp_rn
        c.language.language_constant = lang
        ops.append(m)
    for k in negatives or []:
        m = mop(g)
        c = m.campaign_criterion_operation.create
        c.campaign = camp_rn
        c.negative = True
        c.keyword.text = k["text"]
        c.keyword.match_type = enum(g, "KeywordMatchTypeEnum", k["match_type"])
        ops.append(m)
    return ops


def _existing_names(g, cid: str, name: str) -> bool:
    from tools.common import esc
    return bool(g.search(cid, f"SELECT campaign.id, campaign.name, campaign.status FROM campaign "
                              f"WHERE campaign.name = '{esc(name)}' AND campaign.status != 'REMOVED'"))


def _rollback_pause(result) -> dict[str, Any] | None:
    camp = next((rn for rn in result.resource_names if rn and "/campaigns/" in rn), None)
    return {"kind": "set_status", "params": {"entity_type": "campaign", "ids": [camp.split("/")[-1]], "status": "PAUSED"}} if camp else None


# ----------------------------------------------------------------------------- Search

@register
class CreateSearchCampaign(ChangeSpec):
    kind = "create_search_campaign"

    def prepare(self, g, cid, params, guard):
        name = (params.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        if _existing_names(g, cid, name):
            raise ValueError(f"A campaign named '{name}' already exists")
        budget = float(params["daily_budget"])
        guard.check_daily_budget(name, budget)
        bidding = _bidding(params, ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "MANUAL_CPC", "MAXIMIZE_CLICKS"))
        geos, langs = _targeting(g, cid, params)
        negatives = _kw_items(params.get("negative_keywords") or [], "PHRASE")
        ad_groups, errors = [], []
        for i, ag in enumerate(params.get("ad_groups") or []):
            kws = _kw_items(ag.get("keywords") or [], ag.get("match_type") or "PHRASE")
            item = {"name": ag.get("name") or f"Ad group {i + 1}", "cpc_bid": ag.get("cpc_bid"), "keywords": kws}
            if not kws:
                errors.append(f"ad group '{item['name']}' has no keywords")
            if ag.get("rsa"):
                rsa, errs, warns = validate_rsa(ag["rsa"].get("headlines") or [], ag["rsa"].get("descriptions") or [],
                                                ag["rsa"].get("path1"), ag["rsa"].get("path2"), ag["rsa"].get("final_url"))
                errors += [f"{item['name']}: {e}" for e in errs]
                for w in warns:
                    guard.warn(f"{item['name']}: {w}")
                item["rsa"] = rsa
            else:
                guard.warn(f"Ad group '{item['name']}' has no RSA — it cannot serve until you add one.")
            if bidding["strategy"] == "MANUAL_CPC" and not item["cpc_bid"]:
                errors.append(f"ad group '{item['name']}' needs cpc_bid with MANUAL_CPC")
            ad_groups.append(item)
        if not ad_groups:
            errors.append("at least one ad group is required")
        if errors:
            raise ValueError("Campaign validation failed:\n- " + "\n- ".join(errors))
        network = {"search_partners": bool(params.get("search_partners", False)),
                   "display_expansion": bool(params.get("display_expansion", False))}
        if network["display_expansion"]:
            guard.warn("Display expansion ON — usually off for Search.")
        p = {"name": name, "daily_budget": budget, "bidding": bidding, "geo": geos, "languages": langs,
             "negatives": negatives, "ad_groups": ad_groups, "network": network,
             "presence_only": params.get("presence_only", True), "final_url_suffix": params.get("final_url_suffix")}
        diff = [{"resource": f"new campaign '{name}'", "field": "create", "before": None,
                 "after": {"status": "PAUSED", "channel": "SEARCH", "daily_budget": budget, "bidding": bidding,
                           "geo": geos, "languages": langs, "network": network,
                           "location_option": "PRESENCE" if p["presence_only"] else "PRESENCE_OR_INTEREST",
                           "negatives": len(negatives)}}]
        for ag in ad_groups:
            diff.append({"resource": f"new ad group '{ag['name']}'", "field": "create", "before": None,
                         "after": {"keywords": [f"{k['text']} [{k['match_type']}]" for k in ag["keywords"]],
                                   "cpc_bid": ag["cpc_bid"], "rsa_headlines": len(ag.get("rsa", {}).get("headlines", []))}})
        return Prepared(params=p, diff=diff, impact={"max_monthly_spend_when_enabled": round(budget * 30.4, 2)},
                        summary=f"Create PAUSED Search campaign '{name}' ({len(ad_groups)} ad groups, "
                                f"{sum(len(a['keywords']) for a in ad_groups)} keywords, budget {budget}/day)")

    def build(self, g, cid, params):
        temp = _Temp(cid)
        ops = []
        bop, budget_rn = _budget_op(g, temp, params["name"], params["daily_budget"])
        ops.append(bop)
        m = mop(g)
        c = m.campaign_operation.create
        c.resource_name = camp_rn = temp("campaigns")
        c.name = params["name"]
        c.status = enum(g, "CampaignStatusEnum", "PAUSED")
        c.advertising_channel_type = enum(g, "AdvertisingChannelTypeEnum", "SEARCH")
        c.campaign_budget = budget_rn
        c.network_settings.target_google_search = True
        c.network_settings.target_search_network = params["network"]["search_partners"]
        c.network_settings.target_content_network = params["network"]["display_expansion"]
        c.network_settings.target_partner_search_network = False
        geo_type = "PRESENCE" if params["presence_only"] else "PRESENCE_OR_INTEREST"
        c.geo_target_type_setting.positive_geo_target_type = enum(g, "PositiveGeoTargetTypeEnum", geo_type)
        c.geo_target_type_setting.negative_geo_target_type = enum(g, "NegativeGeoTargetTypeEnum", "PRESENCE")
        c.contains_eu_political_advertising = enum(g, "EuPoliticalAdvertisingStatusEnum", EU_NO)
        if params.get("final_url_suffix"):
            c.final_url_suffix = params["final_url_suffix"]
        _apply_bidding(c, params["bidding"])
        ops.append(m)
        ops += _criteria_ops(g, camp_rn, params["geo"], params["languages"], params["negatives"])
        for ag in params["ad_groups"]:
            m = mop(g)
            a = m.ad_group_operation.create
            a.resource_name = ag_rn = temp("adGroups")
            a.name = ag["name"]
            a.campaign = camp_rn
            a.status = enum(g, "AdGroupStatusEnum", "ENABLED")
            a.type_ = enum(g, "AdGroupTypeEnum", "SEARCH_STANDARD")
            if ag.get("cpc_bid"):
                a.cpc_bid_micros = to_micros(ag["cpc_bid"])
            ops.append(m)
            for k in ag["keywords"]:
                m = mop(g)
                kc = m.ad_group_criterion_operation.create
                kc.ad_group = ag_rn
                kc.status = enum(g, "AdGroupCriterionStatusEnum", "ENABLED")
                kc.keyword.text = k["text"]
                kc.keyword.match_type = enum(g, "KeywordMatchTypeEnum", k["match_type"])
                ops.append(m)
            if ag.get("rsa"):
                m = mop(g)
                aga = m.ad_group_ad_operation.create
                aga.ad_group = ag_rn
                aga.status = enum(g, "AdGroupAdStatusEnum", "ENABLED")
                add_rsa_to(g, aga, ag["rsa"])
                ops.append(m)
        return ops

    def rollback_from_result(self, cid, params, result):
        return _rollback_pause(result)


# ----------------------------------------------------------------------------- Performance Max

PMAX_TEXT = {"headlines": ("HEADLINE", 3, 15, 30), "long_headlines": ("LONG_HEADLINE", 1, 5, 90),
             "descriptions": ("DESCRIPTION", 2, 5, 90)}
PMAX_IMAGES = {"marketing_images": "MARKETING_IMAGE", "square_images": "SQUARE_MARKETING_IMAGE",
               "portrait_images": "PORTRAIT_MARKETING_IMAGE", "logos": "LOGO", "landscape_logos": "LANDSCAPE_LOGO"}


@register
class CreatePmaxCampaign(ChangeSpec):
    kind = "create_pmax_campaign"

    def prepare(self, g, cid, params, guard):
        name = (params.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        if _existing_names(g, cid, name):
            raise ValueError(f"A campaign named '{name}' already exists")
        budget = float(params["daily_budget"])
        guard.check_daily_budget(name, budget)
        bidding = _bidding(params, ("MAXIMIZE_CONVERSION_VALUE", "MAXIMIZE_CONVERSIONS"))
        geos, langs = _targeting(g, cid, params)
        ag = params.get("asset_group") or {}
        errors: list[str] = []
        text: dict[str, list[str]] = {}
        for key, (field, lo, hi, mx) in PMAX_TEXT.items():
            vals = [str(v) for v in ag.get(key) or []]
            if not lo <= len(vals) <= hi:
                errors.append(f"{key}: need {lo}-{hi} (got {len(vals)})")
            for v in vals:
                if visible_length(v) > mx:
                    errors.append(f"{key}: '{v}' is {visible_length(v)} chars (max {mx})")
            if len({v.lower() for v in vals}) != len(vals):
                errors.append(f"{key}: duplicates")
            text[key] = vals
        if text["descriptions"] and not any(visible_length(d) <= 60 for d in text["descriptions"]):
            errors.append("descriptions: at least one must be <= 60 chars")
        if text["headlines"] and not any(visible_length(h) <= 15 for h in text["headlines"]):
            guard.warn("No headline <= 15 chars — Google recommends at least one short headline.")
        if any("!" in h for h in text["headlines"]):
            errors.append("headlines cannot contain '!'")
        business_name = ag.get("business_name") or ""
        if not business_name or visible_length(business_name) > 25:
            errors.append("business_name required, max 25 chars")
        final_url = ag.get("final_url")
        if not final_url or not str(final_url).startswith(("http://", "https://")):
            errors.append("asset_group.final_url required")
        images = []
        for key, field in PMAX_IMAGES.items():
            for src in ag.get(key) or []:
                try:
                    images.append(inspect_image(str(src), field))
                except ValueError as ex:
                    errors.append(str(ex))
        have = {i["field_type"] for i in images}
        merchant = params.get("merchant_center_id")
        for need in ("MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "LOGO"):
            if need not in have:
                (guard.warn if merchant else errors.append)(f"missing {need} (min 1)")
        if errors:
            raise ValueError("PMax validation failed:\n- " + "\n- ".join(errors))
        videos = [str(v) for v in ag.get("youtube_video_ids") or []]
        if not videos:
            guard.warn("No YouTube video — Google will auto-generate one from your images unless you add a video.")
        signals = {"search_themes": [str(s) for s in (params.get("audience_signal") or {}).get("search_themes") or []][:25],
                   "audience_ids": [str(a) for a in (params.get("audience_signal") or {}).get("audience_ids") or []]}
        if not signals["search_themes"] and not signals["audience_ids"]:
            guard.warn("No audience signal — add search themes and/or an audience (customer list / remarketing).")
        opt_out = [str(x).upper() for x in params.get("asset_automation_opt_out") or []]
        p = {"name": name, "daily_budget": budget, "bidding": bidding, "geo": geos, "languages": langs,
             "merchant_center_id": int(merchant) if merchant else None, "feed_label": params.get("feed_label"),
             "asset_group": {"name": ag.get("name") or f"{name} - AG1", "final_url": final_url, "business_name": business_name,
                             "path1": ag.get("path1"), "path2": ag.get("path2"), **text, "images": images, "videos": videos},
             "signals": signals, "asset_automation_opt_out": opt_out}
        diff = [{"resource": f"new campaign '{name}'", "field": "create", "before": None,
                 "after": {"status": "PAUSED", "channel": "PERFORMANCE_MAX", "daily_budget": budget, "bidding": bidding,
                           "geo": geos, "languages": langs, "merchant_center_id": p["merchant_center_id"],
                           "listing_group": "all products" if merchant else None, "asset_automation_opt_out": opt_out}},
                {"resource": f"new asset group '{p['asset_group']['name']}'", "field": "create", "before": None,
                 "after": {"final_url": final_url, "headlines": len(text["headlines"]), "long_headlines": len(text["long_headlines"]),
                           "descriptions": len(text["descriptions"]), "images": {f: sum(1 for i in images if i["field_type"] == f) for f in have},
                           "videos": len(videos), "search_themes": signals["search_themes"], "audiences": signals["audience_ids"]}}]
        return Prepared(params=p, diff=diff, impact={"max_monthly_spend_when_enabled": round(budget * 30.4, 2)},
                        summary=f"Create PAUSED Performance Max '{name}' (budget {budget}/day)")

    def build(self, g, cid, params):
        temp = _Temp(cid)
        ops = []
        bop, budget_rn = _budget_op(g, temp, params["name"], params["daily_budget"])
        ops.append(bop)
        m = mop(g)
        c = m.campaign_operation.create
        c.resource_name = camp_rn = temp("campaigns")
        c.name = params["name"]
        c.status = enum(g, "CampaignStatusEnum", "PAUSED")
        c.advertising_channel_type = enum(g, "AdvertisingChannelTypeEnum", "PERFORMANCE_MAX")
        c.campaign_budget = budget_rn
        c.contains_eu_political_advertising = enum(g, "EuPoliticalAdvertisingStatusEnum", EU_NO)
        c.brand_guidelines_enabled = False  # business name + logo live in the asset group
        c.geo_target_type_setting.positive_geo_target_type = enum(g, "PositiveGeoTargetTypeEnum", "PRESENCE")
        if params.get("merchant_center_id"):
            c.shopping_setting.merchant_id = int(params["merchant_center_id"])
            if params.get("feed_label"):
                c.shopping_setting.feed_label = params["feed_label"]
        for t in params.get("asset_automation_opt_out") or []:
            s = type(c).AssetAutomationSetting()
            s.asset_automation_type = enum(g, "AssetAutomationTypeEnum", t)
            s.asset_automation_status = enum(g, "AssetAutomationStatusEnum", "OPTED_OUT")
            c.asset_automation_settings.append(s)
        _apply_bidding(c, params["bidding"])
        ops.append(m)
        ops += _criteria_ops(g, camp_rn, params["geo"], params["languages"])

        ag = params["asset_group"]
        links: list[tuple[str, str]] = []

        def text_asset(value: str, field: str) -> None:
            mm = mop(g)
            a = mm.asset_operation.create
            a.resource_name = temp("assets")
            a.text_asset.text = value
            ops.append(mm)
            links.append((a.resource_name, field))

        for key, (field, *_rest) in PMAX_TEXT.items():
            for v in ag[key]:
                text_asset(v, field)
        text_asset(ag["business_name"], "BUSINESS_NAME")
        for i, img in enumerate(ag["images"]):
            mm = mop(g)
            a = mm.asset_operation.create
            a.resource_name = temp("assets")
            a.name = f"{params['name']} {img['field_type'].lower()} {i + 1}"
            a.image_asset.data = load_verified(img)
            ops.append(mm)
            links.append((a.resource_name, img["field_type"]))
        for vid in ag["videos"]:
            mm = mop(g)
            a = mm.asset_operation.create
            a.resource_name = temp("assets")
            a.youtube_video_asset.youtube_video_id = vid
            ops.append(mm)
            links.append((a.resource_name, "YOUTUBE_VIDEO"))

        mm = mop(g)
        grp = mm.asset_group_operation.create
        grp.resource_name = ag_rn = temp("assetGroups")
        grp.name = ag["name"]
        grp.campaign = camp_rn
        grp.final_urls.append(ag["final_url"])
        grp.status = enum(g, "AssetGroupStatusEnum", "ENABLED")
        if ag.get("path1"):
            grp.path1 = ag["path1"]
        if ag.get("path2"):
            grp.path2 = ag["path2"]
        ops.append(mm)
        for asset_rn, field in links:
            mm = mop(g)
            link = mm.asset_group_asset_operation.create
            link.asset_group = ag_rn
            link.asset = asset_rn
            link.field_type = enum(g, "AssetFieldTypeEnum", field)
            ops.append(mm)
        if params.get("merchant_center_id"):
            mm = mop(g)
            lg = mm.asset_group_listing_group_filter_operation.create
            lg.asset_group = ag_rn
            lg.type_ = enum(g, "ListingGroupFilterTypeEnum", "UNIT_INCLUDED")
            lg.listing_source = enum(g, "ListingGroupFilterListingSourceEnum", "SHOPPING")
            ops.append(mm)
        for theme in params["signals"]["search_themes"]:
            mm = mop(g)
            s = mm.asset_group_signal_operation.create
            s.asset_group = ag_rn
            s.search_theme.text = theme
            ops.append(mm)
        for aud in params["signals"]["audience_ids"]:
            mm = mop(g)
            s = mm.asset_group_signal_operation.create
            s.asset_group = ag_rn
            s.audience.audience = aud if aud.startswith("customers/") else f"customers/{cid}/audiences/{int(aud)}"
            ops.append(mm)
        return ops

    def rollback_from_result(self, cid, params, result):
        return _rollback_pause(result)


# ----------------------------------------------------------------------------- Demand Gen

DG_IMAGES = {"marketing_images": "MARKETING_IMAGE", "square_images": "SQUARE_MARKETING_IMAGE",
             "portrait_images": "PORTRAIT_MARKETING_IMAGE", "logos": "LOGO"}


@register
class CreateDemandGenCampaign(ChangeSpec):
    kind = "create_demand_gen_campaign"

    def prepare(self, g, cid, params, guard):
        name = (params.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        if _existing_names(g, cid, name):
            raise ValueError(f"A campaign named '{name}' already exists")
        budget = float(params["daily_budget"])
        guard.check_daily_budget(name, budget)
        bidding = _bidding(params, ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "MAXIMIZE_CLICKS"))
        geos, langs = _targeting(g, cid, params)
        ad = params.get("ad")
        errors: list[str] = []
        ad_p = None
        if ad:
            hl = [str(h) for h in ad.get("headlines") or []]
            ds = [str(d) for d in ad.get("descriptions") or []]
            if not 1 <= len(hl) <= 5 or any(visible_length(h) > 40 for h in hl):
                errors.append("ad.headlines: 1-5, max 40 chars each")
            if not 1 <= len(ds) <= 5 or any(visible_length(d) > 90 for d in ds):
                errors.append("ad.descriptions: 1-5, max 90 chars each")
            bn = ad.get("business_name") or ""
            if not bn or visible_length(bn) > 25:
                errors.append("ad.business_name required, max 25 chars")
            if not str(ad.get("final_url") or "").startswith(("http://", "https://")):
                errors.append("ad.final_url required")
            images = []
            for key, field in DG_IMAGES.items():
                for src in ad.get(key) or []:
                    try:
                        images.append(inspect_image(str(src), field))
                    except ValueError as ex:
                        errors.append(str(ex))
            if not any(i["field_type"] == "LOGO" for i in images):
                errors.append("ad: at least one logo (1:1) is required")
            if not any(i["field_type"] in ("MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE", "PORTRAIT_MARKETING_IMAGE") for i in images):
                errors.append("ad: at least one marketing image is required")
            ad_p = {"headlines": hl, "descriptions": ds, "business_name": bn, "final_url": ad.get("final_url"),
                    "call_to_action": ad.get("call_to_action"), "images": images}
        else:
            guard.warn("No ad provided — the ad group cannot serve until you add a Demand Gen ad.")
        if errors:
            raise ValueError("Demand Gen validation failed:\n- " + "\n- ".join(errors))
        p = {"name": name, "daily_budget": budget, "bidding": bidding, "geo": geos, "languages": langs,
             "ad_group_name": params.get("ad_group_name") or f"{name} - AG1", "ad": ad_p}
        diff = [{"resource": f"new campaign '{name}'", "field": "create", "before": None,
                 "after": {"status": "PAUSED", "channel": "DEMAND_GEN", "daily_budget": budget, "bidding": bidding,
                           "geo": geos, "languages": langs, "ad_group": p["ad_group_name"],
                           "ad": {"headlines": ad_p["headlines"], "images": len(ad_p["images"])} if ad_p else None}}]
        return Prepared(params=p, diff=diff, impact={"max_monthly_spend_when_enabled": round(budget * 30.4, 2)},
                        summary=f"Create PAUSED Demand Gen '{name}' (budget {budget}/day)")

    def build(self, g, cid, params):
        temp = _Temp(cid)
        ops = []
        bop, budget_rn = _budget_op(g, temp, params["name"], params["daily_budget"])
        ops.append(bop)
        m = mop(g)
        c = m.campaign_operation.create
        c.resource_name = camp_rn = temp("campaigns")
        c.name = params["name"]
        c.status = enum(g, "CampaignStatusEnum", "PAUSED")
        c.advertising_channel_type = enum(g, "AdvertisingChannelTypeEnum", "DEMAND_GEN")
        c.campaign_budget = budget_rn
        c.contains_eu_political_advertising = enum(g, "EuPoliticalAdvertisingStatusEnum", EU_NO)
        _apply_bidding(c, params["bidding"])
        ops.append(m)
        ops += _criteria_ops(g, camp_rn, params["geo"], params["languages"])
        m = mop(g)
        a = m.ad_group_operation.create
        a.resource_name = ag_rn = temp("adGroups")
        a.name = params["ad_group_name"]
        a.campaign = camp_rn
        a.status = enum(g, "AdGroupStatusEnum", "ENABLED")
        ops.append(m)
        ad = params.get("ad")
        if ad:
            img_rns: dict[str, list[str]] = {}
            for i, img in enumerate(ad["images"]):
                mm = mop(g)
                asset = mm.asset_operation.create
                asset.resource_name = temp("assets")
                asset.name = f"{params['name']} {img['field_type'].lower()} {i + 1}"
                asset.image_asset.data = load_verified(img)
                ops.append(mm)
                img_rns.setdefault(img["field_type"], []).append(asset.resource_name)
            mm = mop(g)
            aga = mm.ad_group_ad_operation.create
            aga.ad_group = ag_rn
            aga.status = enum(g, "AdGroupAdStatusEnum", "ENABLED")
            aga.ad.final_urls.append(ad["final_url"])
            dg = aga.ad.demand_gen_multi_asset_ad
            for field, attr in (("MARKETING_IMAGE", "marketing_images"), ("SQUARE_MARKETING_IMAGE", "square_marketing_images"),
                                ("PORTRAIT_MARKETING_IMAGE", "portrait_marketing_images"), ("LOGO", "logo_images")):
                for rn in img_rns.get(field, []):
                    x = g.type("AdImageAsset")
                    x.asset = rn
                    getattr(dg, attr).append(x)
            for h in ad["headlines"]:
                t = g.type("AdTextAsset")
                t.text = h
                dg.headlines.append(t)
            for d in ad["descriptions"]:
                t = g.type("AdTextAsset")
                t.text = d
                dg.descriptions.append(t)
            dg.business_name = ad["business_name"]
            if ad.get("call_to_action"):
                dg.call_to_action_text = ad["call_to_action"]
            ops.append(mm)
        return ops

    def rollback_from_result(self, cid, params, result):
        return _rollback_pause(result)
