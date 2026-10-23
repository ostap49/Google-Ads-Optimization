"""Search terms report with flags + n-gram analysis."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.fmt import compact, div, r
from gads.queries import search_terms as Q
from memory.brief import targets
from tools._registry import tool
from tools.common import account_header, agg, cost_threshold, ctx, limit_default, m, safe
from tools.text import IRRELEVANT_DEFAULT, STOPWORDS, negative_matches, ngrams, tokens


def load_negatives(g, cid: str) -> dict[str, Any]:
    """Negative keyword inventory: per campaign, per ad group, shared lists, account-level lists."""
    camp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    adg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    account: list[dict[str, Any]] = []
    for row in g.search(cid, Q.CAMPAIGN_NEGATIVES):
        camp[str(row["campaign.id"])].append({"text": row["campaign_criterion.keyword.text"],
                                              "match_type": row["campaign_criterion.keyword.match_type"],
                                              "level": "campaign"})
    for row in g.search(cid, Q.AD_GROUP_NEGATIVES):
        adg[str(row["ad_group.id"])].append({"text": row["ad_group_criterion.keyword.text"],
                                             "match_type": row["ad_group_criterion.keyword.match_type"],
                                             "level": "ad_group"})
    shared: dict[str, list[dict[str, Any]]] = defaultdict(list)
    shared_names: dict[str, str] = {}
    for row in g.search(cid, Q.SHARED_CRITERIA):
        sid = str(row["shared_set.id"])
        shared_names[sid] = row["shared_set.name"]
        item = {"text": row["shared_criterion.keyword.text"], "match_type": row["shared_criterion.keyword.match_type"],
                "level": "shared_list", "list": row["shared_set.name"]}
        if row["shared_set.type"] == "ACCOUNT_LEVEL_NEGATIVE_KEYWORDS":
            item["level"] = "account"
            account.append(item)
        else:
            shared[sid].append(item)
    attached: dict[str, list[str]] = defaultdict(list)
    for row in g.search(cid, Q.CAMPAIGN_SHARED_SETS):
        sid = str(row["shared_set.id"])
        attached[str(row["campaign.id"])].append(sid)
        camp[str(row["campaign.id"])].extend(shared.get(sid, []))
    return {"campaign": camp, "ad_group": adg, "account": account, "shared": shared,
            "shared_names": shared_names, "attached": attached}


def covering_negative(term: str, campaign_id: str, ad_group_id: str | None, negs: dict[str, Any]) -> dict[str, Any] | None:
    pool = negs["account"] + negs["campaign"].get(str(campaign_id), []) + (negs["ad_group"].get(str(ad_group_id), []) if ad_group_id else [])
    for n in pool:
        if negative_matches(term, n["text"], n["match_type"]):
            return n
    return None


def fetch_terms(g, cid: str, dr, campaign_id: str | None = None, ad_group_id: str | None = None,
                include_pmax: bool = True, errors: dict[str, str] | None = None) -> list[dict[str, Any]]:
    errors = errors if errors is not None else {}
    rows = []
    for row in g.search(cid, Q.search_terms(dr, campaign_id, ad_group_id)):
        rows.append({
            "search_term": row["search_term_view.search_term"],
            "status": row.get("search_term_view.status"),
            "campaign_id": str(row["campaign.id"]), "campaign": row["campaign.name"],
            "channel": row.get("campaign.advertising_channel_type"),
            "ad_group_id": str(row["ad_group.id"]), "ad_group": row["ad_group.name"],
            **m(row),
        })
    if include_pmax and not ad_group_id:
        pmax = safe("pmax_search_terms", lambda: g.search(cid, Q.pmax_search_terms(dr, campaign_id)), errors) or []
        for row in pmax:
            rows.append({
                "search_term": row["campaign_search_term_view.search_term"], "status": None,
                "campaign_id": str(row["campaign.id"]), "campaign": row["campaign.name"],
                "channel": "PERFORMANCE_MAX", "ad_group_id": None, "ad_group": None, **m(row),
            })
    return rows


def ngram_table(rows: list[dict[str, Any]], n_values=(1, 2, 3), skip_stopwords: bool = True) -> list[dict[str, Any]]:
    acc: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        toks = tokens(row["search_term"])
        for n in n_values:
            seen = set()
            for gram in ngrams(toks, n):
                if gram in seen:
                    continue
                seen.add(gram)
                if skip_stopwords and n == 1 and (gram in STOPWORDS or len(gram) < 2):
                    continue
                a = acc.setdefault((n, gram), {"ngram": gram, "n": n, "terms": 0, "cost": 0.0, "clicks": 0,
                                                "impressions": 0, "conversions": 0.0, "conv_value": 0.0})
                a["terms"] += 1
                a["cost"] += row["cost"] or 0
                a["clicks"] += row["clicks"] or 0
                a["impressions"] += row["impressions"] or 0
                a["conversions"] += row["conversions"] or 0
                a["conv_value"] += row["conv_value"] or 0
    out = []
    for a in acc.values():
        a["cost"] = r(a["cost"])
        a["conversions"] = r(a["conversions"])
        a["conv_value"] = r(a["conv_value"])
        a["cpa"] = r(div(a["cost"], a["conversions"]))
        a["roas"] = r(div(a["conv_value"], a["cost"]))
        a["ctr"] = r(div(a["clicks"], a["impressions"], 6) * 100, 2) if a["impressions"] else None
        a["conv_rate"] = r(div(a["conversions"], a["clicks"], 6) * 100, 2) if a["clicks"] else None
        out.append(a)
    return out


@tool(title="Search terms with flags")
def get_search_terms(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    flag: str | None = None,
    include_pmax: bool = True,
    min_cost: float = 0.0,
    waste_threshold: float | None = None,
    promote_min_conversions: float = 2.0,
    limit: int | None = None,
    sort_by: str = "cost",
) -> dict[str, Any]:
    """Search terms (search_term_view + PMax campaign_search_term_view) with flags:

    - zero_conv_spender: cost >= threshold (default 2x target CPA from brief) and 0 conversions
    - irrelevant_ngram: contains a junk word (default uk/ru/en list + brief irrelevant/competitor terms)
      or a 1-/2-gram that spent >= threshold with 0 conversions across all terms
    - covered_by_negative: an existing negative (account/shared/campaign/ad group) already blocks it
    - promote_to_keyword: >= promote_min_conversions, CPA/ROAS at or better than target, not yet a keyword
    - competitor / brand: matches brief competitor_terms / brand_terms

    flag: return only rows with this flag. Summary counts and $ per flag are always included.
    """
    g, cid, info, dr = ctx(customer_id, date_range)
    t = targets(cid)
    errors: dict[str, str] = {}
    rows = fetch_terms(g, cid, dr, campaign_id, ad_group_id, include_pmax, errors)
    total = agg([{"metrics.cost_micros": int((x["cost"] or 0) * 1e6), "metrics.clicks": x["clicks"],
                  "metrics.impressions": x["impressions"], "metrics.conversions": x["conversions"],
                  "metrics.conversions_value": x["conv_value"]} for x in rows])
    threshold, basis = (waste_threshold, "explicit") if waste_threshold else cost_threshold(cid, total["cpa"])
    negs = safe("negatives", lambda: load_negatives(g, cid), errors)

    grams = ngram_table(rows, (1, 2))
    bad_grams = {a["ngram"] for a in grams if a["cost"] >= threshold and not a["conversions"]
                 and not any(b in a["ngram"] for b in t["brand_terms"])}
    vocab = IRRELEVANT_DEFAULT | set(t["irrelevant_terms"])
    target_cpa = t.get("target_cpa") or total["cpa"]
    target_roas = t.get("target_roas")

    flag_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "cost": 0.0})
    out_rows = []
    for row in rows:
        term = row["search_term"] or ""
        toks = tokens(term)
        low = term.lower()
        flags: list[str] = []
        details: dict[str, Any] = {}
        if (row["cost"] or 0) >= threshold and not row["conversions"]:
            flags.append("zero_conv_spender")
        junk = sorted(set(toks) & vocab)
        gram_hits = sorted({gram for n in (1, 2) for gram in ngrams(toks, n)} & bad_grams)
        if junk or gram_hits:
            flags.append("irrelevant_ngram")
            details["ngrams"] = (junk + gram_hits)[:5]
        if any(c in low for c in t["competitor_terms"]):
            flags.append("competitor")
        if any(b in low for b in t["brand_terms"]):
            flags.append("brand")
        if negs:
            cov = covering_negative(term, row["campaign_id"], row["ad_group_id"], negs)
            if cov or row.get("status") in ("EXCLUDED", "ADDED_EXCLUDED"):
                flags.append("covered_by_negative")
                if cov:
                    details["negative"] = cov
        good_eff = (
            (target_cpa and row["cpa"] is not None and row["cpa"] <= float(target_cpa))
            or (target_roas and row["roas"] is not None and row["roas"] >= float(target_roas))
        )
        already_keyword = row.get("status") in ("ADDED", "ADDED_EXCLUDED")
        if (row["conversions"] or 0) >= promote_min_conversions and good_eff and not already_keyword \
                and "covered_by_negative" not in flags:
            flags.append("promote_to_keyword")
        for f in flags:
            flag_stats[f]["count"] += 1
            flag_stats[f]["cost"] = r(flag_stats[f]["cost"] + (row["cost"] or 0))
        row["flags"] = flags
        if details:
            row["flag_details"] = details
        if (row["cost"] or 0) >= min_cost and (not flag or flag in flags):
            out_rows.append(row)
    out = compact(out_rows, limit=limit_default(limit), sort_by=sort_by,
                  totals_fields=("cost", "clicks", "conversions", "conv_value"))
    out.update(account_header(cid, info, dr))
    out["flag_summary"] = dict(flag_stats)
    out["waste_threshold"] = {"amount": r(threshold), "basis": basis}
    out["all_terms_totals"] = total
    if errors:
        out["section_errors"] = errors
    return out


@tool(title="N-gram analysis of search terms")
def ngram_analysis(
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    n: list[int] | None = None,
    campaign_id: str | None = None,
    include_pmax: bool = True,
    min_cost: float = 0.0,
    zero_conv_only: bool = False,
    limit: int | None = None,
    sort_by: str = "cost",
) -> dict[str, Any]:
    """Aggregate search terms into 1/2/3-grams (Unicode-aware: Cyrillic, apostrophes) with cost, clicks,
    conversions, CPA, ROAS, conv rate. Each n-gram is counted once per search term.
    Use zero_conv_only=True + sort_by="cost" to find negative-keyword candidates."""
    g, cid, info, dr = ctx(customer_id, date_range)
    errors: dict[str, str] = {}
    rows = fetch_terms(g, cid, dr, campaign_id, None, include_pmax, errors)
    n_values = tuple(sorted(set(n or [1, 2, 3])))
    table = [a for a in ngram_table(rows, n_values)
             if a["cost"] >= min_cost and (not zero_conv_only or not a["conversions"])]
    lim = limit_default(limit)
    out: dict[str, Any] = account_header(cid, info, dr)
    out["search_terms_analyzed"] = len(rows)
    for k in n_values:
        out[f"{k}_grams"] = compact([a for a in table if a["n"] == k], limit=lim, sort_by=sort_by)
    if errors:
        out["section_errors"] = errors
    return out
