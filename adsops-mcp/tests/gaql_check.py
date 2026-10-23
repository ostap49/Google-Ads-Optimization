"""Offline GAQL field validation against the v26 GoogleAdsRow proto descriptors."""
import re

from google.ads.googleads.v26.services.types.google_ads_service import GoogleAdsRow

_ROW = GoogleAdsRow.pb().DESCRIPTOR
FIELD_RE = re.compile(r"\b([a-z_]+(?:\.[a-z_0-9]+)+)\b")


def fields_in(query: str) -> set[str]:
    body = re.sub(r"'[^']*'", "''", query)  # drop string literals
    body = re.split(r"\bPARAMETERS\b", body)[0]
    return set(FIELD_RE.findall(body))


def invalid_fields(query: str) -> list[str]:
    bad = []
    for path in sorted(fields_in(query)):
        desc = _ROW
        for part in path.split("."):
            fd = (desc.fields_by_name.get(part) or desc.fields_by_name.get(part + "_")) if desc else None
            if fd is None:
                bad.append(path)
                break
            desc = fd.message_type
    return bad


def resource_of(query: str) -> str:
    return re.search(r"\bFROM\s+([a-z_]+)", query, re.I).group(1)
