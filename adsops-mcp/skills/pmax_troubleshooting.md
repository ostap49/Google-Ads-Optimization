---
title: PMax troubleshooting
description: Diagnose a Performance Max campaign that under-delivers or over-spends.
---
# PMax troubleshooting

Context from the user: {context}

## Steps
1. `get_account_brief`, then `get_campaign_performance(channel_types=["PERFORMANCE_MAX"], date_range="LAST_30_DAYS")`
   and the same with `segment="date"` to locate when it broke.
2. `get_change_history(days=30, resource_types=["CAMPAIGN","CAMPAIGN_BUDGET","ASSET_GROUP","ASSET_GROUP_ASSET"])`.
3. `pmax_insights(campaign_id=...)`: asset groups (ad strength), asset coverage gaps, asset metrics, search term
   insight categories, product performance, placements, channel breakdown.
4. `audit_conversion_tracking` — PMax follows primary goals; micro-conversions or a broken tag explain most collapses.
5. Retail: `get_shopping_products(channel="PERFORMANCE_MAX")` — zero-impression / not-eligible products, spend concentration.
6. `get_search_terms(campaign_id=..., include_pmax=True)` and `ngram_analysis(campaign_id=...)` — brand leakage, junk.
7. `budget_pacing` (limited by budget?) and `get_recommendations(campaign_id=...)`.

## Diagnosis rules
- Spend dropped after a tROAS/tCPA change > 20% → target too aggressive: revert in ≤ 20% steps (`propose_update_bid_strategy`).
- Conversions dropped but clicks stable → tracking (check last conversion date per action).
- Ad strength POOR / missing minimum assets → fill assets before touching bids.
- > 40% of cost on CONTENT/YOUTUBE/DISCOVER with low conversion share → weak feed/creatives or audience signal; add search themes, exclude junk placements (account level).
- Brand terms > 30% of PMax conversions → brand cannibalization; propose account-level brand negatives only with a brand Search campaign in place.
- Products: top 10% of products taking > 70% spend with ROAS < target → split asset groups / custom labels.

## Output
Root cause (ranked, with evidence), then fixes as proposals, then "what to watch for 14 days".
