# adsops-mcp

Self-hosted, single-user MCP server that lets Claude (Claude Code + Claude Desktop) analyse and manage Google Ads
accounts under your MCC with **live API data** (no cache except 1-hour metadata), full read access, and
**write access gated by a two-step approval flow**.

- Python 3.11+, official `google-ads` library **34.0.0 → Google Ads API v26** (latest at build time; v23–v25 also
  ship in the package — set `GOOGLE_ADS_API_VERSION` to switch), MCP Python SDK **2.3** (`MCPServer`, the renamed
  FastMCP; `mcp` 1.x also works).
- stdio by default; optional streamable HTTP with bearer token.
- SQLite (`data/adsops.db`): aliases, change proposals, audit log, snapshots, briefs, decisions, alert de-dup.

```
server.py            MCP entry (stdio / http)          auth.py      OAuth desktop flow -> refresh token
gads/client.py       client factory, SearchStream, retry/backoff, partial failure, request-id, 1h cache
gads/queries/*.py    GAQL builders                      tools/*.py   read tools + propose_* wrappers
safety/              guardrails, proposal engine, change specs (one per write kind), apply/rollback tools
memory/              account brief + decision log       reports/     generate_report (md/html/xlsx + charts)
skills/*.md          playbooks, exposed as MCP prompts  monitor.py   read-only cron monitor -> Telegram
scripts/smoke.py     stage-by-stage read-only checks against your real MCC
tests/               70 offline tests (mocked GoogleAdsService, v26 proto validation) + integration test
```

## 1. Setup

```bash
cd adsops-mcp
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp config.yaml.example config.yaml                     # guardrails — edit per account
```

### Developer token
Google Ads UI on your **MCC** → Admin → **API Center**. A new token is test-only (works with test accounts only);
apply for **Basic** (15,000 operations/day) or **Standard** (no daily operation cap) access to use it on client
accounts. That access level, not this server, decides how many requests you can make: adsops itself adds no limits.

### OAuth client + refresh token
1. Google Cloud console → new/existing project → *APIs & Services* → enable **Google Ads API**.
2. *OAuth consent screen*: User type External (or Internal on Workspace). Add your Google account as a test user,
   then **Publish app → In production**. While the app is in *Testing*, refresh tokens expire after 7 days.
3. *Credentials* → Create credentials → OAuth client ID → **Desktop app**. Put the client ID/secret in `.env`
   (`GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`), plus `GOOGLE_ADS_DEVELOPER_TOKEN` and
   `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (your MCC, digits only).
4. `python auth.py` → browser consent with the Google user that has access to the MCC → writes `google-ads.yaml`
   (chmod 600, gitignored). `python auth.py --no-browser` prints the URL instead.

`google-ads.yaml` can also be filled by hand from `google-ads.yaml.example`.

### Verify, stage by stage (read-only)
```bash
python scripts/smoke.py 1                     # list_accounts + run_gaql
python scripts/smoke.py 2 --account 123-456-7890   # all read tools on one account
python scripts/smoke.py 3 --account stryi     # validate-only budget proposal, then rejected
python scripts/smoke.py 4 --account stryi     # validate-only write proposals, then rejected
python scripts/smoke.py 5 / 6 / 7             # brief, playbooks, report
```
Nothing in `smoke.py` applies a change: proposals only run `validate_only=True` and are rejected afterwards.

## 2. Connect Claude

**Claude Code**
```bash
claude mcp add adsops --scope user -- /abs/path/adsops-mcp/.venv/bin/python /abs/path/adsops-mcp/server.py
# Windows: claude mcp add adsops --scope user -- C:\path\adsops-mcp\.venv\Scripts\python.exe C:\path\adsops-mcp\server.py
```
Playbooks appear as slash commands: `/mcp__adsops__weekly_review`. Do **not** add `apply_proposal` to your
allowed tools: leaving it unapproved means Claude Code asks you before every apply, on top of the server's own checks.

**Claude Desktop**: `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows:
`%APPDATA%\Claude\`):
```json
{
  "mcpServers": {
    "adsops": {
      "command": "/abs/path/adsops-mcp/.venv/bin/python",
      "args": ["/abs/path/adsops-mcp/server.py"]
    }
  }
}
```

**HTTP mode** (for remote use later)
```bash
# .env: ADSOPS_HTTP_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
python server.py --transport http --port 8765                       # http://127.0.0.1:8765/mcp
python server.py --transport http --host 0.0.0.0 --allowed-host ads.example.com   # behind a TLS tunnel/proxy
claude mcp add --transport http adsops http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```
Every request needs `Authorization: Bearer <token>`. DNS-rebinding protection is on, so list public hostnames with
`--allowed-host`. Always put TLS in front (Cloudflare Tunnel, Caddy…). Claude.ai custom connectors authenticate with
OAuth rather than a static bearer header, so exposing adsops to claude.ai will need an OAuth front (e.g. a reverse
proxy with OAuth) in front of this endpoint. The bearer mode covers Claude Code and other header-capable clients today.

## 3. How writes work (safety layer)

1. `propose_*` reads the current state, checks guardrails, stores a **ChangeProposal** (diff *before → after*,
   estimated impact, warnings, reason) and runs the mutate with **`validate_only=True`**.
2. Claude shows you the proposal and waits for your explicit **yes**.
3. `apply_proposal(proposal_id, user_confirmed=True)`:
   - re-checks guardrails, refuses proposals older than `proposal_ttl_hours`;
   - detects drift (values changed in the UI since proposing → `stale`, re-propose or `force=True`);
   - if the client supports MCP elicitation, **you get an approval dialog from the server itself**
     (`ADSOPS_REQUIRE_ELICITATION=true` makes it mandatory);
   - in dry-run mode (`ADSOPS_DRY_RUN=true` or `--dry-run`) it only validates;
   - writes the **audit log** (timestamp, account, resource, old/new values, Google `request_id`) and a snapshot.
4. `rollback_proposal(id)` creates a *new* proposal that restores previous values (bids, budgets, statuses,
   strategies, device/schedule modifiers) or removes what adsops created (negatives, keywords, asset links). Created
   ads and campaigns are **paused, never removed**. There is no tool that removes campaigns, ad groups or ads.

Guardrails (`config.yaml`, per account ID or alias): `max_daily_budget`, `max_budget_change_pct` (30),
`max_bid_change_pct` (30), `max_target_change_pct` (20), `forbidden_actions`, `protected_campaign_ids`,
`max_operations_per_proposal`, `proposal_ttl_hours`. New campaigns are always created **PAUSED**.

## 4. Tools

| Area | Tools |
|---|---|
| Accounts | `list_accounts` (walks CustomerClient), `set_account_alias`, `run_gaql` (SELECT-only, auto LIMIT) |
| Performance | `get_account_overview`, `get_campaign_performance` (segments: date/device/network/day_of_week/hour, PoP deltas), `get_adgroup_and_keyword_performance` (QS + components) |
| Search terms | `get_search_terms` (zero-conv spenders, junk n-grams, covered by negatives, promote candidates, PMax terms), `ngram_analysis` |
| PMax / Shopping | `pmax_insights` (asset groups, asset coverage + metrics, search-term insights, products, listing groups, placements, channel breakdown), `get_shopping_products` |
| Diagnostics | `analyze_wasted_spend`, `budget_pacing` (one account or `"all"`), `audit_conversion_tracking`, `account_audit` (scored), `get_change_history`, `get_recommendations` (with verdicts), `anomaly_scan` |
| Planning | `keyword_research` (Keyword Planner, geo/lang defaults UA + uk/ru, themes), `forecast` |
| Writes (proposals) | `propose_add_negative_keywords`, `propose_add_keywords`, `propose_update_keyword_bids`, `propose_set_status`, `propose_update_campaign_budget`, `propose_update_bid_strategy`, `propose_create_rsa`, `propose_create_sitelinks`, `propose_create_callouts`, `propose_create_snippets`, `propose_create_search_campaign`, `propose_create_pmax_campaign`, `propose_create_demand_gen_campaign`, `propose_add_exclusions`, `propose_apply_recommendation` |
| Proposal lifecycle | `apply_proposal`, `rollback_proposal`, `reject_proposal`, `get_proposal`, `list_proposals`, `get_audit_log` |
| Memory | `get_account_brief`, `update_account_brief`, `log_decision`, `get_decision_log` |
| Playbooks & reports | `list_playbooks`, `get_playbook`, `generate_report` (+ MCP prompts below) |

Every tool takes `customer_id` as 10 digits (dashes optional) or an alias. Money is in the account currency
(`currency` in every response). List tools return the top `limit` rows (sortable via `sort_by`), totals computed
over all rows, and `"truncated": true` when cut.

**Playbooks / prompts** (`skills/*.md`, args `customer_id`, `lang` = uk|en, `context`): `weekly_review`,
`wasted_spend_cleanup`, `new_search_campaign`, `new_pmax_campaign`, `pmax_troubleshooting`,
`conversion_tracking_audit`, `budget_pacing_check`, `search_term_mining`, `client_report`.

## 5. Account brief

`update_account_brief(customer_id, patch)`: business type, margin, `kpi` (cpa|roas), `target_cpa`,
`target_roas` (ratio, 4.0 = 400%), `monthly_budget`, per-campaign monthly budgets, products, brand / competitor /
irrelevant terms, languages, geo, seasonality, `brand_impression_share_min`, rules. You can also drop hand-written
files into `briefs/<customer_id>.yaml` or `briefs/<alias>.yaml` (see `briefs/example.yaml`); values set through the
tool override the file. Analysis thresholds come from the brief. For example, wasted spend means cost ≥ 2× target CPA
with 0 conversions; if there's no target, it falls back to 2× the account's CPA, never below `min_waste_spend`.

## 6. Example prompts

- «Покажи всі акаунти і задай аліаси: 123-456-7890 = stryi, 234-567-8901 = abtime»
- «stryi: тижневий огляд українською» → or `/mcp__adsops__weekly_review stryi uk`
- «Знайди неефективні витрати в abtime за 30 днів і підготуй мінус-слова на спільний список»
- «Перевір налаштування конверсій у stryi — чи немає дублів покупок і мікроконверсій як primary?»
- "Budget pacing for all accounts — which ones will overspend this month?"
- "Keyword research for 'кутовий диван' in Lviv, uk + ru, group into ad groups and propose a paused Search campaign"
- "Why did PMax ROAS drop last week in stryi? Use the pmax_troubleshooting playbook."
- "Client report for abtime, last month, English, with XLSX"

## 7. Monitoring (optional)

```bash
python monitor.py --dry            # print alerts
python monitor.py                  # send to Telegram (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID in .env)
```
cron: `15 9 * * * cd /abs/adsops-mcp && .venv/bin/python monitor.py`. Windows Task Scheduler: program
`.venv\Scripts\python.exe`, arguments `monitor.py`, start in the `adsops-mcp` folder. Read-only by construction; alerts
are de-duplicated per account / check / day.

## 8. Quotas & performance

- Reads use **SearchStream** (one request per report regardless of size). Writes are batched into one
  `GoogleAdsService.Mutate` per proposal (atomic for campaign builds with temp IDs; partial-failure mode for bulk
  criteria, chunks of 5,000 ops).
- Retries with exponential backoff + jitter on `RESOURCE_EXHAUSTED`, transient/internal errors and
  `CONCURRENT_MODIFICATION`, honouring Google's `retry_delay` hint.
- Account metadata, campaign names and geo names are cached for 1 hour; performance data is always live.
- API v24+ rejects filters on fields that are not selected; the client adds such fields to SELECT automatically
  (also for `run_gaql`).
- Keyword Planner calls count against a separate, lower rate limit — `keyword_research` makes one request per language.

## 9. Tests

```bash
pytest                                      # 70 offline tests
ADSOPS_TEST_CUSTOMER_ID=... pytest -m integration          # against a Google Ads TEST account (test MCC)
ADSOPS_INTEGRATION_APPLY=1 ADSOPS_TEST_CUSTOMER_ID=... pytest -m integration   # + apply & roll back
```
The offline suite mocks `GoogleAdsService` (SearchStream batches, quota retries, partial failures), checks every
GAQL field any tool issues against the v26 `GoogleAdsRow` descriptors, and builds every write with real v26
`MutateOperation` messages. Things it cannot catch without the API: segment/metric compatibility rules and
server-side validation. That's what `scripts/smoke.py` and the integration test are for.

## 10. Known limits

- Consent-mode status is not exposed by the Google Ads API (the audit says so; check Google tag diagnostics).
- Conversion actions are audited, not edited: fixes are made in the UI / GTM on purpose.
- PMax channel breakdown uses `segments.ad_network_type`; asset-level metrics for PMax are used when the API returns
  them. If a section isn't supported on your account, it shows up in `section_errors` instead of failing the tool.
- Product exclusions in PMax listing groups are reported with suggested actions; the tool doesn't edit listing-group
  trees beyond the initial "all products" group.
