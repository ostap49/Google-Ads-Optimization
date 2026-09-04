"""Product bucketing: join the Merchant Center feed with Ads performance.

Labelizer-style buckets over the full feed (so true zombies — products
Google never shows — are visible, not just products with traffic):

    hero     — converts, ROAS >= target
    sidekick — converts, ROAS below target
    villain  — spent >= villain_cost with zero conversions
    zombie   — enabled product with almost no impressions (< zombie_impr)
    watch    — has some traffic but not enough signal yet

Read-only. The output includes a ready supplemental-feed TSV
(id + custom_label_4) to push buckets into Merchant Center.
"""

import logging
from typing import Any, Dict, List

from .audits import _search, _date_clause

logger = logging.getLogger(__name__)

MAX_TABLE_ROWS = 500


def product_performance(client, customer_id: str, days: int) -> Dict[str, Dict]:
    """Per-product Ads metrics (Shopping + PMax), keyed by lowercased item id."""
    query = f"""
        SELECT segments.product_item_id,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM shopping_performance_view
        WHERE {_date_clause(days)}
          AND campaign.status = 'ENABLED'
    """
    perf: Dict[str, Dict] = {}
    for batch in _search(client, customer_id, query):
        for r in batch.results:
            key = r.segments.product_item_id.lower()
            p = perf.setdefault(
                key,
                {"impressions": 0, "clicks": 0, "cost": 0.0,
                 "conversions": 0.0, "value": 0.0},
            )
            p["impressions"] += r.metrics.impressions
            p["clicks"] += r.metrics.clicks
            p["cost"] += r.metrics.cost_micros / 1_000_000
            p["conversions"] += r.metrics.conversions
            p["value"] += r.metrics.conversions_value
    logger.info(
        "Ads performance: %d products with traffic for %s", len(perf), customer_id
    )
    return perf


def bucketize(
    feed: List[Dict[str, Any]],
    statuses: Dict[str, Dict[str, Any]],
    perf: Dict[str, Dict],
    target_roas: float = 3.0,
    villain_cost: float = 10.0,
    zombie_impr: int = 10,
) -> Dict[str, Any]:
    """Assign a bucket to every feed product and build summaries."""
    products = []
    matched_ids = set()

    for item in feed:
        oid = item["offer_id"]
        key = oid.lower()
        matched_ids.add(key)
        p = perf.get(key, {})
        cost = p.get("cost", 0.0)
        conv = p.get("conversions", 0.0)
        value = p.get("value", 0.0)
        impressions = p.get("impressions", 0)
        roas = value / cost if cost > 0 else 0.0

        st = statuses.get(key, {})
        status = st.get("status", "unknown")
        disapproved = status == "disapproved"

        if conv > 0 and roas >= target_roas:
            bucket = "hero"
        elif conv > 0:
            bucket = "sidekick"
        elif cost >= villain_cost:
            bucket = "villain"
        elif impressions < zombie_impr:
            bucket = "zombie"
        else:
            bucket = "watch"

        products.append(
            {
                "offer_id": oid,
                "title": item["title"],
                "brand": item["brand"],
                "product_type": item["product_type"],
                "price": item["price"],
                "availability": item["availability"],
                "status": status,
                "disapproved": disapproved,
                "issues": [i["description"] for i in st.get("issues", [])
                           if i.get("servability") == "disapproved"][:3],
                "impressions": impressions,
                "clicks": p.get("clicks", 0),
                "cost": round(cost, 2),
                "conversions": round(conv, 1),
                "value": round(value, 2),
                "roas": round(roas, 2),
                "bucket": bucket,
                "current_label4": item["custom_labels"].get(4, ""),
            }
        )

    # Products with Ads traffic that are no longer in the feed
    orphans = len([k for k in perf if k not in matched_ids])

    buckets: Dict[str, Dict[str, Any]] = {}
    for pr in products:
        b = buckets.setdefault(
            pr["bucket"],
            {"count": 0, "cost": 0.0, "value": 0.0, "conversions": 0.0},
        )
        b["count"] += 1
        b["cost"] += pr["cost"]
        b["value"] += pr["value"]
        b["conversions"] += pr["conversions"]
    for b in buckets.values():
        b["cost"] = round(b["cost"], 2)
        b["value"] = round(b["value"], 2)
        b["conversions"] = round(b["conversions"], 1)

    disapproved_n = sum(1 for pr in products if pr["disapproved"])
    issue_counts: Dict[str, int] = {}
    for key, st in statuses.items():
        for i in st.get("issues", []):
            if i.get("servability") == "disapproved":
                issue_counts[i["description"]] = issue_counts.get(i["description"], 0) + 1
    top_issues = sorted(issue_counts.items(), key=lambda x: -x[1])[:5]

    # Supplemental feed TSV: pushes buckets into custom_label_4
    tsv_lines = ["id\tcustom_label_4"] + [
        f"{pr['offer_id']}\t{pr['bucket']}" for pr in products
    ]

    # Table: worst money first — villains by cost, then zombies, then rest
    order = {"villain": 0, "sidekick": 1, "hero": 2, "watch": 3, "zombie": 4}
    products.sort(key=lambda pr: (order.get(pr["bucket"], 9), -pr["cost"]))

    return {
        "total_products": len(feed),
        "with_traffic": len(matched_ids & set(perf.keys())),
        "orphan_perf_products": orphans,
        "disapproved": disapproved_n,
        "top_issues": [{"issue": i, "count": c} for i, c in top_issues],
        "buckets": buckets,
        "thresholds": {
            "target_roas": target_roas,
            "villain_cost": villain_cost,
            "zombie_impr": zombie_impr,
        },
        "products": products[:MAX_TABLE_ROWS],
        "table_truncated": len(products) > MAX_TABLE_ROWS,
        "feed_tsv": "\n".join(tsv_lines),
    }
