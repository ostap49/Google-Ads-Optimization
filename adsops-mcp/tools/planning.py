"""Keyword research (KeywordPlanIdeaService) and forecasts."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from core.dates import today_in
from core.fmt import compact, from_micros, r, to_micros
from tools._registry import tool
from tools.common import account_header, ctx
from tools.text import STOPWORDS, tokens

GEO = {"UA": 2804, "US": 2840, "GB": 2826, "UK": 2826, "PL": 2616, "DE": 2276, "CA": 2124, "AU": 2036,
       "MD": 2498, "KZ": 2398, "CZ": 2203, "IE": 2372, "FR": 2250, "ES": 2724, "IT": 2380, "NL": 2528}
LANG = {"en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "cs": 1021, "pl": 1030, "ru": 1031,
        "ro": 1032, "uk": 1036, "tr": 1037, "nl": 1010, "pt": 1014}


def geo_resources(g, geo: list[str] | str | None, locale: str = "en") -> list[str]:
    items = [geo] if isinstance(geo, str) else list(geo or ["UA"])
    out = []
    for item in items:
        s = str(item).strip()
        if s.startswith("geoTargetConstants/"):
            out.append(s)
        elif s.isdigit():
            out.append(f"geoTargetConstants/{s}")
        elif s.upper() in GEO:
            out.append(f"geoTargetConstants/{GEO[s.upper()]}")
        else:  # free-text location name, e.g. "Kyiv", "Lviv Oblast", "London"
            svc = g.service("GeoTargetConstantService")
            req = g.type("SuggestGeoTargetConstantsRequest")
            req.locale = locale
            req.location_names.names.append(s)
            resp = svc.suggest_geo_target_constants(request=req)
            if not resp.geo_target_constant_suggestions:
                raise ValueError(f"Unknown location '{s}'")
            out.append(resp.geo_target_constant_suggestions[0].geo_target_constant.resource_name)
    return out


def lang_resources(languages: list[str] | str | None) -> list[str]:
    items = [languages] if isinstance(languages, str) else list(languages or ["uk", "ru"])
    out = []
    for item in items:
        s = str(item).strip().lower()
        if s.startswith("languageconstants/"):
            out.append("languageConstants/" + s.split("/")[-1])
        elif s.isdigit():
            out.append(f"languageConstants/{s}")
        elif s in LANG:
            out.append(f"languageConstants/{LANG[s]}")
        else:
            raise ValueError(f"Unknown language '{item}'. Use one of {sorted(LANG)} or a languageConstants ID.")
    return out


def _themes(ideas: list[dict[str, Any]], seeds: list[str]) -> None:
    """Theme = the most common Planner concept on the keyword (ignoring near-universal seed concepts),
    else the most common shared non-seed token, else "core"."""
    n = max(1, len(ideas))
    cfreq = Counter(c for i in ideas for c in set(i.get("concepts") or []))
    cfreq = Counter({c: v for c, v in cfreq.items() if v < 0.8 * n or n < 5})
    seed_toks = set(t for s in seeds for t in tokens(s))
    tfreq = Counter(t for i in ideas for t in set(tokens(i["keyword"])) if t not in STOPWORDS and t not in seed_toks and len(t) > 2)
    for i in ideas:
        cands = [c for c in (i.get("concepts") or []) if cfreq.get(c, 0) >= 2]
        if cands:
            i["theme"] = max(cands, key=lambda c: (cfreq[c], c))
            continue
        toks = [t for t in set(tokens(i["keyword"])) if tfreq.get(t, 0) >= 2]
        i["theme"] = max(toks, key=lambda t: (tfreq[t], t)) if toks else "core"


@tool(title="Keyword research (Keyword Planner)")
def keyword_research(
    customer_id: str,
    seed_keywords: list[str] | None = None,
    url: str | None = None,
    geo: list[str] | None = None,
    languages: list[str] | None = None,
    include_adult: bool = False,
    include_partners: bool = False,
    max_ideas: int = 300,
    min_volume: int = 10,
    limit: int | None = 60,
    sort_by: str = "avg_monthly_searches",
) -> dict[str, Any]:
    """Keyword ideas from seed keywords and/or a URL. geo: country codes (UA, US, GB...), geo IDs or names
    ("Kyiv", "London"); default ["UA"]. languages: codes (uk, ru, en, pl, de...); default ["uk", "ru"] —
    one Planner request per language, merged (max volume wins). Returns avg monthly searches, competition,
    top-of-page bid range (account currency), 12-month trend, grouped into themes (Planner concepts,
    falling back to shared-token clustering)."""
    if not seed_keywords and not url:
        raise ValueError("Provide seed_keywords and/or url.")
    g, cid, info, _ = ctx(customer_id)
    geos = geo_resources(g, geo, locale=(languages or ["uk"])[0] if languages else "uk")
    langs = lang_resources(languages)
    svc = g.service("KeywordPlanIdeaService")
    network = g.enum("KeywordPlanNetworkEnum")
    merged: dict[str, dict[str, Any]] = {}
    for lang in langs:
        req = g.type("GenerateKeywordIdeasRequest")
        req.customer_id = cid
        req.language = lang
        req.geo_target_constants.extend(geos)
        req.include_adult_keywords = include_adult
        req.keyword_plan_network = network.GOOGLE_SEARCH_AND_PARTNERS if include_partners else network.GOOGLE_SEARCH
        req.keyword_annotation.append(g.enum("KeywordPlanKeywordAnnotationEnum").KEYWORD_CONCEPT)
        if seed_keywords and url:
            req.keyword_and_url_seed.url = url
            req.keyword_and_url_seed.keywords.extend(seed_keywords)
        elif seed_keywords:
            req.keyword_seed.keywords.extend(seed_keywords)
        else:
            req.url_seed.url = url
        req.page_size = min(1000, max_ideas)
        count = 0
        for idea in g._retry(lambda: list(svc.generate_keyword_ideas(request=req)), "generate_keyword_ideas"):
            count += 1
            if count > max_ideas:
                break
            mtr = idea.keyword_idea_metrics
            vol = int(mtr.avg_monthly_searches or 0)
            text = idea.text.lower()
            concepts = [c.name for c in idea.keyword_annotations.concepts if c.name] if idea.keyword_annotations else []
            prev = merged.get(text)
            if prev and prev["avg_monthly_searches"] >= vol:
                prev["languages"].append(lang.split("/")[-1])
                continue
            trend = [int(x.monthly_searches or 0) for x in mtr.monthly_search_volumes][-12:]
            merged[text] = {
                "keyword": idea.text,
                "avg_monthly_searches": vol,
                "competition": mtr.competition.name if hasattr(mtr.competition, "name") else str(mtr.competition),
                "competition_index": int(mtr.competition_index or 0),
                "top_of_page_bid_low": from_micros(mtr.low_top_of_page_bid_micros),
                "top_of_page_bid_high": from_micros(mtr.high_top_of_page_bid_micros),
                "trend_12m": trend,
                "yoy_pct": r((trend[-1] / trend[0] - 1) * 100, 0) if len(trend) >= 12 and trend[0] else None,
                "concepts": concepts,
                "languages": (prev["languages"] if prev else []) + [lang.split("/")[-1]],
            }
    ideas = [i for i in merged.values() if i["avg_monthly_searches"] >= min_volume]
    _themes(ideas, seed_keywords or [])
    themes: dict[str, dict[str, Any]] = defaultdict(lambda: {"keywords": 0, "volume": 0, "top": []})
    for i in sorted(ideas, key=lambda x: -x["avg_monthly_searches"]):
        th = themes[i["theme"]]
        th["keywords"] += 1
        th["volume"] += i["avg_monthly_searches"]
        if len(th["top"]) < 5:
            th["top"].append(i["keyword"])
        lows = [x["top_of_page_bid_low"] for x in ideas if x["theme"] == i["theme"] and x["top_of_page_bid_low"]]
        th["median_bid_low"] = sorted(lows)[len(lows) // 2] if lows else None
    for i in ideas:
        i.pop("concepts", None)
    out = compact(ideas, limit=int(limit or 60), sort_by=sort_by)
    out.update(account_header(cid, info))
    out["themes"] = dict(sorted(themes.items(), key=lambda kv: -kv[1]["volume"])[:30])
    out["targeting"] = {"geo": geos, "languages": langs, "network": "GOOGLE_SEARCH_AND_PARTNERS" if include_partners else "GOOGLE_SEARCH"}
    out["note"] = "Bids are in account currency. Volumes are Planner estimates (ranges for low-spend accounts)."
    return out


@tool(title="Traffic forecast (Keyword Planner)")
def forecast(
    customer_id: str,
    keywords: list[str],
    match_type: str = "PHRASE",
    max_cpc: float | None = None,
    daily_budget: float | None = None,
    bidding: str = "manual_cpc",
    geo: list[str] | None = None,
    languages: list[str] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Forecast clicks, cost, avg CPC, conversions and CPA for a keyword set over the next `days` days
    (KeywordPlanIdeaService.GenerateKeywordForecastMetrics). keywords may carry a match type as
    "[exact]" / "\\"phrase\\"" / plain (uses match_type). bidding: manual_cpc (needs max_cpc) |
    maximize_clicks | maximize_conversions (need daily_budget)."""
    g, cid, info, _ = ctx(customer_id)
    tz_today = today_in(info.get("time_zone"))
    req = g.type("GenerateKeywordForecastMetricsRequest")
    req.customer_id = cid
    req.currency_code = info["currency"]
    req.forecast_period.start_date = (tz_today + timedelta(days=1)).isoformat()
    req.forecast_period.end_date = (tz_today + timedelta(days=max(1, days))).isoformat()
    camp = req.campaign
    camp.language_constants.extend(lang_resources(languages))
    camp.geo_target_constants.extend(geo_resources(g, geo))
    mt = g.enum("KeywordMatchTypeEnum")
    ag = g.type("ForecastAdGroup")
    for kw in keywords:
        k = g.type("KeywordInfo")
        s = kw.strip()
        if s.startswith("[") and s.endswith("]"):
            k.text, k.match_type = s[1:-1], mt.EXACT
        elif s.startswith('"') and s.endswith('"'):
            k.text, k.match_type = s[1:-1], mt.PHRASE
        else:
            k.text, k.match_type = s, getattr(mt, match_type.upper())
        ag.keywords.append(k)
    camp.ad_groups.append(ag)
    if bidding == "manual_cpc":
        if not max_cpc:
            raise ValueError("manual_cpc forecast needs max_cpc (account currency).")
        camp.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = to_micros(max_cpc)
        if daily_budget:
            camp.bidding_strategy.manual_cpc_bidding_strategy.daily_budget_micros = to_micros(daily_budget)
    elif bidding == "maximize_clicks":
        if not daily_budget:
            raise ValueError("maximize_clicks forecast needs daily_budget.")
        camp.bidding_strategy.maximize_clicks_bidding_strategy.daily_target_spend_micros = to_micros(daily_budget)
        if max_cpc:
            camp.bidding_strategy.maximize_clicks_bidding_strategy.max_cpc_bid_ceiling_micros = to_micros(max_cpc)
    elif bidding == "maximize_conversions":
        if not daily_budget:
            raise ValueError("maximize_conversions forecast needs daily_budget.")
        camp.bidding_strategy.maximize_conversions_bidding_strategy.daily_target_spend_micros = to_micros(daily_budget)
    else:
        raise ValueError("bidding must be manual_cpc | maximize_clicks | maximize_conversions")
    svc = g.service("KeywordPlanIdeaService")
    resp = g._retry(lambda: svc.generate_keyword_forecast_metrics(request=req), "generate_keyword_forecast_metrics")
    fm = resp.campaign_forecast_metrics
    out = account_header(cid, info)
    out.update({
        "period": {"start": req.forecast_period.start_date, "end": req.forecast_period.end_date},
        "keywords": len(keywords), "bidding": bidding, "max_cpc": max_cpc, "daily_budget": daily_budget,
        "forecast": {
            "clicks": r(fm.clicks, 0), "cost": from_micros(fm.cost_micros), "avg_cpc": from_micros(fm.average_cpc_micros),
            "conversions": r(fm.conversions, 1), "cpa": from_micros(fm.average_cpa_micros),
            "daily_cost": r((from_micros(fm.cost_micros) or 0) / max(1, days)),
        },
        "note": "Planner forecasts are directional; conversions use the account's historical conversion rate.",
    })
    return out
