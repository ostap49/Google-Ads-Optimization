---
title: New Search campaign
description: Research → structure → copy → paused campaign proposal for a product / service.
---
# New Search campaign

Context from the user: {context}

## Steps
1. `get_account_brief` (geo, languages, brand/competitor rules, target CPA, budget).
2. Clarify: product/service, landing page URL, geo, languages (default uk + ru for Ukraine), daily budget, goal.
3. `keyword_research(seed_keywords=[...], url=<landing page>, geo=..., languages=...)` → themes.
4. `get_search_terms(flag="promote_to_keyword")` and existing keywords
   (`get_adgroup_and_keyword_performance`) — avoid cannibalizing existing campaigns.
5. `forecast(keywords=[...], max_cpc=<median top-of-page high bid>)` for budget sanity.
6. Build structure: 1 ad group per theme (5–20 tightly related keywords), PHRASE + EXACT; brand separate.
7. Write one RSA per ad group: 15 headlines (≤ 30 chars), 4 descriptions (≤ 90), paths ≤ 15. Include the main
   keyword in 3–5 headlines, USPs, price/delivery/guarantee, CTA; Ukrainian copy for uk, Russian for ru ad groups
   only if the brief asks for separate language groups. No '!' in headlines. Pin only if legally required.
8. Negatives: account/shared junk list + competitor terms if the brief forbids them.
9. `propose_create_search_campaign(...)` → PAUSED. Then sitelinks/callouts/snippets via
   `propose_create_sitelinks` / `propose_create_callouts` / `propose_create_snippets` on the new campaign.

## Decision thresholds
- Bidding: < 30 conv/month expected → MAXIMIZE_CONVERSIONS without target (or MANUAL_CPC for tight control);
  move to tCPA after ~30 conversions. E-commerce with values → MAXIMIZE_CONVERSION_VALUE later.
- Budget: ≥ 10× target CPA per day ideally; if not possible, fewer ad groups.
- Location: PRESENCE only. Search partners and Display expansion OFF.

## Output
Structure table (ad group · keywords · est. volume · top-of-page bid), RSA copy per ad group with character
counts, forecast, then the proposal(s). Remind the user the campaign is PAUSED and how to enable it
(`propose_set_status`).
