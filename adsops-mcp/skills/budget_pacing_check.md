---
title: Budget pacing check
description: Month-to-date spend vs plan for one account or the whole MCC, with budget proposals.
---
# Budget pacing check

## Steps
1. For one account: `get_account_brief` (monthly_budget, campaign_budgets) → `budget_pacing(customer_id=...)`.
   For all accounts: `budget_pacing(customer_id="all")`, then drill into the off-pace ones.
2. For off-pace accounts: `get_campaign_performance(date_range="THIS_MONTH")` to see where spend should move.

## Decision thresholds
- On track: projected EOM within ±10% of target.
- Overpacing > 10%: cut daily budgets on campaigns with worst CPA/ROAS first, total cut ≈ current daily run rate −
  `recommended_daily_spend`.
- Underpacing > 10%: raise budgets only on campaigns limited by budget (lost IS budget ≥ 10%) with CPA ≤ target
  (ROAS ≥ target); otherwise report "under-delivery is demand/bid-limited, not budget-limited".
- Each budget change ≤ max_budget_change_pct (default 30%) — split bigger corrections over several days.
- Shared budgets affect all their campaigns — say so.

## Output
Table: account/campaign · MTD · projected · target · pacing % · status · recommended daily. Then proposals
(`propose_update_campaign_budget`), one per campaign, for approval.
