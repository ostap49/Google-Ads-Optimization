---
title: Weekly account review
description: 7-day health check vs targets and previous week; anomalies, waste, pacing, top 5 actions.
---
# Weekly review

## Steps
1. `get_account_brief` → note targets, budget, rules.
2. `get_account_overview(date_range="LAST_7_DAYS")` — KPIs vs previous 7 days and vs targets.
3. `anomaly_scan` — flag only statistically meaningful swings it returns.
4. `get_campaign_performance(date_range="LAST_7_DAYS", sort_by="cost")` — winners / losers by CPA or ROAS
   (whichever `kpi` the brief uses).
5. `budget_pacing` — month-to-date vs monthly budget, campaigns limited by budget.
6. `analyze_wasted_spend(date_range="LAST_14_DAYS", limit=15)` — top waste items.
7. `get_search_terms(date_range="LAST_7_DAYS", flag="promote_to_keyword", limit=10)` — growth opportunities.
8. `get_change_history(days=7)` and `get_decision_log(days=14)` — what changed and why (explains swings).
9. If PMax spends > 30% of budget: `pmax_insights(date_range="LAST_14_DAYS")`.

## Decision thresholds
- Campaign CPA > 1.3× target (or ROAS < 0.75× target) for 7d with ≥ 3 conv → "losing", investigate.
- CPA ≤ 0.8× target and lost IS (budget) ≥ 10% → candidate for +15–30% budget (within guardrails).
- Pacing outside ±10% → adjust daily budgets toward `recommended_daily_spend`.
- Zero-conversion day flagged by `anomaly_scan` → check `audit_conversion_tracking` before touching bids.

## Output
1. **Summary** (3 bullets): spend, conv, CPA/ROAS vs target and vs last week (Δ%).
2. **What changed & why** — anomalies + change history.
3. **Campaigns** table: campaign · cost · conv · CPA/ROAS · Δ% · status (✅ on target / ⚠ watch / ❌ off).
4. **Top 5 actions** ranked by expected impact (currency), each with the `propose_*` tool you would use.
5. Ask which actions to prepare as proposals.
