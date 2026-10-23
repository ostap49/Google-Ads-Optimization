"""Synthetic GoogleAdsRow-shaped data for any GAQL query, driven by the v26 proto descriptors."""
import random
import re
from datetime import date, timedelta

from google.ads.googleads.v26.services.types.google_ads_service import GoogleAdsRow

from tests.gaql_check import invalid_fields

_ROW = GoogleAdsRow.pb().DESCRIPTOR
ENUM_PREF = {"status": "ENABLED", "advertising_channel_type": "SEARCH", "match_type": "PHRASE",
             "device": "MOBILE", "ad_network_type": "SEARCH", "type": None}


def _fd(path):
    desc, fd = _ROW, None
    for part in path.split("."):
        fd = desc.fields_by_name.get(part) or desc.fields_by_name.get(part + "_")
        if fd is None:
            return None
        desc = fd.message_type
    return fd


def _dates(query):
    m = re.search(r"segments\.date BETWEEN '(\d{4}-\d\d-\d\d)' AND '(\d{4}-\d\d-\d\d)'", query)
    if not m:
        return None
    a, b = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def value_for(path, i, rnd, day=None):
    leaf = path.split(".")[-1]
    if path == "segments.date":
        return day
    fd = _fd(path)
    if fd is None:
        return None
    if getattr(fd, "is_repeated", False):
        return []
    if fd.enum_type is not None:
        pref = ENUM_PREF.get(leaf)
        names = [v.name for v in fd.enum_type.values if v.name not in ("UNSPECIFIED", "UNKNOWN")]
        return pref if pref in names else (names[i % len(names)] if names else "UNKNOWN")
    if fd.message_type is not None:
        return None
    if path.startswith("metrics."):
        if leaf.endswith("impression_share"):
            return round(rnd.uniform(0.1, 0.95), 3)
        if leaf == "cost_micros":
            return rnd.randint(0, 400) * 1_000_000
        if leaf == "impressions":
            return rnd.randint(100, 5000)
        if leaf == "clicks":
            return rnd.randint(1, 100)
        if "conversions" in leaf:
            return float(rnd.choice([0, 0, 1, 2, 5]))
        return rnd.uniform(0, 10)
    if leaf == "hour":
        return i % 24
    if fd.type in (3, 4, 5, 13, 17, 18):  # ints
        return 1000 + i if (leaf == "id" or leaf.endswith("_id")) else (rnd.randint(1, 10) * 1_000_000 if leaf.endswith("micros") else (i % 10) + 1)
    if fd.type in (1, 2):
        return round(rnd.uniform(0.5, 5), 2)
    if fd.type == 8:
        return bool(i % 2)
    if leaf == "resource_name" or leaf in ("campaign", "ad_group", "shared_set", "conversion_action"):
        return f"customers/1234567890/{leaf}s/{1000 + i}"
    if leaf in ("search_term", "text"):
        return rnd.choice(["купити диван київ", "диван безкоштовно", "buy sofa", "sofa jobs", "диван кутовий ціна"])
    return f"{leaf}-{i}"


def rows_for(query, n=3, seed=7):
    bad = invalid_fields(query)
    assert not bad, f"Invalid GAQL fields {bad} in: {query}"
    from gads.client import ensure_where_fields_selected, select_fields
    fields = select_fields(ensure_where_fields_selected(query))
    rnd = random.Random(seed)
    days = _dates(query) if "segments.date" in fields else None
    out = []
    for i in range(n):
        for day in (days or [None]):
            out.append({f: value_for(f, i, rnd, day) for f in fields})
    return out
