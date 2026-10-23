"""Read-only GAQL escape hatch."""
from __future__ import annotations

import re
from typing import Any

from core.fmt import compact
from tools._registry import tool
from tools.common import account_header, ctx, money_row

# GoogleAdsService.Search cannot mutate anything; we still refuse multi-statement input.
_FORBIDDEN = re.compile(r";\s*\S")
MAX_ROWS = 10000


def validate_gaql(query: str, max_rows: int) -> str:
    q = " ".join(query.strip().rstrip(";").split())
    if not re.match(r"^SELECT\s", q, re.I):
        raise ValueError("Only SELECT queries are allowed.")
    if " FROM " not in q.upper():
        raise ValueError("Query must contain FROM <resource>.")
    if _FORBIDDEN.search(q):
        raise ValueError("Only a single SELECT statement is allowed.")
    if not re.search(r"\bLIMIT\s+\d+", q, re.I):
        m = re.search(r"\bPARAMETERS\b", q, re.I)
        clause = f" LIMIT {max_rows}"
        q = q[: m.start()].rstrip() + clause + " " + q[m.start():] if m else q + clause
    return q


@tool(title="Run read-only GAQL")
def run_gaql(customer_id: str, query: str, limit: int = 50, sort_by: str | None = None,
             max_rows: int = 2000) -> dict[str, Any]:
    """Run any read-only GAQL SELECT against an account (live data, SearchStream).

    A LIMIT (max_rows, default 2000) is added if missing. Every *_micros field gets a currency-unit
    sibling (metrics.cost_micros -> metrics.cost). Returns top `limit` rows, optionally sorted by `sort_by`
    (a field name as it appears in the rows, e.g. "metrics.cost").
    """
    g, cid, info, _ = ctx(customer_id)
    q = validate_gaql(query, min(int(max_rows), MAX_ROWS))
    rows = [money_row(r) for r in g.search(cid, q)]
    out = compact(rows, limit=limit, sort_by=sort_by)
    out.update(account_header(cid, info))
    out["query"] = q
    return out
