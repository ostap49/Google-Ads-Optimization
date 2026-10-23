---
title: Conversion tracking audit
description: Verify the conversion setup that Smart Bidding optimizes for.
---
# Conversion tracking audit

## Steps
1. `get_account_brief` (business_type decides which categories are "real" conversions).
2. `audit_conversion_tracking(lookback_days=90)` — score, findings, actions, tracking settings.
3. `get_account_overview(date_range="LAST_30_DAYS")` — compare `conversions` vs `all_conversions`
   (big gap = many secondary actions; fine) and conv value vs expected revenue if the user knows it.
4. `anomaly_scan` — zero-conversion days align with tracking breaks?
5. `run_gaql` for a per-action daily series if a break date is unclear:
   `SELECT segments.date, segments.conversion_action_name, metrics.all_conversions FROM customer WHERE segments.date DURING LAST_30_DAYS`.

## Decision thresholds
- E-commerce: exactly one PRIMARY purchase action (with dynamic value, counting = every). Leads: primary lead
  actions counting = one. Micro-conversions (page view, add to cart, engagement) → secondary.
- Primary action with no conversion in 7 days while clicks continue → critical (tag broken).
- Duplicate primary purchases (GA4 + Ads tag) → keep the Ads tag (with enhanced conversions) primary.
- Enhanced conversions OFF → recommend enabling; consent mode cannot be read via the API — ask the user to check
  Google tag diagnostics (EU/UK traffic) and say so explicitly.
- Fixes to conversion actions are done in the Google Ads UI / GTM (adsops has no conversion-action write tools by
  design) — give click-by-click steps.

## Output
Score /100, findings table (severity · issue · fix), a short "impact on bidding" paragraph, and an action list for
the user / developer.
