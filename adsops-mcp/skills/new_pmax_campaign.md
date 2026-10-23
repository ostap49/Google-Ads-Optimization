---
title: New Performance Max campaign
description: Feed / assets / signals checklist and a paused PMax proposal.
---
# New Performance Max campaign

Context from the user: {context}

## Steps
1. `get_account_brief`; `audit_conversion_tracking` — do NOT launch PMax on broken or micro-conversion
   tracking (fix primary goals first).
2. Existing coverage: `pmax_insights`, `get_shopping_products` (for retail) — avoid overlapping asset groups /
   product sets with existing PMax.
3. Collect assets: final URL, business name (≤ 25), 3–15 headlines (≤ 30; one ≤ 15), 1–5 long headlines (≤ 90),
   2–5 descriptions (≤ 90; one ≤ 60), images: landscape 1.91:1 (≥ 1200×628), square 1:1 (≥ 1200×1200),
   portrait 4:5, logo 1:1 (+ 4:1 landscape logo), YouTube video IDs (else Google auto-generates video).
4. Audience signals: search themes (top converting search terms / keyword research themes, up to 25),
   customer lists / remarketing audience IDs if the user provides them.
5. Retail: `merchant_center_id` (+ `feed_label`) → all-products listing group; split asset groups by category /
   margin later.
6. Asset automation: recommend opting out of `TEXT_ASSET_AUTOMATION` and image enhancement for brand-sensitive
   clients (`asset_automation_opt_out`).
7. Brand: add brand terms to the account-level negative list only if a separate brand Search campaign exists
   and the user agrees (`propose_add_negative_keywords(level="account")`).
8. `propose_create_pmax_campaign(...)` → PAUSED.

## Decision thresholds
- Bidding: MAXIMIZE_CONVERSION_VALUE (e-commerce with values) — add tROAS only after ~30 conv / 30 days;
  MAXIMIZE_CONVERSIONS for lead gen. Start tROAS ≈ 0.8× the account's actual ROAS, not the wish target.
- Budget: ≥ 3× target CPA per day (≥ 5× preferred) so the algorithm can learn.

## Output
Checklist (✅/❌) of assets, tracking and signals, then the proposal. Note that the campaign is PAUSED.
