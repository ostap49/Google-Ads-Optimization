---
title: Wasted spend cleanup
description: Find zero-return spend across search terms, keywords, placements, products, devices, geo, hours and fix it.
---
# Wasted spend cleanup

## Steps
1. `get_account_brief` (target CPA / ROAS, brand terms — never negate brand).
2. `analyze_wasted_spend(date_range="LAST_30_DAYS")` — ranked list with proposed actions.
3. `ngram_analysis(zero_conv_only=True, min_cost=<0.5× target CPA>)` — junk n-grams to negate as PHRASE.
4. `get_search_terms(flag="zero_conv_spender")` and `get_search_terms(flag="irrelevant_ngram")`.
5. For keywords flagged: `get_adgroup_and_keyword_performance(campaign_id=...)` — check QS and whether the
   waste comes from one bad search term (negate it) vs the keyword itself (pause / bid down).

## Decision thresholds
- Search term: cost ≥ 2× target CPA, 0 conv → negative EXACT. Junk n-gram (jobs, free, DIY, wrong product) with
  cost ≥ 1× target CPA across terms → negative PHRASE on a shared list.
- Keyword: cost ≥ 2× target CPA, 0 conv, ≥ 50 clicks → pause; fewer clicks → bid −30%.
- Placement (Display/DG/Video): 0 conv and cost ≥ 1× target CPA, or obvious junk (kids games, apps) → account-level exclusion.
- Device / hour / location: 0 conv with cost ≥ 2× target CPA, or CPA > 2.5× target → bid modifier −30%…−50%
  (respect max_bid_change_pct; full exclusion only if clearly irrelevant).
- Product: spend ≥ 2× target CPA, 0 conv → custom label "zombie"/exclude (manual in Merchant Center; explain).
- Never negate brand terms or terms with assisted conversions the user cares about — ask when unsure.

## Proposals (group by type, one approval each)
1. `propose_add_negative_keywords(level="shared_list"|"campaign", ...)` — show blocked cost (impact).
2. `propose_add_exclusions(exclusion_type="placement", level="account", ...)`.
3. `propose_set_status(entity_type="keyword", status="PAUSED", ...)` / `propose_update_keyword_bids`.
4. `propose_add_exclusions(exclusion_type="device"|"ad_schedule"|"location", ...)`.

## Output
Table: item · dimension · cost · clicks · conv · action. Then total recoverable spend per month
(sum of zero-conv waste in non-overlapping dimensions — search terms + placements + products), and the proposals.
