---
title: Search term mining
description: Turn search terms into new keywords, negatives and ad-copy insights.
---
# Search term mining

## Steps
1. `get_account_brief` (brand / competitor / irrelevant terms, rules).
2. `get_search_terms(date_range="LAST_30_DAYS", limit=100)` — read `flag_summary`.
3. `ngram_analysis(date_range="LAST_90_DAYS", n=[1,2,3])` — themes that convert vs waste.
4. Promote: `get_search_terms(flag="promote_to_keyword")`.
5. Negate: `get_search_terms(flag="zero_conv_spender")`, `flag="irrelevant_ngram"`, `flag="competitor"` (if rules forbid).
6. Already covered by negatives (`flag="covered_by_negative"`) — usually old data; ignore unless spend is recent.

## Decision thresholds
- Promote: ≥ 2 conversions, CPA ≤ target (or ROAS ≥ target), not yet a keyword → EXACT in the best-matching ad
  group (`propose_add_keywords`), plus PHRASE if the 2-gram converts across several terms.
- Negate EXACT: single term, cost ≥ 2× target CPA, 0 conv. Negate PHRASE: n-gram with cost ≥ 1× target CPA and
  0 conv across ≥ 3 terms, clearly irrelevant intent. Put generic junk on a shared list; campaign-specific
  mismatches (e.g. wrong product line) on that campaign only.
- PMax terms: negatives only via the account-level list.
- Copy insights: converting modifiers (ціна, купити, недорого, доставка, city names) → suggest headline variants.

## Output
Three tables — Promote / Negate / Insights — with cost, conv, CPA per row, then grouped proposals.
