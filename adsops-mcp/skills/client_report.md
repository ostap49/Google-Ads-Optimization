---
title: Client report
description: Client-ready performance report (Markdown + HTML + charts) for a period.
---
# Client report

Context from the user: {context}

## Steps
1. `get_account_brief` (targets, KPI, language preference).
2. `generate_report(customer_id, period=<e.g. LAST_MONTH>, lang="{lang}", xlsx=<if asked>)` — produces Markdown + HTML
   (charts) + optional XLSX and returns the file paths and a data summary.
3. Optionally enrich: `get_campaign_performance` (period), `analyze_wasted_spend` (for "next steps"),
   `get_decision_log` (context for actions).
4. Review the generated summary: rewrite "Wins / Losses" and "Next steps" in plain client language (no jargon
   without explanation, no internal guardrail talk).

## Rules
- Lead with business outcomes (sales/leads, revenue, CPA/ROAS vs target), then efficiency, then actions taken.
- Explain changes with causes (seasonality from the brief, changes from the audit log), never blame the platform
  without evidence.
- Numbers in the account currency with thousands separators; period and comparison period always stated.
- Ukrainian (`lang=uk`) or English (`lang=en`) only.

## Output
Link the generated files (paths from `generate_report`) and paste the executive summary (5–7 sentences) into the chat.
