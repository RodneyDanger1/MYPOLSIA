# COMPLETENESS — MYPOLSIA, the honest final statement

Audit wave **V22** (8 seats: W1 wiring · W2 honesty · L1 tools persistence · L2 phone thread view ·
L3 wiring fixes · L4 proof/coverage · L5 docs · L6 verifier). Base commit `4f6a834`.
**V23 pass at 2026-10-19 13:09 CDT** closed the five remaining product asks in §7 — items 6, 8, 9,
10 and 11 — from the four lane receipts under `wave-v23/`. **L6's frozen-tree gate #2 PASSED:**
backend **2,745 tests / 0 failed / 0 errors / 13 skipped**, jest **23 suites / 278 tests**, `tsc` 0
bytes, ruff clean; per-lane verdicts **PASS / PASS-WITH-NOTES** (the two notes are LOW traceability
accuracy only). The wave is still **uncommitted**, `0011` is not live, and the five live checks are
the parent's.
Written **2026-10-19 09:50 CDT; last updated 13:09 CDT**. This file is built only from what is on
disk; every sentence below names its evidence. Anything not yet receipted is marked **in flight (no
receipt at this write)** — never presented as done.

> **Read this first.** The two read-only audits, all four writer lanes, and the verifier's frozen-tree
> gate have passed and their evidence is on disk. **The wave is still uncommitted and the served
> frontend predates it**, so the live UI and e2e runs remain parent steps. Section 7 is the
> remaining-items list — it is not empty (item 7's retire decision and V23's settlement item 18);
> D1/D2 are closed at settle, with their owner step named in §7.1.

---

## 1. What MYPOLSIA is

A self-hosted business-operations platform: **FastAPI + Celery** backend, **Next.js 14** frontend,
**nginx** in front, **PostgreSQL 16** (22 tables), **Redis** (broker), **ChromaDB** (vector memory).
An orchestrator plus eight specialist agents run on a **DeepSeek-first runtime** with the Claude Code
CLI as fallback; every external action passes a policy firewall (sandbox labelling, send caps, cost
budgets, approvals, kill switch). Sources: `README.md:9-21`, `USAGE.md:10-26`, `docker-compose.yml`,
`backend/app/models/`.

## 2. The surfaces — feature, page, API

`frontend/src/app/` holds **19 `page.tsx`** files: **16 top-level routes** in the navigation
(`frontend/src/components/layout/nav-items.ts:39-68`) plus the root redirect and two detail routes
(`/tasks/[id]`, `/runs/[id]`). The backend exposes **77 method+path operations** across **26 modules** in
`backend/app/api/v1/` (counted from the decorators at the V23 write). The V22 live `/openapi.json`
capture banked at `wave-v22/w1-wiring/raw/openapi.tsv` recorded `TOTAL 76`; it predates V23's
`GET /api/v1/ops/activity-publish` (`backend/app/api/v1/ops.py:106`, L4).

| # | Surface | Page (source of truth) | Backend it consumes |
|---|---|---|---|
| 1 | Dashboard | `frontend/src/app/dashboard/page.tsx` + `components/dashboard/*`, `chat/*`, `auto_mode/*` | `dashboard.py` (summary, activity, reports, reports/daily), `chat.py`, `auto_mode.py`, `channels.py` (caps), `notifications.py`, plus the widgets' reads (`ads`, `social`, `outreach`, `tasks`, `finance/planning`, `phone/summary`, `config`, `health`) |
| 2 | Agents | `frontend/src/app/agents/page.tsx` + `components/agents/*` | `agents.py` (`/status`, `/{agent_type}/trigger`), `ops.py` (`/capabilities` — the toolbox) |
| 3 | Tasks | `frontend/src/app/tasks/page.tsx`, `app/tasks/[id]/page.tsx` + `components/tasks/*` | `tasks.py` (list, create, get, status patch), `agents.py` (trigger), `approvals.py` |
| 4 | Planner | `frontend/src/app/planner/page.tsx` + `components/planner/*` | `planner.py` (`/status`, `/propose`, `/confirm`), `config.py` (first-dollar checklist) |
| 5 | Schedules | `frontend/src/app/schedules/page.tsx` + `components/schedules/*` | `schedules.py` (list, create, patch, delete) |
| 6 | Social | `frontend/src/app/social/page.tsx` + `components/social/*` | `social.py` (`/posts`), `tasks.py` (new task) |
| 7 | Outreach | `frontend/src/app/outreach/page.tsx` + `components/outreach/*` | `outreach.py` (`/prospects`), `tasks.py` |
| 8 | Support | `frontend/src/app/support/page.tsx` + `components/support/*` | `support.py` (`/inbox` GET + POST), `tasks.py` (linked task) |
| 9 | Phone | `frontend/src/app/phone/page.tsx` + `components/phone/*` | `phone.py` — all 8: `/summary`, `/messages`, `/calls`, `/codes`, `/media`, `/media/{part_id}`, `/send`, `/export` |
| 10 | Ads | `frontend/src/app/ads/page.tsx` + `components/ads/*` | `ads.py` (`/campaigns`, `/campaigns/{id}/metrics`) |
| 11 | Finance | `frontend/src/app/finance/page.tsx` + `components/finance/*` | `finance.py` (`/summary`, `/revenue`, `/expenses`, `/events`, `/planning`), `economics.py` (`/context`) |
| 12 | Invoices | `frontend/src/app/invoices/page.tsx` + `components/invoices/*` | `invoices.py` (list, create, get, `/send`, `/void`) |
| 13 | Usage + run detail | `frontend/src/app/usage/page.tsx`, `app/runs/[id]/page.tsx` + `components/usage/*`, `components/runs/*` | `runs.py` (list, `/export`, `/{id}`), `ops.py` (`/events/stats`, `/events/reset`, `/agent-quality`, `/activity-publish` — the last added by V23/L4) |
| 14 | Memory | `frontend/src/app/memory/page.tsx` | `memory.py` (list, create, delete) |
| 15 | Settings | `frontend/src/app/settings/page.tsx` + `components/settings/*` | `config.py` (6 ops incl. DeepSeek key), `data.py` (export/wipe), `pricing.py`, `site.py` (package + download), `approvals.py` |
| 16 | Capabilities | `frontend/src/app/capabilities/page.tsx` + `components/capabilities/*` | `ops.py` (`/capabilities`) |
| — | Root | `frontend/src/app/page.tsx` — redirects to `/dashboard` | — |
| — | API docs | `/apidocs` → nginx → FastAPI `/docs`; schema `/openapi.json` (`nginx/nginx.conf`, V21) | `main.py` docs routes |
| — | Activity stream | shell-wide feed (`components/shell/ActivityStrip.tsx`) | WebSocket `/ws/activity` (`backend/app/api/v1/ws.py`) |

The four agent tools in the registry are `web_fetch`, `macro_snapshot`, `pricing_catalog`,
`phone_read` (`backend/app/agents/tool_registry.py:124-215`). The Celery task families are
`agent_tasks`, `cadence`, `daily_cycle`, `dunning`, `followups`, `maintenance`, `night_task`,
`phone_activity` (`backend/celery_app/tasks/`).

## 3. Wiring — every capability has a UI consumer

**W1's verdict** (`wave-v22/w1-wiring/WIRING-MATRIX.md`; read-only audit). The backend surface was
enumerated twice — AST over `backend/app/api/v1/*.py` and the live `/openapi.json` — with **0
missing / 0 extra**: **76 HTTP operations** at that read (the 77th surface is the WebSocket
`/api/v1/ws/activity`, absent from OpenAPI by design). V23 adds one further HTTP operation,
`GET /api/v1/ops/activity-publish`, which is WIRED on arrival (the `/usage` Worker-process card +
`v23-observability` pins), so the current tree is **78 surfaces: 75 WIRED · 3 API-only · 0
UNWIRED**. Per-row evidence (consumer `file:line`,
testid, jest/e2e pin) is in W1's matrix; these are its final counts:

| Verdict | At base `4f6a834` | Working tree at W1's read (HEAD + V22 writers) |
|---|---|---|
| **WIRED** (a shipped consumer + a test pin) | 73 | **74** |
| **API-ONLY-BY-DESIGN** (reason stated per row) | 3 | **3** |
| **UNWIRED (GAP)** | **1** (`GET /ads/campaigns/{id}/metrics`) | **0** |
| **GAP-DEADREAD** (reverse sweep, 19 pages) | 0 | **0** |
| Agent tools (4) | 4 WIRED to the capability surface | 4 |
| Celery task functions (17) | 15 WIRED · 2 API-only (retention, worker-log healthcheck) | 17 |
| Ops/dev endpoints (5: `/`, `/docs`, `/redoc`, `/openapi.json`, `/health`) | 1 WIRED · 4 API-only | 5 |

The single route that was UNWIRED at the base is now wired: `GET /api/v1/ads/campaigns/{id}/metrics`
has a consumer (`frontend/src/components/ads/CampaignMetrics.tsx:88`, mounted from
`AdCampaignCard.tsx`) and a test (`frontend/src/__tests__/v22-wiring-ads-metrics.test.tsx`), added by
L3 in this wave. **Process condition, not a code gap:** those files are uncommitted at this
write — the wave commit must include them or the route regresses to UNWIRED (W1 GAP-3).

W1's open findings in its final matrix, reconciled with L3's final receipt (10:16) — W1's TL;DR
predates the last closures, so where they disagree the working-tree code and L3's pins are the truth:

- **W1 GAP-1 — `X-Polsia-Total` headers.** W1 found **5 of 7 emitted totals never read** (invoices,
  memory, outreach, support, dashboard/activity). **Closed for 4 of 5 by L3**: `/invoices` now prints
  `Showing the first 100 of N` (`frontend/src/app/invoices/page.tsx:237`,
  `components/invoices/useInvoices.ts:32,55,79`), `/outreach` prints `100 of 137`
  (`app/outreach/page.tsx:60,95-97`), `/support` prints `50 of 137` (`app/support/page.tsx:51,101-103`),
  and `/memory`'s false "the API reports no total" sentence is replaced by the precise semantic-branch
  wording (`app/memory/page.tsx:482-487`) — **no header read there, because that branch serves none**.
  Remaining: **none — closed in V23 by L4.** `/dashboard/activity`'s header is now read on the same
  response as the rows and the feed header prints `N events` (`1 event` singular); an absent header
  draws no count (`frontend/src/hooks/useActivityFeed.ts:88-97,198-215,395`;
  `components/dashboard/ActivityFeed.tsx:147-163`; `v23-observability.test.tsx`). See §7 item 6.
- **W1 GAP-2 — invoice `line_items` had no UI.** Both halves are now closed by L3: display via a
  read-only `Items (N)` expander (`frontend/src/app/invoices/page.tsx:76-110`), entry via a repeating
  description/qty/unit-price editor that validates before POST
  (`components/invoices/InvoiceCreateForm.tsx:68-111,223-316`). Tests:
  `v22-wiring-invoice-items.test.tsx` (4) and `v22-wiring-invoice-create-items.test.tsx` (3).
- **W1 GAP-3 — (process) the mid-audit closures are uncommitted.** A single wave commit must include
  the 17 modified/new product files and 6 new test files L3 lists in its receipt §"Skipped". **Owner:
  parent.** Otherwise the route/totals gaps re-open.
- **W1 GAP-4 — dead UI components imported by nothing:** `components/dashboard/AgentStatusGrid.tsx`,
  `components/dashboard/MetricsCard.tsx`, `components/layout/TopbarPopover.tsx` (two have their own
  unit specs; no page mounts them). **Owner: parent** — L3 skipped deletion because the campaign's
  recorded no-delete rule pins two of them (`__tests__/u4-darktail.test.tsx:68-75`); retire-vs-keep
  is a recorded decision, not a wiring fix.
- **W1 GAP-5 — worker-side activity-publish counters are not visible in the UI.** Breaker counters are
  per-process; the usage panel reads the API process, and its copy said so
  (`frontend/src/app/usage/page.tsx:188-190`, `backend/celery_app/tasks/maintenance.py:210`).
  **Closed in V23 by L4:** the 15-minute tick publishes the worker's snapshot to the shared Redis key
  `polsia:activity-publish:stats` (TTL 900 s) and `GET /api/v1/ops/activity-publish` serves it; the
  `/usage` **Worker process** card shows the last published counters or an honest "not published yet"
  (`maintenance.py:215-260`, `ops.py:75-136`, `OpsPanel.tsx:298-349`). See §7 item 8.

W1's reverse sweep found **no page that discards a response body** (0 GAP-DEADREAD); the partial reads
it checked are deliberate (dashboard margin widget vs the full Finance page; Settings' single
`/config` read shared by every tab). Its route-parity check is exact: **0 frontend calls point at a
route the API does not serve**.

## 4. Honesty — no mock, fake, or demo in the product path

**W2's verdict** (`wave-v22/w2-honesty/MOCK-AUDIT.md`; read-only audit). One `grep -rInE` over
`backend/app`, `backend/celery_app`, `frontend/src/{app,components,lib,hooks}`, `scripts` and
`backend/scripts` found **96 hits in 61 unique files** (L6 re-counted W2's raw artifact; W2's prose
said 70 — the raw file is the truth). **95 of the 96 are individually triaged**; the one exception is
a comment line at `backend/app/agents/deepseek_runtime.py:201`, which reads as an anti-stub note, not
a product mock.

- **Zero product-path test doubles.** The only mock switch is `CLAUDE_CLI_MOCK`, env-gated (default
  `false`, CI-only via `docker-compose.ci.yml`), and it labels its own output
  (`status='partial'` + a note) rather than impersonating a model (`backend/app/agents/base_agent.py:576-604`).
- The remaining hits are anti-mock comments, HTML `placeholder=` attributes, or explicitly labelled
  fallbacks (the generated site package's contact slots).
- **No handler-less live control and no link to a missing route** across all 19 pages (W2's action
  map, §2 of its audit). Controls that cannot act are either not drawn or refuse by name in visible
  copy.

But the sweep found **9 defects (2 HIGH, 3 MEDIUM, 4 LOW)** — the honesty problem is not mock code,
it is **seeded constants presented as measurements** and a few false labels. **L3 closed the four
frontend defects (D3–D6) in this wave; the five backend/seed items (D1, D2, D7–D9) were then CLOSED
by the parent at settle** (see §7.1), leaving only the owner's live-row cleanup step; and all
owned by the parent:

| # | Sev | What the owner sees | Location | Owner |
|---|---|---|---|---|
| **D1** | **H** | Dashboard Business panel shows **MRR `$0.00`, Active customers `0`, Churn `0.0%`** as measurements. At the V22 audit no product **UI** wrote `company_config.kpis` (`PUT /config` accepted the key, but nothing in the UI sent it) and Settings had no KPI editor. **V23:** the Settings **KPI editor** now writes that key (item 10), so seeded values are correctable in-product. | `backend/scripts/seed_company.py:36-43` → `frontend/src/components/dashboard/BusinessPanel.tsx:124-126`; V23 `frontend/src/components/settings/KpiEditor.tsx` | **parent** (seed is backend; no V22 writer owns it) |
| **D2** | **H** | The compliance fields are **already set** in the live config, so the gate passes **today**; with `SANDBOX_MODE=false` every outbound email footer would print `Privacy: https://yourcompany.com/privacy` / unsubscribe links on a domain the owner does not own. Only sandbox mode stops it now. | `backend/scripts/seed_company.py:44` → `backend/app/services/email_compliance.py:240-264` | **parent** (seed) |
| **D3** | M | Daily Report says **"Planned 8 · Completed 0 · Failed 0"** with no "as of", while the dashboard says 27 completed today. The report is a 06:00 snapshot; `updated_at` is served and typed but never rendered. | `frontend/src/components/dashboard/DailyReportPanel.tsx:131-136`, `DocumentsOverlay.tsx:226-232` | **closed by L3** — `Updated <stamp>` now renders (`DailyReportPanel.tsx:153`, `DocumentsOverlay.tsx:240`), pin `v22-wiring-copy-truth.test.tsx` |
| **D4** | M | Settings → Emails helper says the requeued-run toggle **"sends nothing yet"** — false; it sends a real email. | `frontend/src/components/settings/EmailsSection.tsx:40-42` vs `backend/app/services/notification_service.py:113-116,190-200` | **closed by L3** — helper now states the shipped route (`EmailsSection.tsx:42`), pin `v22-wiring-copy-truth.test.tsx` + `u7-settings` |
| **D5** | M | Command palette says **"Search tasks, runs, reports…"** but reads only the newest 50/50/20; an older match answers **"No matches."** — a claim of absence. | `frontend/src/components/shell/CommandPalette.tsx:48-50,392,409` | **closed by L3** — persistent scope line from the same limit constants (`CommandPalette.tsx:58,471-477`), pin `v22-wiring-copy-truth.test.tsx` |
| **D6** | L | Bell says **"Notifications, 20 in the last 48 hours"** — 20 is the route's hard cap, not the window count. | `frontend/src/components/layout/NotificationsPanel.tsx:283`; `backend/app/api/v1/notifications.py:196-199` | **closed by L3** — "up to 20" at the cap (`NotificationsPanel.tsx:288`), pin `v22-wiring-copy-truth.test.tsx` |
| **D7** | L | Stale "INTENDED STATUS" comments claim the Memory page has no Add/Forget controls; both ship. | `backend/app/api/v1/memory.py:65-71,96-100` | **parent** (backend) |
| **D8** | L | `outreach_sending_domain` is declared with no reader. | `backend/app/config.py:179-182` | **parent** (backend) |
| **D9** | L | `chat_service.list_history` turns a `None` count into a fabricated `0` instead of a named refusal (the sibling reads refuse). | `backend/app/services/chat_service.py:804-811` | **parent** (backend) |

Three findings W2 made mid-audit were **fixed by V22 writers while it ran** and are recorded, not
queued: the `/tasks` false "API reports no total" line (now reads `X-Polsia-Total`, pin
`v22-wiring-totals.test.tsx`), the `/ads` page-length count (now "N of M campaigns"), and the run
detail's "tools never persisted" state (L1's migration/column/chips — see §5.3).

W2 also names capabilities with **no owner-facing control** (missing features, not false claims):
the daily cycle-report email toggle (`cycle_report_email`), a KPI/goals editor, and a human hourly
rate that would make gross margin computable. **All three were built in V23** — Settings' Emails
switch (item 9), the Settings KPI editor (item 10), and migration `0011` + `PUT /config` + the
Finance page's Human rate card (item 11); §7 records the receipts.


## 5. Proof — what has actually been run

### 5.1 Last settled gate (V21 frozen tree) and the V22 gate

| Gate | Result at V21 close | Evidence |
|---|---|---|
| Backend tests | **2715 passed / 0 failed / 0 errors / 13 skipped** | `wave-v21/v-verify/VERIFY.md:3`, `:141` |
| Coverage | **92.19%** on the full CI command (11,678 statements / 912 missed) ≥ floor **90** | same file `:141`, `:155-156`; floor at `backend/pyproject.toml:58` |
| ruff | `All checks passed!`, exit 0 | `wave-v21/v-verify/VERIFY.md:144` |
| mypy | exit 0, 148 files, hard gate in `ci.yml` (no `|| true`) | `wave-v21/v-verify/VERIFY.md`, V21 L1 receipt |
| jest (scoped) | 13 suites / 168 tests passed | `wave-v21/v-verify/VERIFY.md:145` |
| tsc | exit 0, 0 bytes of output | `wave-v21/v-verify/VERIFY.md:146` |
| e2e | toolbox spec **5/5 green**; full sweep **not run by L6** (served bundle was pre-V21) | `wave-v21/v-verify/VERIFY.md:153` |

On the V22 working tree, L4's single CI-exact unit run measured **92.10%** (11,711 statements / 925
missed), below its **92.3** ratchet bar, so `fail_under` remains **90**
(`backend/pyproject.toml:58`, byte-identical to V21; `wave-v22/l4-proof/RECEIPT.md` §5).

### 5.2 One-command acceptance

`scripts/smoke.sh` runs **13 GET/HEAD-only checks** (`--dry-run` prints `13 checks planned, 0 requests
made`, `scripts/smoke.sh:206`): `health`, `headers`, `auth-401`, `auth-200`, `capabilities`,
`phone-summary`, `phone-media`, `phone-codes`, `send-bridge`, `docs`, `api-docs`, `server-header`,
`backups`. The last recorded live run of the 12-check script was **10 PASS / 2 named SKIP / 0 FAIL,
exit 0** (`wave-v19/l5-smoke-docs/raw/live-smoke.txt`); V21's `api-docs` check was proven to move
SKIP → FAIL on a stale proxy and a **green 13-check run is a settle check** on the rebuilt stack
(`wave-v21/l5-docs-smoke/raw/api-docs-branches.txt`).

### 5.3 V22 lane proofs (receipts on disk)

| Lane | What it changed | Proof quoted from its receipt |
|---|---|---|
| **L1 — tools persistence** | `agent_runs.tools_used` (migration `backend/alembic/versions/0010_agent_run_tools_used.py`, additive + reversible); envelope → `accounting.usage_from_envelope` → `agent_tasks._apply_run_usage` → column → `GET /api/v1/runs/{id}` → `/runs/[id]` three states. Cap 50 with a named marker; NULL ≠ `[]`. | RED `ImportError` → GREEN **129 tests** in the focused backend run (11 new); FE **15/15**; `tsc` 0 bytes; mypy **162 files** clean; migration proven upgrade/downgrade on SQLite and offline PG SQL. `wave-v22/l1-tools-persist/RECEIPT.md` |
| **L2 — phone thread view** | New in-page `frontend/src/components/phone/PhoneThreadView.tsx` behind an **Open conversation** action; the base page already had a `?thread=` deep-linked conversation read (V16), so V22 adds the panel without a route change. `parseMessages` now tolerates the store's legal `when:null` — the real contract gap. Fetch-on-select only; no storage/console. | RED **12 failed / 2 passed** → GREEN **14/14** new pins; regression **7 suites / 122 tests**; scoped `tsc` 0 bytes; one read-only live GET confirmed the payload shape (values withheld). `wave-v22/l2-phone-thread/RECEIPT.md` |
| **L3 — wiring fixes** | 13 rows closed: ads metrics expander; invoice item display + entry; `X-Polsia-Total` for invoices/outreach/support/tasks/ads; memory wording; W2 D3–D6 copy/state fixes. | **15 suites / 209 tests** + **10 suites / 97 tests** green; `tsc` 0 bytes; only the pre-existing `PhoneMedia.tsx` `<img>` lint warning. `wave-v22/l3-wiring-fix/RECEIPT.md` |
| **L4 — e2e + coverage** | `e2e/v22-run-chips.spec.ts` (5 tests) and `e2e/v22-phone-thread.spec.ts` (5 tests), both enumerating; one CI-exact coverage run. | Specs **RED on the stale served FE** (3 failed / 2 vacuous passes; 1 failed on the missing `phone-thread-open`) — the assertions bite and the stubbed envelopes are wire-valid. Coverage **92.10%** (11,711 statements / 925 missed) → **below the 92.3 bar, so `fail_under` stays 90** and `backend/pyproject.toml` is byte-identical (`8a15b56b…`). `wave-v22/l4-proof/RECEIPT.md` |

### 5.4 The V22 verifier gate — PASSED on the frozen tree (L6)

`wave-v22/v-verify/VERIFY.md` records one gate run on the frozen tree (no backend/FE product file
changed during it):

| Gate | Result |
|---|---|
| Backend unit + parity + redteam | **2,726 tests, 0 failures, 0 errors, 13 skipped** (681 s, junit XML) |
| Jest (scoped, 23 suites incl. every V22 suite) | **318 tests passed** |
| `tsc --noEmit` | exit 0, **0 bytes** |
| ruff | **All checks passed** |
| Coverage | **92.10%** (11,711 / 925) ≥ floor 90; no ratchet |

Per-lane verdicts: **L1 PASS-WITH-NOTES · L2 PASS · L3 PASS-WITH-NOTES · L4 PASS · L5 PASS** (after
the three claim corrections it flagged) **· W1 PASS** (77/77 rows + a 21-row sample) **· W2
PASS-WITH-NOTES**. The verifier's own probes: migration 0010 apply/downgrade/re-upgrade on scratch
SQLite, the three run-detail states over ASGI, `parseMessages` shape/`when:null`/refusals, and a live
thread-scoped read. **Still unsettled:** the wave is uncommitted, the served FE is pre-V22 (the two
new e2e specs are RED on it by design), the W2 artifact needs PII redaction, and CI has not run.

## 6. Owner-gated — real-world credentials/decisions, not code gaps

These cannot be closed by a commit; each is a deliberate owner action named in `NEXTSTEPS.md` §4.

1. **Stripe live key** — the burned live key was expired; real payments need a NEW restricted live key
   (`NEXTSTEPS.md` §4, "Production Stripe"). The test key + webhook secret are wired in `.env`
   (untracked).
2. **API-key rotation (F-02 residual)** — the stack is loopback-bound (`127.0.0.1:80`), so the measured
   exposure is closed; the shipped default `API_KEY` must be rotated **before** publishing beyond
   localhost, with `NEXT_PUBLIC_API_KEY` matching and a frontend rebuild (`README.md` "Known gaps",
   `NEXTSTEPS.md` §4).
3. **Website URL** — `website_url` is still the seed placeholder `https://yourcompany.com`; set the real
   domain in Settings → Company (unsubscribe links depend on it) (`NEXTSTEPS.md` §4,
   `frontend/src/app/settings/page.tsx:198`).
4. **Real SMS pairing / send proof** — `POST /api/v1/phone/send` is policy-gated, off by default, and
   the live send needs the owner's paired Google Messages browser (or the host bridge with a ≥24-char
   token and a deliberate LAN bind). Refusals by name already cover unpaired/unavailable/sandbox;
   the real send is owner-run (`USAGE.md` §5.2, `NEXTSTEPS.md` §4).

## 7. Every remaining item

**The honest answer: MYPOLSIA is not "fully complete" at this write.** Every backend capability has a
UI consumer or a stated API-only reason (V22 W1: 74 WIRED / 3 API-only / 0 UNWIRED across 77 surfaces;
V23 adds the 78th surface, `GET /ops/activity-publish`, WIRED on arrival — 75 / 3 / 0), and there is
**no mock or fake in the product path** (W2: zero product-path test doubles in 96 triaged hits). What
remains is listed here — nothing is softened, and nothing is claimed fixed without a receipt.

### 7.1 Owner-visible honesty defects (backend/seed; all closed at settle)

| # | Item | Evidence (`file:line`) | Owner |
|---|---|---|---|
| 1 | **D1 (HIGH) — CLOSED at settle.** The seed now writes `kpis={}` (was six fabricated zeros rendered as measurements); pinned by `backend/tests/unit/test_v22_seed_truth.py`. **Remaining owner step:** your live DB was seeded before the fix — run `python backend/scripts/clear_seeded_placeholders.py` (dry run) then `--apply` to clear the exact placeholder dict from the existing row. The KPI **editor** is now shipped too (item 10, V23). | `backend/scripts/seed_company.py` (`default_company`), `backend/scripts/clear_seeded_placeholders.py`, `test_v22_seed_truth.py` | **done (parent)** |
| 2 | **D2 (HIGH) — CLOSED at settle.** The seed now writes `website_url=""`, so the compliance gate refuses BY NAME (`E_CONFIG`, `missing: [website_url]`) until a real domain is set; pinned by the same test. **Remaining owner step:** the live row still carries the placeholder — same cleanup script clears it; then set your real URL in Settings → Company. | `backend/scripts/seed_company.py`, `email_compliance.py:221-264`, `test_v22_seed_truth.py` | **done (parent)** |
| 3 | **D7 (LOW) — CLOSED at settle.** Both stale "API-only / no Add-Forget UI" docstrings corrected (the page has shipped Add + Forget since V14). | `backend/app/api/v1/memory.py` docstrings | **done (parent)** |
| 4 | **D8 (LOW) — CLOSED at settle.** The never-read `outreach_sending_domain` field and its `.env.example` line are deleted (`extra="ignore"` keeps any leftover env var harmless). | `backend/app/config.py`, `.env.example` | **done (parent)** |
| 5 | **D9 (LOW) — CLOSED at settle.** `list_history` now refuses `E_DB` by name when the count scalar is None (mirrors `run_service.count_runs`); pinned. | `backend/app/services/chat_service.py`, `test_v22_seed_truth.py` | **done (parent)** |

### 7.2 Wiring / observability items

| # | Item | Evidence (`file:line`) | Owner |
|---|---|---|---|
| 6 | **CLOSED (V23 · L4).** The feed header now prints the server's own total — `N events` (`1 event` singular) — read from the same response's `X-Polsia-Total` through the shared `onHeaders` hook; an absent or non-integer header draws **no count** (never the rows on hand). A malformed snapshot cannot publish a stale/foreign count. | `frontend/src/hooks/useActivityFeed.ts:88-97,198-215,395`; `frontend/src/components/dashboard/ActivityFeed.tsx:147-163`; pin `frontend/src/__tests__/v23-observability.test.tsx` | **done (V23 L4)** |
| 7 | Three components are imported by nothing: `AgentStatusGrid`, `MetricsCard`, `TopbarPopover` (two have their own unit specs). Retire-vs-keep is a recorded decision — the campaign's no-delete rule pins two of them. | `__tests__/u4-darktail.test.tsx:68-75`; `components/dashboard/*`, `components/layout/TopbarPopover.tsx` | **parent (retire decision)** |
| 8 | **CLOSED (V23 · L4).** The 15-minute maintenance tick writes `{failures, skipped_while_open, breaker_open, ts}` to the shared Redis key `polsia:activity-publish:stats` (TTL 900 s; a Redis failure is logged and never fails the job), and `GET /api/v1/ops/activity-publish` serves it: `{data:{…}}` / `{data:null}` when the worker never wrote / **503 `E_REDIS`** when Redis is unreachable or the stored value is not the frozen shape (no coercion of `"2"`/`true` into counts). The `/usage` **Worker process** card renders the four counters or the honest absence ("No counters are shown rather than fabricated zeros"); the "worker invisible" copy is gone. | `backend/celery_app/tasks/maintenance.py:215-260`; `backend/app/core/redis_client.py:84-110`; `backend/app/api/v1/ops.py:75-136`; `frontend/src/components/usage/OpsPanel.tsx:298-349`; pins `backend/tests/unit/test_v23_breaker_stats.py` (8), `frontend/src/__tests__/v23-observability.test.tsx` (7) | **done (V23 L4)** — the live tick/route read is the parent's (RELEASE-CHECKLIST V23) |

### 7.3 Capabilities that had no owner-facing control (all closed in V23)

| # | Item | Evidence (`file:line`) | Owner |
|---|---|---|---|
| 9 | **CLOSED (V23 · L1).** Settings → Emails carries a **cycle-report email** switch that reads `GET /config` → `cycle_report_email` and writes `PUT /config {"cycle_report_email": bool}`. It awaits the server's own response — the switch does not move while in flight (`aria-busy`, disabled), and a response that disagrees with the request wins; a refusal keeps the old state and names the error inline through the shared `readErrorText`. Copy states the shipped route: one email per morning and evening cycle report to the configured Notification Email, after the report is saved, skips/failures recorded in the activity feed. | `frontend/src/components/settings/EmailsSection.tsx`; copy provenance `backend/app/services/notification_service.py:287-352`, `backend/celery_app/tasks/daily_cycle.py:687,833`; pin `frontend/src/__tests__/v23-cycle-email.test.tsx` (8 tests) | **done (V23 L1)** |
| 10 | **CLOSED (V23 · L2).** Settings carries a **KPI editor** (`KpiEditor`, below the form card) for the six keys `mrr_usd`, `active_customers`, `churn_rate`, `cac_usd`, `ltv_usd`, `nps`: numeric entries saved through one `PUT /config {"kpis": {…}}`. A blank box **removes** the key (never sent as `0` — the V22 honesty law), the editor's stored-note line shows `—` for absent keys, Save reflects the server's returned object (not the typed value), clear-all writes `{}`, and a refusal prints "KPI save refused" + `code: message — fix` inline. The page's single `GET /config` seeds it; the editor fires no read of its own. | `frontend/src/components/settings/KpiEditor.tsx`; `frontend/src/app/settings/page.tsx`; pin `frontend/src/__tests__/v23-kpi-editor.test.tsx` (9 tests) | **done (V23 L2)** |
| 11 | **CLOSED (V23 · L3).** `company_config.human_hourly_rate` (nullable `Float`, no default) via migration **0011** (additive, reversible, chain `0011 ← 0010`, single head; real SQLite upgrade → downgrade → re-upgrade proven). `PUT /config` accepts it (`ge=0`, `null` clears, NaN/±Infinity refused by name) and `GET /config` serves it. `finance.py`'s planning view reads the stored rate instead of the hard-coded `None`, so margin computes when price, AI cost (needs capacity), human hours and the rate are ALL measured; rate alone leaves margin null **with** reasons (never a fake margin). The Finance page's **Human rate** card shows / sets / clears it, always reflecting the server's response, with a named refusal. | `backend/alembic/versions/0011_company_human_rate.py`; `backend/app/api/v1/finance.py` (`build_planning_view`); `backend/app/services/company_service.py` (`measured_human_hourly_rate`); `backend/app/schemas/company.py`; `frontend/src/components/finance/HumanRateCard.tsx`; pins `backend/tests/unit/test_v23_human_rate.py` (7), `frontend/src/__tests__/v23-human-rate.test.tsx` (7) | **done (V23 L3)** — the PostgreSQL live apply is the parent's (RELEASE-CHECKLIST V23) |

### 7.4 Process and settlement (must happen for the above to be real)

| # | Item | Evidence | Owner |
|---|---|---|---|
| 12 | **Committed.** The wave is in `cc3be4e` (65 files): L1's migration 0010 + model/service/API + chips, L2's `PhoneThreadView`, L3's 17 product files + 6 FE test files (incl. `lib/api.ts`'s additive `onHeaders`), plus the parent's D1/D2/D7–D9 fixes and this file. | `git show --stat cc3be4e` | **done (parent)** |
| 13 | `backend/tests/parity/*.py` header comments narrate the V21 record — reviewed at settle: they date the route accurately (V21 L2 owned it); no stale claim about persistence remains in them. No change needed. | reviewed by parent | **done (parent)** |
| 14 | **L4 (e2e + coverage) — DONE ✅ and now green live.** Parent rebuilt FE+BE and ran the sweep: full Playwright **43 passed / 8 skipped / 0 failed**, including the 10 V22 specs (run chips 5, phone thread 5). Coverage 92.10% → `fail_under` stays **90**. | `wave-v22/v-final/e2e-full2.txt` | **done (parent)** |
| 15 | **L6 verifier gate — PASSED ✅ on the frozen tree.** 2,726 tests / 0 failed / 0 errors / 13 skipped (parent's authoritative re-gate after settle: **2,729 / 0 / 0 / 13**, the +3 being the new D1/D2/D9 pins); full jest **839/839**; tsc 0; ruff 0; coverage 92.10% ≥ 90. | `wave-v22/v-verify/VERIFY.md`; `v-final/gate-final.xml`, `jest-full.txt` | **done** |
| 16 | **Live settle DONE.** Images rebuilt and recreated; nginx reloaded; live DB migrated **0008 → 0009 → 0010**; `make smoke` **PASS 13 checks**; `/apidocs` + `/openapi.json` 200; 404 page live; `/runs/{id}` honest three-state; the seeded placeholders cleared from the live row (`kpis {}`, `website_url ""`). CI watch pending (runners queued at push time). | `v-final/` evidence; live probes | **done (parent)** |
| 17 | **PII redacted** in the W2 audit artifact (`<owner name: redacted>` / `<owner postal address: redacted>`); repo-wide re-grep found no live PII in tracked content. | `wave-v22/w2-honesty/MOCK-AUDIT.md`; grep | **done (parent)** |
| 18 | **V23 settlement (in flight at this write; no commit yet).** The four V23 lanes are implemented, pinned and receipted (`wave-v23/l1-cycle-email`, `l2-kpi-editor`, `l3-human-rate`, `l4-observability`). **L6 gate #2 PASSED on the frozen tree (13:01):** backend **2,745 tests / 0 failed / 0 errors / 13 skipped**, jest **23 suites / 278 tests**, `tsc` 0 bytes, ruff clean; verdicts **PASS** (L1, L2, L3) / **PASS-WITH-NOTES** (L4 and L5 — LOW traceability only). Gate #1's 2 parity failures were the parent's two additive rows for `GET /api/v1/ops/activity-publish`, audited by L6 and re-gated; L6's V3 (`useActivityFeed.ts:390` → `:395`) is fixed in this file. Required before V23 is real: rebuild backend + frontend; `make migrate` to the single head **0011**; the RELEASE-CHECKLIST V23 live checks; commit/push/CI. | `wave-v23/*/RECEIPT.md`; `wave-v23/v-verify/VERIFY.md`; `RELEASE-CHECKLIST.md` §5 (V23 block) | **parent** |

### 7.5 Known residuals already documented in the repo (pre-date V22, still true)

- `AdMetricRow` carries no `revenue_usd`, so a persisted ads row's `roas` reads the column's 0 default
  until the CLI schema gains the field (`README.md` "Known gaps"; `backend/app/schemas/agent_outputs.py:307-316`).
- `GOOGLE_ADS_CLIENT_SECRET` is RESERVED with no reader; `CELERY_WORKER_CONCURRENCY` is compose-owned
  (`README.md` "Known gaps", D14 decision).
- Channels with no counting source (github, google_ads, meta_ads, stripe, render) refuse a configured
  send cap by name (`POLSIA-E114`, `cap_unknown`) instead of reading it as zero sends; LinkedIn has no
  send adapter (`README.md` "Known gaps").

**Nothing above is hidden or re-labelled as done.** With V23 the product asks are built: items 6, 8,
9, 10 and 11 now have shipped controls/readers and focused pins (§7.2–7.3). The shortest path to
"complete" in the owner's sense is now: **settle V23** (L6 gate, rebuild, apply migration `0011`, the
RELEASE-CHECKLIST V23 live checks, commit/CI — §7.4 item 18) and clear the remaining parent/owner
items — the three dead components' retire decision (item 7), the §7.1 live-row cleanup, and the
owner-gated credentials in §6. No product-feature ask named by W2 §5 remains open; what is left is
process, labels/decision, and owner-gated credentials.


## 8. How to re-verify this statement

1. `make smoke` — 13 GET/HEAD checks against the running stack.
2. `cd backend && python -m pytest tests/unit tests/parity tests/redteam -q --cov=app` — must be
   ≥ `fail_under = 90` (`backend/pyproject.toml:58`).
3. `cd frontend && npx tsc --noEmit && npx jest` — 0 bytes of type output; suites green.
4. `cd e2e && npx playwright test --reporter=list` — the live UI sweep.
5. Read `wave-v22/w1-wiring/WIRING-MATRIX.md`, `wave-v22/w2-honesty/MOCK-AUDIT.md`, and
   `wave-v22/v-verify/VERIFY.md` for the per-row truth behind §3, §4, and §7; the five V23 closures in
   §7.2–7.3 cite `wave-v23/{l1-cycle-email,l2-kpi-editor,l3-human-rate,l4-observability}/RECEIPT.md`.
