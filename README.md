# Polsia — AI Business Agent Platform

An autonomous multi-agent system that runs your business on autopilot. Nine specialized AI agents handle social media, competitor research, email outreach, ads management, customer support, code generation, business planning, finance, and orchestration — on a DeepSeek-first runtime (V4.1 Flash via the OpenAI-compatible API; the key is entered in the app and stored server-side) with the Claude Code CLI as a fallback.

**Start here:** [USAGE.md](USAGE.md) — the operator's guide: what MYPOLSIA is, the first-run
checklist, and how to work each feature day-to-day. [DEPENDENCIES.md](DEPENDENCIES.md) — install.
Readiness: [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) · what's done/next: [NEXTSTEPS.md](NEXTSTEPS.md).

## What this is, in one screen

- **The fleet** — an orchestrator (tool-less planner: your morning plan + evening summary) plus eight
  specialist agents, all on a **DeepSeek-first runtime** (key entered in the app, stored
  server-side) with the Claude Code CLI as fallback. Every external action passes a policy
  firewall: sandbox labeling, per-channel send caps, cost budgets, an approval flow, and a
  kill switch.
- **The day** — the morning cycle (your company-local hour) has the orchestrator write the plan,
  materialises the tasks it returns, and files it in the dashboard's Daily Report; the evening
  cycle files the day's summary. Tasks, approvals, runs and phone events stream into the live
  Activity feed; the chat rail talks to the orchestrator.
- **The rule** — nothing fakes anything: a sandboxed action is a labelled draft, an unmeasured
  value is `null` (not 0), and a missing credential is a refusal **by name** with the fix in it.

## Feature tour

| Surface | Route | What you do there |
|---|---|---|
| **Dashboard** | `/dashboard` | Morning plan + Daily Report, live Activity feed, chat rail, Auto Mode, send-caps counter; widgets show/hide/reorder. **V23:** the feed header prints the server's own total — `N events` (`1 event` singular) from the same response's `X-Polsia-Total`; no header, no count (never the rows on hand) |
| **Agents** | `/agents` | Fleet status; run a single agent on demand; **per-agent toolbox (V20)** — each card's `TOOLS · N` expander lists the tools that agent can actually use (names + the registry's own descriptions, from `GET /api/v1/ops/capabilities`); honest "No tools are granted to this agent." for an empty list, and `TOOLS — UNAVAILABLE` + Retry when the read fails (never "no tools"). Needs the V20 frontend rebuild. **V26:** the page reaches a terminal state even when it was opened in a hidden tab — the mount read is never skipped, `loading` ends on success/error/empty, an empty answer is stated as such, and a failed read renders `Could not load agent status: …` with a working Retry |
| **Tasks** | `/tasks` | The work queue — filter, open a task (deep-links `/tasks/{id}`), run or approve parked rows. **V25:** image attachments (PNG/JPEG/WebP ≤5 MB, ≤4/task, magic-byte sniffed; the blob is served with the sniffed type). **V26:** a per-image **Describe** action renders the model's sentence inline under "Model description — not saved to the task" — it is never written to the task result |
| **Planner** | `/planner` | Propose/confirm the business plan (sourced claims verified) |
| **Schedules** | `/schedules` | Recurring task prompts (15-minute cadence, company-local, DST-safe) |
| **Social** | `/social` | Post drafting/publishing + mention replies (really threaded) |
| **Outreach** | `/outreach` | Prospects + compliance-gated cold email with follow-up sweep |
| **Prospects** | `/prospects` | *V27* — the owner-managed CRM: add / correct / remove a row, search across email, name and company, filter by exact status; blank and duplicate addresses are refused by name; the list carries the server's declared total (`X-Polsia-Total`) and an honest `{items: [], total: 0}` when empty |
| **Support** | `/support` | The inbox mirror; draft/send replies through the one compliance bridge |
| **Ads** | `/ads` | Campaigns + **per-campaign metrics expander** (V22): opening a campaign issues one `GET /ads/campaigns/{id}/metrics` and renders the real rows (null → `—`, measured 0 → a real zero); collapsed issues no request. Ads↔Stripe sync every 6 h. The count line reads the server's declared total ("N of M campaigns") |
| **Finance** | `/finance` | Revenue/expenses/invoices KPIs (`null` when never measured). **V23:** a **Human rate** card sets/clears the owner's hourly rate (`PUT /config {"human_hourly_rate": n \| null}`, server response reflected) — gross margin becomes measurable only once price, AI cost (needs capacity) and human hours are also measured; the Planning row shows `—` with the Finance reason until then |
| **Invoices** | `/invoices` | Create → send → void, with the dunning ladder and amount-gated emails. V22: an optional **line-item editor** on create (blank row omitted; a touched row must name its description and carry numeric qty/price — refused before POST), a read-only `Items (N)` breakdown on each row, and an honest truncation marker from the server's declared total |
| **Usage** | `/usage` | Agent-runs ledger + CSV export + ops panel (breaker stats, quality reads). Since V21's settle every ledger row links to `/runs/{id}`; V22 adds the run's **real tool activity** — one chip per tool with a count when `tools_used` was recorded, "No tools were called in this run." for a recorded empty list, and "Tool activity: not recorded for this run." for legacy/missing rows (never a fabricated chip or zero) — beside the stored `num_turns`/`is_error`. **V23:** the ops panel adds a **Worker process** card fed by `GET /api/v1/ops/activity-publish` — the worker's last published breaker snapshot (failed publishes, skipped-while-open, breaker state, publish time) or an honest "not published yet" (never fabricated zeros); the API-process counters stay separate |
| **Memory** | `/memory` | Company-memory search (+ add/forget); runs read and write memories |
| **Ops** | `/ops` | *V25* — the God Mode cockpit: live runs, pending approvals, manual agent dispatch, the sandbox/kill switches, and the *V27* retention panel (dry-run by default; `apply` deletes exactly the expired run directories and records the counts). Every control calls a real handler; every failure renders the server's named refusal, never a fabricated success |
| **Settings** | `/settings` | Company + owner profile, sender identity, DeepSeek key, night task, data export/wipe, pricing, site package. **V23:** a **cycle-report email** switch in Emails (`GET`/`PUT /config` → `cycle_report_email`; awaits the server, never flips optimistically) and a **KPI editor** below the form card — six keys (`mrr_usd`, `active_customers`, `churn_rate`, `cac_usd`, `ltv_usd`, `nps`); a blank box removes the key (never saved as 0), clear-all writes `{}`. **V26:** Settings → **Integrations** — a section below the form card (not a fifth tab) with masked status for GitHub/Render/SendGrid (writable) and Stripe/Meta/Google (read-only), save/clear through the one secret store, and read-only test probes whose refusals are shown by name (`E_AUTH`, `E_UPSTREAM`, …). No response carries a credential value |
| **Sites** | `/sites` | *V24/V26* — build a site from the company profile and preview it. **V26** writes three real pages (`index.html`, `about.html`, `contact.html`) in one of three script-free presets, serves them at `/sites/<slug>/` through nginx, emits `sitemap.xml` with absolute locs, and RECORDS a custom domain (never DNS-verified; a scheme/path is refused 422 `E_VALIDATION`). The site is hosted on this box; the Render adapter only SUBMITS a deploy of a configured service (dry-run by default, `E_CONFIG` without a key) |
| **Phone** | `/phone` | *Live (V16)* — texts/calls + media (photos/attachments stream from the store) + archive download, a click-to-fetch **verification-codes panel** (V19: masked by default, reveal/copy re-masks, every fetch audit-logged without values), plus a policy-gated send composer (`POST /api/v1/phone/send`; refusals by name: sandbox / unpaired / unavailable / validation). Since V19 the **host send bridge** (`scripts/phone_send_bridge.py`, OFF by default) lets the Docker stack send through the host's paired browser. **Thread view (V22):** any thread row's **Open conversation** opens an in-page panel (`GET /phone/messages?limit=100&thread_id=N`, fetched only on selection) with loading / empty / named-refusal / store-unavailable states and `← All threads` to return. **Agents (V20):** the firewalled read-only `phone_read` tool — newest threads + optional per-thread messages, **codes never** (code-shaped bodies withheld), granted to `orchestrator` + `customer_support` only; the running image shows the new grant after the V20 rebuild (USAGE §5.3). See USAGE §5.1–5.4; [PHONELINK-MCP-TEST.md](PHONELINK-MCP-TEST.md) |
| **Capabilities** | `/capabilities` | *Live (V16)* — read-only map of every agent's real tools, action classes and channels (derived from the registry, never hand-copied); linked from Settings; `GET /api/v1/ops/capabilities` |
| **API docs** | `/apidocs` | *V21* — the API's own **Swagger UI**, proxied by nginx to FastAPI's `/docs` (`/docs` itself stays with the Next app, which has no such page in this tree, so it answers 404). The schema is at `/openapi.json`. Explore/probe any route by hand: each protected operation shows an `X-API-Key` header field — use **Try it out**, paste the owner key, **Execute** (this app declares the key as a header parameter, not a security scheme, so there is no Authorize button). Auto-hidden (404) when `SANDBOX_MODE=false`. Needs the V21 nginx reload; `make smoke`'s `api-docs` check proves both URLs (USAGE §7.2) |

### V28 hardening sweep — measured 2026-10-19 (this commit)

Six sweep lanes, **no new features**: frontend state, backend error paths, wiring audit, e2e
fidelity, data integrity, adversarial truth. Every product fix landed RED-first.

| Gate | Result | Evidence |
|---|---|---|
| Backend | **3,396 tests / 0 failed / 0 errors / 13 skipped** (`EXIT=0`) | `wave-v28/v-settle/gate.xml` |
| Frontend (jest) | **113 suites / 1,026 tests passed** (`EXIT=0`) | `jest-full.log` |
| Integration (real PG/Redis) | **15 passed / 0 failed** | `integration.xml` |
| Full e2e on a **fresh** DB | **53 passed / 10 skipped / 0 failed** | `e2e-full-freshdb.log` |
| Live drills | **45/45, 17/17, 42/42, 41/41**, V28 **22/22** | `drills.log`, `v28-live.sh` |

Fixed by the sweep: deleting a prospect with send history was a raw 500 (now `E_VALIDATION`); a
failed site create leaked a half-written directory and wedged the slug (now atomic writes,
`E_STORAGE`, retryable); the opt-out producer stored a differently-cased address than the CRM; the
retention panel invented “just now” for a record with a null timestamp; AutoMode polled a hidden tab;
a schedules create could fabricate a one-row table before the list read; the cleanup script deleted
`agent_runs` by `task_id` instead of run id (it could delete an untagged task's run) and could orphan
`email_logs`; the SQLite engine never enabled `PRAGMA foreign_keys`, so every cascade was decorative
there; the Render adapter resolved two settings by computed name. Docs: five false claims corrected
(Render “not implemented”, DeepSeek “cannot call tools”, route/table/tool counts, the surface table
missing `/sites` `/ops` `/prospects`, the feature tour), plus two stale e2e assertions (the `/ops`
empty ledger and the “rows are only ever added” outreach premise).

Residuals: SendGrid/Stripe **live** probes need owner keys; a vision cache hit writes no history row
for the reading task; wiring the per-run cleanup into the executor is owner-gated (the retention beat
owns cleanup today); low-severity findings are listed in each lane receipt.

### V27 settlement — measured 2026-10-19 (this commit)

Six lanes: site lifecycle, retention automation, SendGrid/Stripe probes, vision memory, prospect CRM,
proof/docs. Every number below is copied from `wave-v27/v-settle/`; none is estimated.

| Gate | Result | Evidence |
|---|---|---|
| Backend | **3,337 tests / 0 failed / 0 errors / 13 skipped** (`EXIT=0`) | `v-settle/gate2.xml`, `gate2.log` |
| Frontend (jest) | **108 suites / 1,017 tests passed** (`EXIT=0`) | `v-settle/jest-full.log` |
| Live V27 drill | **41 passed / 0 failed** (migration head `0016`) | `v-settle/v27-live.log` |
| V26 drill on the V27 build | **42 passed / 0 failed** | `v-settle/v26-drill.log` |
| Carried V25 drills | **45/45** and **17/17** passed | `v-settle/v-live.log`, `v25-extra.log` |
| Screenshots | 4 (sites lifecycle, ops retention, prospects, vision history) | `v-settle/shots/` |

The rebuilt stack was drilled end to end: a site regenerated to three pages then deleted (second
delete 404); the retention preview left 21 seeded run dirs intact and the apply removed exactly 1
(20 kept) on a temp root; SendGrid reported `testable:true` and a fake key was refused **401
`E_AUTH`** with the value never returned; the first vision describe stored a row (`cached:false`) and
the second was served from memory (`cached:true`, `Previous descriptions (1)`, no provider call); a
prospect was created, found by search, refused as a duplicate, patched, deleted and then 404'd.

### V26 settlement — measured 2026-10-19 (commit `d6bd944`)

Six lanes: agents-page truth, integrations & secrets, run workspaces, vision on demand, sites 2.0,
proof/cleanup. Every number below is copied from `wave-v26/v-settle/`; none is estimated.

| Gate | Result | Evidence |
|---|---|---|
| Backend | **3,226 tests / 0 failed / 0 errors / 13 skipped** (`EXIT=0`) | `v-settle/gate3.xml`, `v-settle/gate3.log` |
| Frontend (jest) | **104 suites / 983 tests passed** (`EXIT=0`) | `v-settle/jest-final.log` |
| Live V26 drill | **42 passed / 0 failed** (migration head `0015`) | `v-settle/v26-live.log` |
| Carried V25 drills | **45/45** and **17/17** passed | `v-settle/v-live.log`, `v-settle/v25-extra.log` |
| Screenshots | 5 (agents, describe, ops, settings-integrations, sites) | `v-settle/shots/` |

The rebuilt served stack was drilled end to end: 9 agent cards with `loading:false`; six integration
providers with masked status, save/clear and a **401 refusal by name**; vision Describe naming red and
blue; three site pages + `sitemap.xml` through nginx with a **422** domain refusal; a code run writing
under `runs/263`; `ads.spend_usd` with **no column default** and an honest empty ads table; the
cleanup script dry-run deleting nothing.

**Residuals, not hidden:** real Render deploys and authenticated GitHub writes are owner-gated (no
keys; both refuse `E_CONFIG`); custom domains are recorded, never DNS-verified; vision needs a
configured model (`E_CONFIG` otherwise); ads writes remain unproven; the live ads DB has no
`ad_metrics` rows (honest empty — the NULL-vs-0 discipline is unit-pinned and the DDL proves the
column). **BLOCKED:** coverage %, the mypy file count, ruff/tsc output and the Playwright suite count
are not in the V26 settle evidence, so none is claimed as a V26 number. The V27/L6 lane adds four
Playwright specs (agents, integrations, vision, sites) and runs them **4/4** — that is a V27 result,
not a V26 one.

## How to use it

1. **Install** — Windows + WSL2 + Docker Desktop + Make, then clone: [DEPENDENCIES.md](DEPENDENCIES.md).
2. **Boot** — `cp .env.example .env` (set `API_KEY`, keep `SANDBOX_MODE=true`), `docker compose up -d`,
   `make init-db`, open `http://localhost`. Details: [Quick Start](#quick-start) below.
3. **First-run checklist** — env → stack → health → Settings (company profile, sender identity,
   owner profile, DeepSeek key): [USAGE.md §3](USAGE.md).
4. **Work it** — morning plan → approvals → activity feed → the surfaces above, feature by
   feature: [USAGE.md §4](USAGE.md).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                         nginx                           │
│              / → frontend   /api/ → backend             │
└────────────────┬────────────────────┬───────────────────┘
                 │                    │
        ┌────────▼──────┐    ┌────────▼──────────┐
        │  Next.js 14   │    │  FastAPI (Python)  │
        │  (frontend)   │    │  + WebSocket       │
        └───────────────┘    └────────┬───────────┘
                                      │
              ┌───────────────────────┼───────────────────┐
              │                       │                   │
     ┌────────▼────────┐   ┌──────────▼──────┐  ┌────────▼──────┐
     │  PostgreSQL 16  │   │  Redis + Celery  │  │  ChromaDB     │
     │  (24 tables)    │   │  (task queue)    │  │  (vector mem) │
     └─────────────────┘   └──────────────────┘  └───────────────┘
```

### The 9 Agents

| Agent | Responsibility | Schedule |
|-------|---------------|----------|
| **Orchestrator** | Writes morning plan + evening summary | Morning: hourly, self-gated to the configured company-local hour (default 06:00) — Evening: hourly, self-gated to the configured company-local hour (default 20:00) |
| **Business Planning** | Strategy, KPIs, growth recommendations | Via the morning plan (no beat entry) |
| **Competitor Research** | Web search, profile updates | Via the morning plan (no beat entry) |
| **Social Media** | Draft + post tweets | Every 2h |
| **Email Outreach** | Prospect finding + cold email sequences | On demand — no beat entry creates it |
| **Customer Support** | Read inbox, draft replies | Every 3h |
| **Ads Management** | Google + Meta campaign optimization | Every 6h |
| **Code Generation** | Ship features, open PRs | On demand |
| **Finance** | Stripe revenue sync, spend tracking | Every 6h |
| **Maintenance** | Task-lease sweep — requeues tasks whose worker died mid-run | Every 5 min |
| **Maintenance** | Stranded-pending reconciler — re-dispatches scheduler rows orphaned by a crash between commit and broker hand-off | Every 5 min |
| **Maintenance** | Activity retention — deletes `activity_log` rows older than `activity_retention_days` (default 90) | Daily at 03:00 UTC |

Each agent runs on the provider-selectable runtime: DeepSeek-first (V4.1 Flash via the OpenAI-compatible API — enter the key in Settings or the dashboard banner; it is stored at `backend/data/deepseek_api_key.json`, masked, and changeable) with the Claude Code CLI (`claude -p "..." --output-format json`) as the fallback, which needs no API key — it authenticates via OAuth stored at `~/.claude`.

Agent supervision tiers — auto / review / approve / human-only — each mapped to the code path that enforces it: [`docs/SUPERVISION.md`](docs/SUPERVISION.md).

## Prerequisites

**Full link-by-link setup guide — WSL2, Docker Desktop, GNU Make, Python, Node, Git, Stripe CLI, with install + verify commands and a troubleshooting table: [DEPENDENCIES.md](DEPENDENCIES.md).**

Quick summary:

- **Windows 10/11 + WSL2** (virtualization enabled) → **Docker Desktop** (WSL2 backend) — the stack runs via Docker Compose
- **GNU Make** (for `make init-db` / `make test` / …) — [ezwinports make via WinGet](https://sourceforge.net/projects/ezwinports/)
- A [DeepSeek API key](https://platform.deepseek.com) (default runtime, set in-app after first start) or the [Claude Code CLI](https://docs.anthropic.com/claude-code) installed and authenticated (`claude login`) for the fallback runtime
- Dev-only: **Python 3.12** (tests without Docker), **Node.js ≥20** (≥22/24 for Playwright E2E)
- Optional for real operations: [Stripe CLI](https://docs.stripe.com/stripe-cli) + Stripe, Twitter/X, SendGrid, Google Ads, Meta Ads accounts

## Quick Start

```bash
# 1. Clone and configure
git clone <repo>
cd polsia
cp .env.example .env
# Edit .env — at minimum set API_KEY and SANDBOX_MODE=true

# The dashboard sends NEXT_PUBLIC_API_KEY as X-API-Key; docker-compose passes it into the frontend
# image as a build arg (default: dev-key, matching API_KEY's default).  This value is inlined at
# build time; changing it requires a rebuild.
export NEXT_PUBLIC_API_KEY=dev-key

# 2. Start all services
docker-compose up -d

# 3. Initialize the database and seed company data
make init-db

# 4. Open the dashboard
open http://localhost
```

The stack is up when `http://localhost/api/v1/health` returns `{"status": "ok"}`.

Then walk the first-run checklist — company profile, sender identity, owner profile, DeepSeek key:
[USAGE.md §3](USAGE.md).

## Configuration

### Essential settings (`.env`)

| Variable | Description |
|----------|-------------|
| `API_KEY` | Secret key for the dashboard API (`X-API-Key` header) |
| `SANDBOX_MODE` | `true` = no real posts/emails/charges (default) |
| `CLAUDE_CLI_PATH` | Path to the `claude` binary. The setting's default is `claude` (found on `PATH`); the container image's ENV sets `/usr/local/bin/claude` (`backend/Dockerfile:25`) |

The fallback Claude CLI authenticates via the `~/.claude` directory on your host, which is mounted read-only into the containers (run `claude login` on your host once if you use it); the default DeepSeek runtime needs only its API key, entered in the app.

Secrets on disk: `.env` carries the deployment credentials, and the backend stores the DeepSeek key at
`backend/data/deepseek_api_key.json` (the `backend_data` compose volume). The backend tightens the key
store after every write — `chmod 600` on POSIX, `icacls <file> /inheritance:r /grant:r "<user>:(R,W)"`
on Windows — and logs a warning naming the file when neither mechanism can be applied. `.env` itself is
yours to tighten, once:

- POSIX: `chmod 600 .env`
- Windows: `icacls .env /inheritance:r /grant:r "%USERNAME%:(R,W)"`

### Integrations

Fill in the relevant sections of `.env` to activate each integration:

- **Twitter/X** — Post social content
- **SendGrid** — Send cold emails and support replies (wired); the support inbox is filled via the
  owner-authenticated `POST /api/v1/support/inbox` — there is no mail-poll (IMAP) setting
- **Web search for competitor research** — no web-search key exists (the advertised-but-dead
  settings were removed, D14): under the CLI runtime the agent reaches the web through the CLI's
  own WebSearch/WebFetch tools; under the DeepSeek-first runtime the agent's registry tools
  (`web_fetch`, `web_search`) ride the request and every `tool_calls` turn is executed by
  `app/agents/deepseek_runtime.py`, so `competitor_research` searches and fetches for real (V28
  correction: the earlier "tool-calling is deferred / prompt context only" text was stale)
- **Google Ads / Meta Ads** — Campaign management (advertised, not implemented:
  `GOOGLE_ADS_CLIENT_SECRET` is not read by any code path; the other Google Ads settings are)
- **Stripe** — Revenue and expense tracking (`STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`; there
  is no client-side publishable-key setting)
- **GitHub** — Open PRs from the code generation agent
- **Deployment (Render)** — the site deploy adapter ships (`backend/app/services/deploy_service.py`):
  `POST /api/v1/sites/{slug}/deploy` is a DRY RUN by default and a live submit needs
  `RENDER_API_KEY` + `RENDER_SERVICE_ID`, claiming success only for a Render response carrying a
  deploy id. There is still no Deployment *agent*; the owner drives the route (V28 correction: the
  earlier "not implemented / placeholder removed" text described the pre-V25 tree)

## Known gaps (measured, not hidden)

Things this repository declares or advertises that are **not** yet true, recorded here instead of being
quietly relaxed. Each one names its measurement.

- **Backend coverage: 92.10% on the V22 tree — the floor is 90 and only moves up.** The
  historical base measurement (4,984 of 5,693 statements; `pytest tests/unit tests/parity
  tests/redteam --cov=app` with the `sysmon` core pinned in `pyproject.toml` — the truthful tracer for
  async route lines, measured by R26/R27/R28; it was 51.48% at the first measurement and 73.0% before
  the R17–R32 hardening waves) **meets the `fail_under = 75` this project originally declared**; the
  gate is a **ratchet at the measured floor (90)**, so it only moves up. V20 lane L3 measured
  **86.66%** mid-wave on an unfrozen tree (not a move); the V20 settle run measured **92.07%** and
  raised the floor **87 → 88**. V21 lane L3 then measured **92.08%** (11,678 statements, 925 missed;
  CI's exact command) and raised it **88 → 90** (`backend/pyproject.toml:58` — the single truth). V22
  lane L4 measured **92.10%** on the V22 tree (11,711 statements, 925 missed) — below the 92.3 bar, so
  the floor **stays 90**. If a future settled tree lands below 90.0, targeted tests are required
  before commit, not a lowered floor.
- **The advertised capability that had no producer wired in is now closed (with one named residual).**
  The `ad_campaigns`/`ad_metrics` writers had no callers — an honest 0-with-no-data situation, tracked
  as work, not presented as a working feature. R33 wired them: a completed `ads_management` run
  persists its own `campaigns[]`/`metrics[]` payload through `agent_tasks._persist_run_artifacts` →
  `ads_service.record_ads_run` (campaigns via `create_campaign`; metrics via the ORM, with the engine
  computing `ctr`/`cpc`/`roas` from the reported raw inputs — the generated columns are never written),
  deduped on the keys the data really has: a campaign by the provider's own id
  (`platform`+`external_id`, with a `(platform, name)` fallback), a metric by `(campaign_id, date)` —
  the key the `uq_ad_metrics_campaign_date` unique index (migration `0002`) already guards — so the
  6-hourly beat re-run inserts no duplicate rows, and a malformed entry is skipped by name in the run
  output, never silently. Pinned by `tests/unit/test_r33_ads_persist_e2e.py` and
  `tests/unit/celery/test_r33_ads_persist.py`. Residual, named not hidden: `AdMetricRow`
  (`app/schemas/agent_outputs.py:307-316`) carries no `revenue_usd`, so a persisted row's `roas` reads
  the column's 0 default until the CLI schema gains that field (R33 receipt `§REQUESTS`).
  (Two halves of this gap are closed. The finance writers are called through
  `finance_service.persist_finance_run` when a finance run is persisted, and `GET /finance/summary`
  reports `null` — not 0 — for a field that was never measured. The policy firewall's per-channel send
  caps are fed by real send records — `social_posts` (`status='published'`) for twitter/linkedin,
  `email_logs` (`status='sent'`) for email — written only after a run's send really happened
  (`agent_tasks._persist_run_artifacts` → `social_service.record_published_posts` /
  `outreach_service.record_sent_emails`, deduped by the provider's own id — SendGrid's `X-Message-Id`
  header, added by RD14), so a count at its cap now refuses the next send by name (`E_RATE_LIMITED`);
  pinned by `tests/unit/test_send_cap_producer_e2e.py` and
  `tests/unit/celery/test_send_cap_producer_worker_e2e.py`, with the customer_support leg included
  (`tests/unit/celery/test_cs_send_cap_producer.py` — CS replies persist through the same producer
  and are counted against the email channel's caps). Residuals, named not hidden: channels with
  no counting source — github, google_ads, meta_ads, stripe, render — have their configured caps refused
  by name (`POLSIA-E114`, `cap_unknown`) instead of being read as zero sends; LinkedIn has no send
  adapter.)
- **The advertised-but-dead settings list is closed (owner decision D14, 2026-10-13).** Eight
  settings no code path ever read were REMOVED — both references say they never existed: the
  original upload (`polsia-src-dossier.md` §6: the same set was read by nothing there, and no mail-
  poll or web-search variable was defined anywhere) and the live product (vendor-processed inbound
  email, no web-search provider, managed Meta-only ads, internal hosting, no batch/backlog knobs).
  Exactly two settings still have no reader, and both are deliberate and named:
  `GOOGLE_ADS_CLIENT_SECRET` — RESERVED-labelled, kept because our google_ads channel exists (the
  OAuth client secret of a client this deployment does not ship) — and
  `CELERY_WORKER_CONCURRENCY`, documented as owned by the docker-compose launch command
  (`--concurrency=4`), not the app (R20/R23). `DEBUG`/`LOG_JSON` are consumed by `app/main.py` (R22).
- **F-02 (the default API key) is half-closed by design; the key ROTATION is deliberately not
  executed.** The fresh-eyes defect sweep (F-02: `build/ui-reverse/u6-research/fresh-eyes/DEFECT-SWEEP.md`
  §F-02; ruled on in `build/ui-reverse/u6-analysis/U6-MAJOR-REPORT.md` item 2) measured the owner API
  protected only by the compose/Dockerfile default key while nginx published port 80 on `0.0.0.0`.
  The loopback half shipped (V7-T08, D1): nginx binds `127.0.0.1:80:80` only, so the network exposure
  the sweep measured is closed. Rotating `API_KEY`/`NEXT_PUBLIC_API_KEY` away from the shipped default
  is **deliberately not executed** — re-measured 2026-10-14 (V10, hash-only): `API_KEY` is still the
  default (sha256 prefix `7e9f8fd111`) and `.env` overrides no `NEXT_PUBLIC_API_KEY` — because a
  rotation needs a coordinated `.env` change plus a frontend rebuild while the loopback bind already
  removes the measured exposure. Do it BEFORE ever publishing this stack beyond localhost: set both
  to a private value and rebuild the frontend image (they must match; see "Running the stack without
  Docker" above).

## Development

### Make commands

```bash
make up              # Start all Docker services
make down            # Stop all services
make build           # Rebuild images
make logs            # Tail all logs
make shell-backend   # Open a shell in the backend container
make migrate         # Run Alembic migrations
make seed            # Seed company data
make init-db         # migrate + seed
make test            # Run unit + integration tests
make test-unit       # Unit tests only
make lint            # Ruff + mypy + ESLint
make reset-db        # Drop and recreate schema
make smoke           # GET-only acceptance against the running stack (named PASS/FAIL, exit code)
make smoke-dry       # Print the smoke plan; no request is made
```

Without Docker there is a separate target set — `make dev-backend` (the venv + local SQLite backend,
the path in "Running the stack without Docker" below), `make dev-frontend` (the local standalone
server) and `make dev-e2e` (the `:80` lab proxy in front of both) — while the `up`/`down`/`build`/
`shell-*`/`test-*` targets above require Docker.

### Running tests locally (no Claude credentials needed)

```bash
cd backend
CLAUDE_CLI_MOCK=true pytest tests/unit/ -v --cov=app
```

The `CLAUDE_CLI_MOCK=true` flag makes all agents return a mock response without invoking the CLI binary. This is also how CI works.

### Running the stack without Docker (verified on this host)

Docker is not required to exercise the app end to end. This is the path that was actually run:

```bash
cd backend
export DATABASE_URL="sqlite+aiosqlite:///./polsia-dev.sqlite"
../.venv/Scripts/python.exe -m alembic upgrade head          # shipped migrations, indexes included
CLAUDE_CLI_MOCK=true SANDBOX_MODE=true API_KEY=dev-key \
  ../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

cd ../frontend
NEXT_PUBLIC_API_KEY=dev-key NEXT_PUBLIC_API_URL=http://localhost npm run build
npm start                                                     # serves .next/standalone — see below

# the browser expects a reverse proxy at :80 (nginx in the container stack):
#   /api/* and /ws/*  ->  :8000          everything else  ->  :3000
```

Three things that bite, each of them measured rather than assumed:

* **`npm start` runs `scripts/start-standalone.mjs`, not `next start`.** The build uses
  `output: standalone`, and `next start` refuses that configuration outright ("Use
  `node .next/standalone/server.js` instead"). The script performs the same two copies the Dockerfile's
  runner stage does (`static` and `public` into the standalone tree), refuses by name
  (`E_FRONTEND_NOT_BUILT`) when there is no build, and binds `0.0.0.0` — the standalone server takes its
  bind address from `HOSTNAME`, which on Windows is the machine name, so an unset override listens on one
  interface only and `127.0.0.1` gets connection-refused.
* **The frontend's API key must match the backend's.** `NEXT_PUBLIC_API_KEY` is inlined at BUILD time and
  sent as `X-API-Key` on every request. With `API_KEY` unset the service is open (a blank header counts as
  no credential); with `API_KEY=dev-key` anything else is 401. Mismatch them and every page renders its
  empty/error state while `curl` — which sends no header — still looks healthy.
* **SQLite covers the parts that do not need PostgreSQL.** Money columns, `FOR UPDATE SKIP LOCKED`, CHECK
  constraints and the append-only trigger are PostgreSQL-only; `alembic upgrade head` on SQLite skips each
  one with a printed reason.

### End-to-end tests (Playwright)

```bash
cd e2e && npm install && npx playwright install chromium
BASE_URL=http://localhost npx playwright test     # needs the stack above answering at :80
```

The fixture-seeding specs write to the SQLite lab DB the local backend serves from: set
`E2E_SQLITE=/path/to/your/lab.sqlite` (or put the repo-relative, gitignored `<repo>/.e2e-lab.sqlite`
in place) and they run; leave it unset and they **skip by name** instead of erroring — the CI stack
is Postgres, so this is a local-lab contract only. `make e2e` / `make dev-e2e` export `E2E_SQLITE`
only when it is already set (an exported empty string would defeat the unset-default), and
`e2e/run-suite.sh` carries the full env contract.

`e2e/_diag-browser.cjs` is a diagnostic, not a test (the runner only matches `*.spec.ts`): it prints what
the browser actually saw — console errors, failed requests, every `/api/` status code and the rendered
text — which is how a page that "renders" was shown to be rendering its failure state.

### Project layout

```
polsia/
├── backend/
│   ├── app/
│   │   ├── agents/          # 9 agent implementations
│   │   │   ├── base_agent.py        # call_claude() subprocess wrapper
│   │   │   ├── crew_factory.py      # AGENT_MAP registry
│   │   │   ├── orchestrator/
│   │   │   ├── social_media/
│   │   │   ├── competitor_research/
│   │   │   ├── business_planning/
│   │   │   ├── email_outreach/
│   │   │   ├── customer_support/
│   │   │   ├── ads_management/
│   │   │   ├── code_generation/
│   │   │   └── finance/
│   │   ├── api/v1/          # REST endpoints + WebSocket
│   │   ├── core/            # DB, Redis, ChromaDB, events, security
│   │   ├── models/          # SQLAlchemy ORM (24 tables)
│   │   ├── schemas/         # Pydantic request/response models
│   │   └── services/        # Business logic layer
│   ├── celery_app/          # Celery worker, Beat schedule, tasks
│   ├── alembic/             # Database migrations
│   └── tests/
│       ├── unit/            # SQLite in-memory, CLAUDE_CLI_MOCK=true
│       └── integration/     # Testcontainers (real Postgres + Redis)
├── frontend/
│   └── src/
│       ├── app/             # Next.js 14 pages (19 nav routes incl. /phone; the /capabilities
│       │                    # page is live since V16, linked from Settings)
│       ├── components/      # ActivityFeed, MetricsCard, AgentStatusGrid, Sidebar
│       ├── hooks/           # useActivityFeed (WebSocket), useAgentStatus (polling)
│       └── lib/             # Typed API client
├── e2e/                     # Playwright end-to-end tests
├── nginx/                   # Reverse proxy config
├── scripts/                 # init_db.sh, seed_company.py, backup.sh, restore.sh
├── docker-compose.yml
├── docker-compose.ci.yml    # CI override (CLAUDE_CLI_MOCK=true, no nginx)
├── Makefile
├── USAGE.md                 # operator's guide (what it is / first run / day-to-day)
├── DEPENDENCIES.md          # install guide (WSL2 → Docker → Make)
├── RELEASE-CHECKLIST.md     # the "is it ready" pass
└── NEXTSTEPS.md             # living done/next map (PHONELINK-MCP-TEST.md: phone status)
```

## Volumes & Backup (five volumes, one command)

The stack's state lives in five named Docker volumes; `make backup` snapshots all of it into
`backups/` (gitignored):

| Volume | Holds |
|--------|-------|
| `postgres_data` | the PostgreSQL 16 data directory — every table (tasks, runs, reports, invoices, chat, memory rows, company config) |
| `backend_data` | `/app/data` — the DeepSeek **key store** (`data/deepseek_api_key.json`) plus backend runtime files; `chroma_data` nests under it |
| `chroma_data` | `/app/data/chroma` — the ChromaDB vector store (rebuildable from `memory_entries`, backed up anyway) |
| `celery_beat_data` | `/app/celerybeat` — the beat clock (when each periodic entry last fired) |
| `redis_data` | the Redis AOF — the Celery broker's queue state |

```bash
make backup                     # backups/polsia-<date>.sql + backups/<volume>.tgz (4 data volumes)
make restore SRC=backups        # prints the scratch-project drill steps; verifies the dump exists first
make restore-drill SRC=backups  # EXECUTES the restore into a throwaway container and verifies it
```

`make backup` requires the stack to be up (`docker-compose up -d`) and REFUSES by name when a
volume is missing — `docker run -v` would otherwise CREATE a missing volume and tar it empty, i.e.
a green-looking, worthless backup. Verify what landed: the `.sql` should be non-trivial in size,
and `tar tzf backups/backend_data.tgz` should list files.

### Scheduled daily backups (compose sidecar, #8)

The `backup` compose service runs `scripts/backup.sh` in a `postgres:16-alpine` container on a
24-hour loop (`BACKUP_INTERVAL_SECONDS`, default `86400`): one cycle immediately at startup, then
one per interval. A cycle writes the same artifacts `make backup` does into the same gitignored
`backups/` directory — `polsia-<UTC timestamp>.sql` via `pg_dump` over the compose network, plus
one `<volume>.tgz` per data volume. Each data volume is mounted **read-only** straight into the
container, so no docker socket and no docker CLI are needed there. The database dump is written to
a `.part` file and only renamed once `pg_dump` exits 0; a missing or **empty** volume mount is
refused by name (the same guard `make backup` applies — an empty tar is a green-looking, worthless
backup). SIGTERM ends the loop cleanly; the next start runs a fresh cycle immediately.

- Cadence: `BACKUP_INTERVAL_SECONDS` (default `86400` = daily) — set in `docker-compose.yml`.
- Retention: `BACKUP_RETENTION_DAYS` (default `14`) — older `polsia-*.sql` / `*.tgz` files are
  pruned at the end of every cycle, and every prune is logged.
- Inspect: `docker compose logs backup` (each cycle logs what it wrote, and every refusal by
  name) and `ls -la backups/` (the `.sql` should be non-trivial in size;
  `tar tzf backups/backend_data.tgz` should list files).
- Stop/start: `docker compose stop backup` / `docker compose up -d backup`.
- `make backup` is unchanged and still works for on-demand snapshots; `make restore SRC=backups` and
  `make restore-drill SRC=backups` consume either tool's output (identical artifact names).

### Restore drill (`make restore-drill` — executes; never the live stack)

`scripts/restore.sh` proves the backup restores. It never touches the live compose project: it starts
**one** throwaway `postgres:16-alpine` container named `polsia-restore-drill-pg` with **no published
ports**, loads the newest `polsia-*.sql` into a fresh `polsia` database, verifies the result against a
manifest parsed out of the dump itself (public table count + row counts for `activity_log`,
`agent_runs`, `tasks`, `memory_entries`, `task_schedules`, `chat_messages`, `company_config`), extracts
every volume archive into a temp dir and proves each listed member materialised, then **always** tears
the container down through a trap (`docker rm -f`) — on success, failure, Ctrl-C and SIGTERM. The full
command list it ran is printed in the log.

```bash
make restore-drill SRC=backups   # execute it: exit 0 PASS, 1 FAIL, 2 refusal
DRY=1 make restore-drill         # print the plan + exact commands; touches nothing
```

Refusals are by name and exit 2: missing `--src`; no `polsia-*.sql`; no `*.tgz`; a missing canonical
volume archive; an unreadable/corrupt archive; a dump with no pg_dump header; docker absent; or a
container named `polsia-restore-drill-pg` that already exists (it never reuses or replaces one — set
`RESTORE_PG_CONTAINER` for a second parallel drill). Both dump formats route correctly: plain-SQL
artifacts (what `make backup` and the backup sidecar write) load through `psql -v ON_ERROR_STOP=1`;
`PGDMP` custom-format artifacts through `pg_restore --no-owner --no-privileges`.

**Full-stack drill (optional, scratch compose project)** — the script proves the *data*; booting a
second stack proves the *app*. Never against the live project:

1. Scratch project: its own directory with its own copy of `docker-compose.yml`; start from
   empty with `docker compose -p scratch down -v`.
2. Seed each data volume from its tar (volume names take the SCRATCH project's prefix):
   `docker run --rm -v scratch_<volume>:/d -v "$(pwd)/backups":/b alpine sh -c "tar xzf /b/<volume>.tgz -C /d"`
   — once for `backend_data`, `chroma_data`, `celery_beat_data` and `redis_data`.
3. Load the dump: `docker compose -p scratch up -d postgres`, then
   `docker compose -p scratch exec -T postgres psql -U polsia polsia < backups/polsia-<date>.sql`.
4. Boot it: `docker compose -p scratch up -d`.
5. Smoke: `curl -fsS http://localhost/api/v1/health` returns `{"status":"ok"}`, and ONE dashboard
   page renders real restored rows (not the empty state).

### One-command acceptance (`make smoke`)

`scripts/smoke.sh` is the release checklist as one command. It issues **GET/HEAD requests only** —
nothing is written, no service is started or stopped, and the compose project is never touched — and
prints one named line per check (`PASS <name>: <detail>` / `FAIL <name>: <detail>`), exiting 0 only
when every check passed (1 = a check failed, 2 = usage refusal; through `make`, a failed check
surfaces as make's own exit 2).

```bash
export API_KEY=<the same key the stack was booted with>   # environment, never `.env`, never inline
make smoke                                                # base http://127.0.0.1
make smoke SMOKE_ARGS="--api-base http://127.0.0.1:8000"  # also check the API's own /docs
make smoke-dry                                            # print the plan, make no request
```

What it proves: `/api/v1/health` is exactly `{"status":"ok"}` · `/` carries CSP +
`X-Frame-Options` + `nosniff` · a protected route answers **401 `E_AUTH` without a key** and a 200
envelope with it · `/api/v1/ops/capabilities` has agents (>0) in sorted order · `/api/v1/phone/summary`
is either real (`store_status=ok`) or a **named** 503, and `/api/v1/phone/media?thread_id=…` returns
the frozen `parts[]` shape or a named refusal (a raw 500, or a plain 404 from a missing route, is a
FAIL) · `/api/v1/phone/codes?limit=1` answers `data.codes[]` and the check reports a **count only** —
code values are never printed, even on failure · `/docs` visibility matches `SANDBOX_MODE` in `.env`
(when the base fronts the Next app at `/docs`, the check reports a named SKIP and tells you to pass
`--api-base`) · the API's own **Swagger UI** answers `GET /apidocs` with a 200 that carries a
Swagger-UI marker **and** `GET /openapi.json` answers 200 with an `openapi` key (V21; through the
proxy by default, or at `--api-base` when given — with `SANDBOX_MODE=false` this is a **named SKIP**
because the API hides its docs by design, and a 404 pair is a named SKIP **only** when the repo's
`nginx/*.conf` has no `/apidocs` location, i.e. a pre-V21 build; once the config has it, 404 is a
FAIL naming the reload/rebuild) · the proxy does not disclose its version in `Server` · the optional
**send bridge** answers `200 {status:"ok"}` at `$PHONE_SEND_BRIDGE_URL/health` when the URL is
exported, and is a **named SKIP** when it is not (bridge OFF — the default; never a silent pass) ·
`backups/` holds a dump plus the four volume archives (WARN-only — a fresh clone has none until
`make backup`).

Its own pins live in `backend/tests/unit/test_v18_smoke_script.py`: static (no write verbs, no `jq`
dependency, `set -u`, no docker, no traps), a dry run that is executed and must make zero network
calls, behaviour against a curl shim (missing key fails by name; a raw 500 is never green; the codes
check reports a count and never a value — even on a malformed body; the bridge skips by name when
unset and fails on `status != ok`, a 5xx, or a scheme-less URL), and a live run that skips with a
reason when nothing answers.

V21 makes it **13 checks**: the new `api-docs` check above sits beside the original 12, which are
unchanged. Its route contract is the nginx rule that landed with the same wave (`/apidocs` →
backend `/docs`; `/openapi.json` → backend schema), so the smoke proves the operator can actually
reach the docs rather than merely that FastAPI would serve them on a port nobody proxies.

V20 adds **two acceptance surfaces that are not smoke checks**, because neither is a GET on the public
stack: the `phone_read` agent tool must be exercised **only through the firewalled `execute_tool` path**
(an agent without the allowlist entry refused by name `POLSIA-E114`; the result must carry no code
field; an absent store must answer `E_PHONE_UNAVAILABLE`, never an empty list), and the **Agents-page
toolbox** must be read visually against `GET /api/v1/ops/capabilities` after the frontend rebuild
(honest empty for a tool-less agent). Both are parent live-checks in
[RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) §5. At this write the `phone_read` lane's receipt is
**complete** and **one firewalled call already ran against the real store** (counts only); what remains
is the rebuilt-image check (`GET /ops/capabilities` lists the two holders) and an agent-bound call. The
toolbox lane's receipt is complete (10/10 pins, `tsc` exit 0) but its **live visual is unrun** — the
served frontend is still the V19 build. Neither surface is claimed live-proven until the settle
rebuild.

### Data export & wipe

- `GET /api/v1/data/export` (owner key) streams a ZIP — `MANIFEST.json` (member names + row
  counts as exported) + CSVs (tasks, agent_runs, activity_log, revenue_snapshots, invoices,
  task_schedules) + JSON (memory_entries, chat_messages). The DeepSeek key store and `.env`
  contents are never in it.
- `DELETE /api/v1/data?dry_run=true` reports WOULD-delete counts (deletes nothing; no token
  needed). `DELETE /api/v1/data?confirm_token=<token>` wipes the member tables in one transaction
  and keeps `company_config` + the key store. The token is **single-use**, minted through
  `POST /api/v1/approvals` for the `action_fingerprint` the dry run returns (same
  `APPROVAL_SECRET`); a reused token is refused by name. The `/api/v1` surface is 102
  method+path operations (V28 measurement from the app's own OpenAPI schema; the original 65-route
  tree documented through V9 is pinned route-by-route in `backend/tests/parity/`).

## CI/CD

Three GitHub Actions workflows run automatically:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | PR + push to main | Lint (ruff, ESLint, and **mypy as a hard gate since V21** — the step carries no `|| true`), unit tests |
| `integration.yml` | Push to main | Integration tests (testcontainers), Playwright E2E |
| `docker-build.yml` | Push to main | Build all images, health-check the stack |

All CI jobs set `CLAUDE_CLI_MOCK=true` — no Claude credentials are needed in GitHub Actions.

## Turning off sandbox mode

When you're ready to let agents operate for real:

1. Set `SANDBOX_MODE=false` in `.env`
2. Fill in the relevant API keys for the integrations you want active
3. Restart the stack: `make down && make up`

Agents will now post to Twitter, send emails, create ad campaigns, and issue Stripe operations.
