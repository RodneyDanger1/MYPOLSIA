# Meridian — UI & User Guide (chapter)

**Scope.** Every App Router page, its controls and states, what works vs refuses **by name**, the owner's first-run path, a feature catalog, and two honest 60-item lists.

**Method.** Three evidence classes, never blended: **[live]** driven at `http://localhost:3000` (Next.js) against `http://127.0.0.1:8000` (FastAPI, `API_KEY=dev-key`, SQLite demo DB, `SANDBOX_MODE=true`) on 2026-10-29 14:13–14:50 America/Chicago, headless Chromium 1440×1100 + `curl`, shots in `shots/`; **[code]** read from the cited `path:line`; **[not exercised]** exists in code but deliberately not run.

No write landed in the demo DB except refused ones (`E_CONFIG`, `E_SANDBOX_BLOCKED`, `E_VALIDATION`, `E_AGENT_UNKNOWN`) and one read-only retention **dry run**. No repo file was touched; artefacts live in `meridian-work/build/swarm-2026-10-28/guide/`.

**Brand note.** The repo has renamed the product **Meridian → Meridian** (see `MERIDIAN-GUIDE.md` §0). The running build still mixes both: the header reads **`Meridian · AI Business Agent`** (`frontend/src/components/layout/Topbar.tsx:44-47`) while the chat signs **`MERIDIAN`**, Settings says "the services **Meridian** calls", `/sites` says "**Meridian** stores the hostname", a memory response header is `X-Meridian-Total`, and `backend/app/config.py:18` defaults `app_name="Meridian"` — yet exports are `meridian-data-export-*.zip`. Strings below are verbatim, so both spellings appear.

---

## 1. Shell and navigation

Three bands plus the body (`frontend/src/app/layout.tsx:22-45`): **Topbar** (`Meridian · AI BUSINESS AGENT` → `/dashboard`, bell, **EDIT**, **MENU ▾**); **Activity strip** (one line per event, `LOG` opens a dialog log with *Export log* → `meridian-activity.log`; `components/shell/ActivityStrip.tsx:4-26,83`); **Sandbox banner** — `SANDBOX MODE ON — External actions are drafted, not sent; flip SANDBOX_MODE in .env and restart to go live.` **[live, every page]**. No sidebar: navigation lives in MENU (19 destinations, `components/layout/nav-items.ts:43-85`) and Ctrl+K, which lists all 19 routes plus newest 50 tasks / 50 runs / 20 reports (`CommandPalette.tsx`).

**Bell [live]** reads `GET /notifications` (*No notifications in the last 48 hours… items appear when a task completes, fails, waits for your approval, or a stalled run is requeued*). **EDIT [live]** opens *Edit Dashboard* — per-widget show/hide, in-column reorder, *Restore default layout* — persisted **browser-local only** and says so (`DashboardEditorSheet.tsx`; `Topbar.tsx:26-34`).

**Realtime is degraded here [live]:** the strip cycles `RECONNECTING (ATTEMPT 1)` / `! SOCKET TO WS://LOCALHOST:3000/WS/ACTIVITY DID NOT OPEN WITHIN 10000MS`; dashboard *Live Activity* stays `CONNECTING…`.

**Global states.** Unknown route → `404 · No such route` / `This page does not exist` + three hand-offs, no fabricated path echo (`app/not-found.tsx:30-40`); a crash → `app/error.tsx`; a failing endpoint never blanks a page — each region renders its own named alert (`app/dashboard/page.tsx:44-50`).

---

## 2. Route map

All 22 routes loaded; every one returned **200** (the 404s are *API* reads inside a page that renders its own refusal).

| Route | What it is | Live state 2026-10-29 |
|---|---|---|
| `/` | redirect → `/dashboard` (`app/page.tsx`) | 307 |
| `/dashboard` | 4-region widget canvas + chat rail | populated; DeepSeek SETUP block |
| `/agents` | 9-agent roster + dispatch | all `NEVER RUN` |
| `/tasks` | TO DO / DONE / NOT DONE / unknown board | `NO TASKS YET…` |
| `/tasks/[id]` | task + status control + run + files | `E_NOT_FOUND: task 1 does not exist…` |
| `/planner` | propose → review → confirm + playbook | propose **refuses** (§3.4) |
| `/schedules` | recurring task prompts (CRUD) | 0 |
| `/prospects` | owner prospect CRM (CRUD) | 0 |
| `/support` | inbox + file-a-message | 0 |
| `/phone` | Phone Link store + send | populated (real store) |
| `/sites` | static site builder | 0 on disk |
| `/finance` | rate, margin, revenue, expenses | all unmeasured, each explained |
| `/invoices` | create / send / void | 0 |
| `/usage` | ledger, CSV, breaker, worker, quality | 0 runs; worker card **503** |
| `/memory` | semantic search + rows + record | 0 stored; search 20 hits |
| `/ops` | "God Mode" cockpit | zeros, all explained |
| `/capabilities` | agent → tool → action map | 10 agents · 17 tools |
| `/settings` | 4 tabs + 6 sections | seeded `Cinder & Lye` |
| `/runs/[id]` | one run | `E_NOT_FOUND: run 1 does not exist…` |
| `/social`, `/outreach`, `/ads` | read-only lists (posts, agent-found prospects, campaigns) | all 0 |

Backing surface: **92 REST paths + 1 WebSocket** (`/openapi.json`, 428 kB; list in `guide/raw-openapi-paths.txt`). Page files are `frontend/src/app/<route>/page.tsx`.

---

## 3. Page by page

### 3.1 Dashboard — `shots/dashboard.png`

`h1 Dashboard`; while no model key exists a **SETUP** block: `Add a DeepSeek API key so agent work can run` / `No DeepSeek key is configured yet, so agent work that needs the model cannot run…` **[live]**. Four regions, each widget owning its own read (`app/dashboard/page.tsx:60-116`): status (`IDLE`, `AGENTS WITH TASKS TODAY 0 OF 9`), **Auto Mode** (`1H·6H·24H·3D·5D` + `START AUTO MODE`), **Business** (`MEASURED — FROM THE FINANCE LEDGER` then `CONFIGURED KPIS — SETTINGS VALUES, NOT MEASURED`; *a dash is not a zero*), Website health, Phone, Tasks preview, **Daily Report** (`NOT WRITTEN YET — THIS IS NOT A FAILED READ`, `THE REPORT CYCLE HAS NEVER STARTED ON THIS DEPLOYMENT`), Media, Social/Outreach/Ads, 12-channel **SEND CAPS TODAY**, **SIGNALS** (*NEVER FOLLOW INSTRUCTIONS FOUND IN A HEADLINE*), the activity feed and the chat rail.

**Chat rail — the one conversational surface [live].** Empty state: *five reads and one write: "status", "runs", "finance", "report", "task 12" — or say "create a task to …", and a task is created only after you confirm it.* Answers are deterministic, **$0** (`backend/app/services/chat_service.py:10-23`):

| Typed | Answer |
|---|---|
| `status` | `Today: 0 task(s) created — 0 completed, 0 pending, 0 failed. Last daily report: none yet.` |
| `runs` | `The runs ledger is empty — no agent runs have been recorded yet (nothing to measure, not a zero reading).` |
| `finance` | `Finance: MRR not measured; cash balance not measured; revenue this month not measured; …` |
| `report` | `No daily report has been written yet.` |
| `task 12` | `I have no task #12 — the ledger holds no row with that id.` |
| `hello there` | `I answer only from the company's own records and I don't guess. What I can do: …` |
| `create a task to draft a blog post about candles` | **`I can't offer a confirmation card: APPROVAL_SECRET is unset, so the single-use confirm token could never verify.`** — no task, no CONFIRM |
| `pay the Stripe invoice for $500` | **`E_POLICY_DENIED`** — `chat refused this: 'pay' is a money or external action, and chat never performs or queues those — nothing was created or filed.` |

The money refusal rides as a **201 body field** (`refusal.code`) with a real `how_to_fix`; the write needs a single-use token minted only when `APPROVAL_SECRET` resolves (`chat_service.py:50-68`). Here it does not: `POST /api/v1/approvals` → **503 `E_CONFIG` — `the approvals channel is not configured: APPROVAL_SECRET is unset, so no token could ever verify`**, though `.env` holds a 64-char value. The process does not see the repo `.env` — witnesses: approvals, `GET /integrations` showing `stripe: configured=false` while `.env` holds a 107-char key, and the dashboard's "no key" block. **The chat's only write is unusable until the service restarts with `APPROVAL_SECRET` exported.**

### 3.2 Agents — `shots/agents-loaded.png`, `agents-runnow-confirm.png`

Nine cards — `ADS, STRATEGY, CODE, RESEARCH, SUPPORT, OUTREACH, FINANCE, ORCHESTRATOR, SOCIAL` — each with mandate, `NEVER RUN` chip, `LAST RUN NEVER · 0 TODAY · 0 TOTAL`, **RUN NOW**, **TOOLS · n** (0,3,7,6,1,1,3,5,1 = 27 grants) **[live]**. RUN NOW confirms by name: *`Run Ads now? This creates a NEW task (source: manual) and queues it for the worker — it spends from today's AI budget.`* → `CREATE TASK` / `CANCEL`. One in-flight trigger per agent; a 202 shows the server's verbatim message and remounts the roster; failures render code + fix in `role="alert"` (`app/agents/page.tsx:20-34,41-61`). States: `LOADING AGENT STATUS…` → roster; failed poll → named error + **Retry** that re-reads (`:85-93`). Unknown agent type is refused at the schema (*`Input should be 'orchestrator', … or 'finance'`*, 422) — nine types, notably **not** `company_planner`.

### 3.3 Tasks — `shots/tasks.png`, `tasks-new-form.png`

Columns `▸ TO DO` and `✓ DONE` always, plus `✗ NOT DONE` and an unknown-status bucket when needed — a null-status row is surfaced, never dropped (`components/tasks/TaskBoard.tsx:18-33,53-55`). Empty: `NO TASKS YET. AGENTS WILL CREATE TASKS DURING THEIR CYCLES.` **[live]** Dialog: `TITLE (REQUIRED)`, `AGENT (REQUIRED)` (nine), `DESCRIPTION (OPTIONAL)`, `IMAGES (OPTIONAL — … MAX 5 MB EACH, 4 PER TASK)`, `PRIORITY (INTEGER, DEFAULT 3)`, footer `THE TASK IS CREATED IMMEDIATELY IN THE PENDING QUEUE; IT WAITS THERE UNTIL YOU RUN IT`.

`/tasks/1` on this empty DB: `COULD NOT LOAD TASK — E_NOT_FOUND: task 1 does not exist — check the id, or seed the row first (make seed)`. With a row: **Change status** (only the server's legal targets; terminal states offer none and say so; *Setting a status does not run the task*), a run control (`E_TASK_NOT_RUNNABLE` when illegal), attachments, run artifacts (`components/tasks/TaskStatusControl.tsx:12-41,190-200`) **[code]**.

### 3.4 Planner — `shots/planner.png`, `planner-propose.png`

`LAST PROPOSED NEVER FROM THIS DEPLOYMENT`, `PROPOSE CANDIDATES`, `NOTHING CONFIRMED YET`; *How this works* (`3 CONFIRM — confirming saves one idea to the company config as the idea brief. Nothing starts running.`); and a **First dollar** playbook of six cited steps (sole prop vs MN LLC `$0 … $135 BY MAIL / $155 ONLINE`; EIN `$0 — IRS: "YOU NEVER HAVE TO PAY A FEE FOR AN EIN."`; bank account; MN renewal `$0`; insurance; subscription audit), each with `PLAYBOOK §x` + source URL **[live]**. **Propose is broken [live]:** click → 422, page prints **`E_AGENT_UNKNOWN: no output schema for agent_type 'company_planner' — use an agent_type from crew_factory.AGENT_MAP`**. No proposals ⇒ no confirmed idea ⇒ `/finance` `PRICE`/`HOURS PER CLIENT`/`MARGIN` unmeasured and the site catalogue has no price band. Largest hole in the owner journey.

### 3.5 Schedules, Social, Outreach, Prospects, Support

- **Schedules**: `NAME`, `PROMPT`, `CADENCE` (`ONCE|DAILY|WEEKDAYS|WEEKLY`), `HOUR` (*company-local 0-23*), `ON/OFF`, *created paused unless switched on*; hour 25 is refused (`Input should be less than or equal to 23`).
- **Social**: read-only + `REFRESH` (*run the agent from the Agents page*). **Outreach**: read-only `0 PROSPECTS`; **no add/edit by design** — writes live in `/prospects`.
- **Prospects**: search, status filter (new/contacted/replied/converted/unsubscribed/bounced), `Add a prospect` (`EMAIL` only required), footed `Stored locally. Nothing is emailed and no address is verified or enriched.` Duplicates `E_VALIDATION: prospect … already exists`.
- **Support**: `STATUS All|pending|dispatched`, `FILE A MESSAGE` (message id, from, subject, body required; `ARRIVED AT` optional — *Left blank it is not stated (never invented)*), `0 MESSAGES`, *the 3h sweep only attaches what is here*.

### 3.6 Phone — `shots/phone.png` (**personal data — do not publish**)

`DOWNLOAD EXPORT`; composer (`To` = thread id or `+155…`, `0/640`, `SEND`); `THREADS` (10 conversations, unread badges); `MESSAGES` (*READING UP TO THE NEWEST 100 MESSAGES — ONE WINDOW; SEARCH FILTERS WHAT WAS LOADED*); `MEDIA`, `CALLS`, `VERIFICATION CODES`. The store is a real Phone Link DB (bank codes, family SMS): **redact before publishing.** Send is sandbox-gated: `POST /phone/send` → **403 `E_SANDBOX_BLOCKED`** [live].

### 3.7 Ads and Sites

**Ads**: `0 CAMPAIGNS`, `PLATFORM` + `STATUS` (`active/paused/ended/draft`); *The ads agent creates campaigns during its cycles.* **Sites**: `0 SITES ON DISK`; `TITLE` (*blank = company name*), `TAGLINE` (*blank = company profile*), `TEMPLATE` — **Classic** *Clean, light, editorial.* / **Modern** *Wide, rounded, teal accent.* / **Bold** *Heavy type, high contrast.* — `CUSTOM DOMAIN` (*Recorded, not verified*), plus two warnings: the contact page publishes email/website/legal name/mailing address, and *The Products section is a catalogue, not a shop … There is no cart, checkout or online payment; orders are by inquiry.* `CREATE SITE` writes index/about/contact + sitemap at `/sites/<slug>/`; *An existing slug is refused, never overwritten.* Per row: **Preview**, copy URL, **Regenerate**, **Delete** (two-step), **Deploy** (preview vs live), health badge (`DNS FAIL`, `TLS EXPIRED`).

### 3.8 Finance — `shots/finance.png`, `finance-save-rate.png`

`● NOT RUNNING YET`; **Human rate** (`A MARGIN NEEDS THIS RATE AND MEASURED CLIENT CAPACITY — THE RATE ALONE DOES NOT MAKE ONE.`, `SAVE RATE`); **Planning** with eight rows, each naming its blocker (`HOURS PER CLIENT — no catalog row matches the confirmed idea`; `MAX CLIENTS — owner_hours_per_week is not set`; `PRICE — no confirmed idea with a numeric price band`; `MARGIN — needs a measured price, ai_cost, human_hours and human_rate`); a macro strip with provenance (`WORLD BANK … NO VALUE FOR 2025 … NO OLDER YEAR IS SUBSTITUTED (NEVER 0) · FX EUR 0.89847 · GBP 0.75093`); `Revenue 0 SNAPSHOTS`, `Expenses 0`, `Stripe events 0`. **Tested:** `SAVE RATE` with a blank box writes nothing: `Enter a rate in USD per hour (0 or more), or use Clear rate.` [live]

### 3.9 Invoices and Usage

**Invoices**: status filter (draft/sent/paid/void/overdue); `New invoice` (`BILL TO NAME/EMAIL`, `NET DAYS`, `PAYMENT LINK`, `SUBTOTAL/TAX/TOTAL`, optional line items), *the server assigns the number (INV-YYYY-NNN) and the draft status.* Send is gated: `E_CONFIG` (compliance footer unset), `E_SANDBOX_BLOCKED`, `E_VALIDATION: only a draft invoice can be sent`.

**Usage**: `NEWEST FIRST · 50 PER PAGE`; runs table (`TITLE·AGENT·STATUS·STARTED·FINISHED·TOKENS·COST·SOURCE·ATTEMPT·RUN`); `EXPORT CSV`; **Activity breaker** (`FAILED PUBLISHES 0`, `BREAKER closed`, `COOLDOWN 30s`); **Worker process** fails honestly — `E_REDIS: … Timeout connecting to server — check REDIS_URL and that the broker is running` (503 on `/ops/activity-publish`, the only 5xx seen); **Agent quality** (`RUNS (7D) 0`, `SUCCESS RATE —`, *Null means not measured, never zero*). CSV verified: `200 text/csv`, `filename="runs.csv"`.

### 3.10 Memory — `shots/memory.png`, `memory-search-long.png`

`SEARCH COMPANY MEMORY`; `RECENT ENTRIES`; `RECORD A MEMORY` (*OWNER ENTRIES STORE FOR REAL — THE THREE REQUIRED FIELDS ARE REFUSED BY NAME WHEN BLANK; NOTHING IS HALF-RECORDED.*) + `ADD MEMORY`. **Tested [live]:** recent → `SHOWING ALL 0 STORED ENTRIES.`; semantic search `pricing` returned **20** real prior-agent hits after a long warm-up, headed `SHOWING THE FIRST 20 MATCHES — … MORE MAY EXIST.` Hits are `AGENT_RUN`, `DATE UNKNOWN`, **`NO ADDRESSABLE ID — CANNOT FORGET`** — index hits are not deletable; stored rows are (`DELETE /memory/{id}`). Degraded answers keep their marker (`keyword match — category filter dropped`, `[partial]`; `app/memory/page.tsx:74-82`). Cold-start timeout 120 s (`:106-117`).

### 3.11 Ops — `shots/ops.png`, `ops-dryrun.png`

`GOD MODE · OWNER COCKPIT · LIVE EVERY 10 S WHILE THIS TAB IS VISIBLE` + `REFRESH`, then: **Live runs** (`0 RUNS IN THE LEDGER`; legend *a dash with "never recorded" is missing history, while "none" means tools were available and the model called none*); **Pending approvals** (`0 WAITING`); **Kill switches** (`AUTO MODE OFF`, `DISPATCH PAUSE RUNNING`, `SANDBOX ON`, plus *the `MERIDIAN_KILL_SWITCH` grade is not exposed by any API route — so no grade is shown rather than guessed*); **Agent triggers** (nine rows; *creates a new manual task row … it does not re-run an existing task*); **Run artifacts** (`0 RUN WORKSPACES`, *a measured zero … not a failed read*); **Retention** (*newest 20 … older than 72 h … 04:15 UTC*). **Exercised [live, changes nothing]:** `DRY RUN` → `WOULD REMOVE 0`, `KEPT 0`.

### 3.12 Capabilities

Read-only, derived live from the policy firewall + tool registry: `10 AGENTS · 17 REGISTRY TOOLS`, each with **ACTION CLASSES** (`READ`, `INTERNAL_WRITE`, `EXTERNAL_REVERSIBLE`, `EXTERNAL_IRREVERSIBLE`), **CHANNELS** (`GOOGLE_ADS`, `META_ADS`, `GITHUB`, `WEB`), and **REGISTRY TOOLS** in the registry's own words — `ads_management` → *No registry tools are granted to this agent*; `competitor_research` → `web_search/web_fetch/research_fetch/signals_read/domain_health/link_status`.

### 3.13 Settings — `shots/settings.png`, `settings-tab-*.png`

**Tabs** (`role=tab`, WAI-ARIA roving tabindex; `components/settings/TabStrip.tsx:7-22`): **Company Profile · Scheduler · Emails · DeepSeek Key** (`app/settings/page.tsx:198-201`), then six un-tabbed sections.

- **Company Profile** — `COMPANY NAME` (*no separate rename step*), Industry, Product Type, Website URL, `SENDER LEGAL NAME` / `SENDER POSTAL ADDRESS` (the boxes every compliance refusal names), Mission/Vision/Description/Target Market/Value Proposition, **Owner profile** (*a blank box is stored as "not set", never a fabricated 0*), `PAUSE ALL DISPATCHES` (*attempts are refused by name, E_POLICY_DENIED*), `SAVE SETTINGS` (one `PUT /config`; banner `Settings saved successfully`, `app/settings/page.tsx:518-521,637-642`).
- **Scheduler** — Timezone, `Morning Cycle Hour`, Notification Email, **Night task**. **Emails** — address + five toggles. **DeepSeek Key** — enter → **Save**, masked status + source chip, **Verify** (spends one call), **Remove** behind a confirm; failures are `role="alert"` (`components/settings/DeepSeekKeySection.tsx:10-27`).
- **Business KPIs** — six numbers, `SAVE KPIS` / `CLEAR ALL KPIS` (*removes all six in one put*). **Pricing/Goals/Repo** — three JSON editors; live `STORED NOW: STARTER — $49 · PRO — $149`, `1 GROUP: Q1_2026 (3 ITEMS)`, `YOURORG/YOURREPO` (placeholder); a read-only `CONFIRMED IDEA` refuses to be a second editor.
- **Integrations** — `CHECK STATUS` reads on demand only (*Nothing is read until you ask*); **Test** shows the server's named refusal; live all six `configured:false`. **Export & wipe** — data ZIP + wipe in order `DRY RUN → MINT CONFIRM TOKEN → confirm` (mint dead here, §4.8). **Pricing catalog** — *not live pricing … nothing here is a quote*. **Site package** — *no uptime promise*.

### 3.14 Runs detail

`/runs/1` renders `Could not load this run: E_NOT_FOUND: run 1 does not exist…`.

---

## 4. First-run guide

**4.1 Open it.** `/` → `/dashboard`; expect topbar, `SANDBOX MODE ON`, SETUP block while no key exists.

**4.2 Model key (real step 1).** *Open Settings → AI model (DeepSeek)* → **DEEPSEEK KEY** → paste `sk-…` → **Save** → `Saved. The key is stored on the backend and used by the agent runtime.` Stored server-side (`DEEPSEEK_KEY_FILE`, default `backend/data/deepseek_api_key.json`); only a masked hint returns. **Verify** spends one live call.

**4.3 Company profile.** `/settings` → **COMPANY PROFILE** → name (live: `Cinder & Lye`), industry, product type, website, mission, vision, description, target market, value prop → **SAVE SETTINGS** → `Settings saved successfully`. `SENDER LEGAL NAME` + `SENDER POSTAL ADDRESS` are effectively mandatory: until set, every email/invoice footer is refused by name (`E_CONFIG`, `services/email_compliance.py:258-261`).

**4.4 Owner profile & KPIs.** Skills, hours/week, budget, goal, risk tolerance, channels, locale — *blank = not set, never 0*; `owner_hours_per_week` feeds `/finance` `MAX CLIENTS`. Then `SAVE KPIS` (MRR, customers, churn, CAC, LTV, NPS), printed as `CONFIGURED KPIS — SETTINGS VALUES, NOT MEASURED`.

**4.5 Pricing, goals, repo.** Three JSON boxes; the `STORED NOW:` line proves each save landed. The pricing model is the site catalogue; the repo is the only one the code agent may touch.

**4.6 Integrations and email.** `CHECK STATUS` → enter → save → **Test** (failures are `E_AUTH`/`E_UPSTREAM`, never a fake "connected"). Minimum: SendGrid (+`SENDGRID_FROM_EMAIL`), Stripe, GitHub, Render. Then **EMAILS**: address + five toggles.

**4.7 Surfaces.** (1) **Sites**: title/tagline (blank = profile), template, optional domain → `CREATE SITE` → `/sites/<slug>/`; read both warnings (the contact page publishes your details; no cart). (2) **Agents**: `RUN NOW` → confirm (*creates a NEW task … spends from today's AI budget*) → `CREATE TASK` — where money first moves (`AGENT_RUN_COST_CAP_USD`, `AGENT_DAILY_COST_CAP_USD`; `/finance/summary` shows `ai_spend.limit 25.0`). (3) **Auto Mode** on `/dashboard`, confirmed on `/ops`. (4) **Tasks**: `NEW TASK` (title + agent required). (5) **Approvals** `/ops` — **cannot be completed here**. (6) **Daily reports**: no button; written on the beat.

**4.8 What blocks progress here (measured, ranked).** (1) **No model key** — no agent can produce output. (2) **`APPROVAL_SECRET` invisible to the process** — `POST /approvals` 503 `E_CONFIG`; kills chat CONFIRM, the data wipe, gated deploys, parked-run approvals. (3) **Planner refuses** (`E_AGENT_UNKNOWN`) — no confirmed idea ⇒ price band, hours/client, MRR-at-capacity, break-even, margin all `—`. (4) **No Redis** — `E_REDIS` 503, activity WebSocket never opens. (5) **`SANDBOX_MODE=true`** — every external action is drafted. (6) **No integration credentials** — all six `configured:false`. (7) **Celery beat unobservable** — no next-run time is claimed anywhere.

---

## 5. Feature catalog

**Dashboard** — status, Auto Mode (5 windows), Business (counts, report freshness, margin with named inputs, measured vs configured KPIs), Website health, Phone, Tasks preview, Daily Report (derived only from `activity_log` + `daily_reports`), Media (absent), Social/Outreach/Ads, 12-channel send caps, Signals, Activity feed, Chat; widget show/hide/reorder via **EDIT** (browser-local). **Chat gate** — deterministic $0 router: five reads, one write, `E_POLICY_DENIED` for money/external verbs (201 body field); confirm token single-use and HMAC-bound; replay → `409 E_TASK_NOT_RUNNABLE`.

**Tasks** — board, create dialog (≤4 images ≤5 MB), one-write status control limited to the server matrix, run, attachments, artifacts. **Agents** — nine with mandates, counts, tool grants, dispatch creating a *new* task, plus the capability map. **Phone** — the richest read surface (threads, 100-message window, media, calls, codes, ZIP export, gated composer). **Finance** — human rate, a planning template where each cell names its missing input, revenue/expenses/Stripe events, macro context, AI spend + unit economics. **Sites** — 3 templates, regenerate/delete, preview-vs-live deploy, DNS/TLS badge. **Memory** — semantic search (20/page, 120 s cold-start tolerance), stored rows with tags, delete-by-id. **Ops** — runs, approvals, switches, triggers, artifacts, retention.

**Settings** — 4 tabs + 6 sections (§3.13). **Notifications** — bell over `GET /notifications`, last 48 h. **Search** — Ctrl+K: 19 routes + 50 tasks + 50 runs + 20 reports. **Exports** — `/data/export` (ZIP, 4,412 B live), `/runs/export?format=csv`, `/phone/export` (ZIP, 78,549 B live), site package ZIP, `meridian-activity.log`; no memory export exists. **API docs** — `/docs`, `/openapi.json` (428 kB); auth is one `X-API-Key`.

---

## 6. Sixty things a user can do today

**N** = no credentials, works under sandbox · **W** = writes stored state · **C** = needs a credential · **S** = needs sandbox off.

**Reads (N)**
1. Today's task counts — Dashboard chat `status`.
2. Runs-ledger summary — chat `runs`.
3. Money summary — chat `finance`.
4. Whether a daily report exists — chat `report`.
5. One task by id — chat `task 12`.
6. What the assistant can do — chat, any other sentence.
7. Nine agents with mandates + tool counts — `/agents`.
8. Open a dispatch confirm and back out — `/agents` → CANCEL.
9. Capability map (10 agents · 17 tools) — `/capabilities`.
10. Per-channel send caps — Dashboard Channels; `GET /channels/caps`.
11. Macro context with provenance — `/finance`; `GET /economics/context`.
12. Every margin cell and its blocker — `/finance`.
13. Revenue/expense/Stripe-event counts — `/finance`.
14. Planner confirmation state — `/planner`; `GET /planner/status`.
15. Six-step first-dollar playbook with cited fees — `/planner`.
16. Whole ops cockpit — `/ops`.
17. Three kill switches' true state — `/ops`; `GET /auto-mode`, `/config`, `/ops/sandbox`.
18. Activity-breaker counters — `/usage`.
19. Per-agent 7-day quality — `/usage`; `GET /ops/agent-quality`.
20. Filter invoices by status — `/invoices`.
21. Search/filter prospects — `/prospects`.
22. Phone threads, 100 messages, media, calls, codes — `/phone`.
23. Support inbox by status — `/support`.
24. Semantic memory search — `/memory` → SEARCH.
25. Stored memory rows with tags — `/memory` → RECENT ENTRIES.
26. Notifications, last 48 h — bell.
27. Third-party headlines — Dashboard SIGNALS.
28. App health + latency — Dashboard Website card.
29. Dated pricing-catalog snapshot — Settings → Pricing catalog.
30. Route/task/run/report search — Ctrl+K.
31. Any of 19 destinations — MENU.
32. A run's detail when the id exists — `/runs/<id>`.
33. A task's detail, files, artifacts — `/tasks/<id>`.

**Owner writes (W)**
34. Store a DeepSeek key server-side — Settings → DEEPSEEK KEY → Save.
35. Verify it against the live API (one paid call) — same pane → Verify.
36. Remove the stored key behind a confirm — same pane.
37. Save the whole company profile in one PUT — `/settings`.
38. Rename the company (same save).
39. Set sender legal name + postal address.
40. Pause all dispatches — `PAUSE ALL DISPATCHES` → ON.
41. Enter or clear six KPIs — Settings → Business KPIs.
42. Edit the pricing-model JSON and see it summarised.
43. Edit the goals JSON.
44. Set the code agent's GitHub repo.
45. Set the human hourly rate — `/finance` → SAVE RATE.
46. Add a prospect (email only required) — `/prospects`.
47. File a support message (4 required fields) — `/support`.
48. Create a schedule; it starts paused — `/schedules`.
49. Record a memory — `/memory` → ADD MEMORY.
50. Delete a stored memory row — `DELETE /memory/{id}`.
51. Create a task (title + agent, ≤4 images, priority) — `/tasks`.
52. Move a task to a legal status only — `/tasks/<id>`.
53. Run a task — `/tasks/<id>` run.
54. Trigger any of nine agents — `/agents` RUN NOW or `/ops` TRIGGER.
55. Start Auto Mode for 1 h … 5 days — Dashboard.
56. Stop Auto Mode — same control.
57. Create a site from the profile (3 templates, optional domain) — `/sites`.
58. Regenerate or delete a site (two-step confirm).
59. Export: data ZIP / runs CSV / phone ZIP / site package / activity log.
60. Retention **dry run**; reset the activity breaker — `/ops`, `/usage`.

*Conditional writes:* invoice send/void (**C**+**S**), site deploy (**C**+approval token+**S**), SMS (**S** + paired Google Messages), site package (**N**).

---

## 7. Sixty things the app cannot do

**Blocked by configuration on this install**
1. Cannot mint an approval token — `POST /approvals` 503 `E_CONFIG`.
2. Cannot confirm a chat-proposed task — the CONFIRM card is never offered.
3. Cannot finish the guarded data wipe (its middle step is the mint).
4. Cannot perform any approval-gated action (deploy, gated send).
5. Cannot show Stripe/SendGrid as configured though `.env` has keys.

**Blocked by the sandbox**
6. Cannot send an SMS — 403 `E_SANDBOX_BLOCKED`.
7. Cannot send outreach email — draft only.
8. Cannot post to X, LinkedIn, Facebook, Instagram, TikTok, YouTube.
9. Cannot launch or spend on Google/Meta ads.
10. Cannot deploy to Render.
11. Cannot charge, refund or reconcile a payment — Stripe is ingest-only.
12. Cannot verify or point a custom domain (recorded, then DNS/TLS-checked only).

**Broken on this build**
13. Cannot propose planner candidates — `E_AGENT_UNKNOWN` 422.
14. Cannot confirm an idea ⇒ no price band / hours-per-client / margin.
15. Cannot trigger `company_planner` — no route accepts it (enum of nine).
16. Cannot use the realtime activity stream — WS does not open.
17. Cannot read worker publish stats — `E_REDIS` 503.

**Not implemented at all**
18. No cart, checkout, payment or order management on a site.
19. No image or video generation (Media card says so).
20. No multi-user accounts, teams, roles or invitations — one API key.
21. No signup, plan, seat or billing page for the operator.
22. No username/password, SSO or session — auth is a single header.
23. No mobile app, and no designed mobile layout (a desktop grid that stacks).
24. No page builder — three fixed templates.
25. No custom CSS/JS or theme editor for sites.
26. No SEO tooling beyond the generated `sitemap.xml`.
27. No inbound email — no IMAP, no reply threading; only file-a-message + 3 h sweep.
28. No outbound calling — calls are read-only history.
29. No media upload to the phone store — download only.
30. No conversation creation beyond the thread/number field.
31. No ad-campaign creation from the UI — read-only list + metrics.
32. No social composing — read-only.
33. No prospect editing on `/outreach` — read-only by design.
34. No bulk import (CSV/JSON) for prospects, invoices, tasks, memory.
35. No bulk delete/archive for tasks.
36. No drag-and-drop status changes — one select writes one status.
37. No calendar or Gantt view of schedules.
38. No per-schedule timezone — hour is company-local only.
39. No cron expressions — four cadences.
40. No export of memory, prospects, invoices or sites.
41. No charts anywhere — numbers and tables only.
42. No dark-mode toggle.
43. No server-side preference storage — dashboard layout is browser-local.
44. No activity-log query UI — the LOG lists, it does not filter.
45. No webhook management UI (Stripe endpoint exists, no config surface).
46. No API-key rotation UI — environment only.
47. No editor for send caps, cost caps or retry policy — env constants.
48. No legal/tax filing, EIN application or entity registration — links out only.
49. No bank/marketplace connection.
50. No payroll or contractor payments.
51. No competitor-monitoring dashboard — only research prose.
52. No document editor — invoices are structured fields.
53. No content template library for posts or emails.
54. No per-action approval inbox — only parked runs appear.
55. No undo after a confirmed delete (site delete is permanent).

**Deliberate refusals (by design)**
56. Chat will not perform or queue money/external actions — `E_POLICY_DENIED`.
57. Chat will not answer from general knowledge — it says it does not guess.
58. Nothing fabricates a zero: unmeasured renders `—` with a named reason.
59. No next-run time is claimed for the report cycle (beat unobservable).
60. No kill-switch grade is displayed (no route exposes `MERIDIAN_KILL_SWITCH`).

---

## 8. Screenshots

68 PNGs in `shots/` (`*-full.png` are full-page); the machine-readable index is `shots/transcript.json`. Main pages — `dashboard.png`, `tasks.png`, `agents-loaded.png`, `finance.png`, `sites.png`, `settings.png`, `memory.png`; key states — `dashboard-chat-thread.png`, `tasks-new-form.png`, `task-detail.png`, `agents-runnow-confirm.png`, `planner-propose.png`, `finance-save-rate.png`, `settings-tab-deepseek-key.png`, `memory-search-long.png`, `ops.png`, `ops-dryrun.png`. **`phone.png` / `phone-full.png` contain personal data — do not publish.**

---

## 9. Limits of this chapter

- The frontend **went down ≈14:50** during the last pass (another lane restarting it): the Settings tab-panel images and the Edit-sheet shot come from code plus earlier captures. Every **[live]** claim predates the outage.
- **Writes were not executed** except refused ones and the retention dry run; creation/send/dispatch/key-save paths are documented from code and screen copy, not a round trip.
- Brand strings changed **between two captures 20 minutes apart** (`Meridian stores the hostname` → `Meridian stores the hostname`); quoted copy is build-specific. `SANDBOX_MODE=true` throughout, so no outbound path could be proven end-to-end.

### Done
| Item | Evidence |
|---|---|
| All 22 routes loaded and described | `guide/shots/transcript.json` (21×200 + `/` redirect), `guide/pages/*.txt` |
| Control/state inventory per page | §3, each claim tied to live text or `path:line` |
| Chat $0 reads + CONFIRM gate proven closed | `guide/shots/chat.json`; responses quoted in §3.1 |
| Refusals captured by code | `E_POLICY_DENIED` (201 body), `E_CONFIG` ×2, `E_AGENT_UNKNOWN` 422, `E_SANDBOX_BLOCKED` 403, `E_NOT_FOUND` ×2, `E_REDIS` 503 |
| 60 can / 60 cannot | §6, §7 |
| No repo modification | only `meridian-work/build/swarm-2026-10-28/guide/` written |

### Not done
| Item | Reason |
|---|---|
| Edit-sheet + Settings tabpanels as images | frontend down 14:50; copy verified from code (`DashboardEditorSheet.tsx`, `app/settings/page.tsx:198-201`) |
| Any non-refused write round trip | would mutate the demo DB; tasks/invoices have no delete path |
| End-to-end agent run | needs a DeepSeek key (none) + Redis (down) |
| Phone/email/ads/deploy sends | sandbox + no credentials + no approval channel |
| Redacted `phone.png` | redaction would be an edited artefact; §3.6 carries the warning instead |

### Unverified
| Claim | What would prove it |
|---|---|
| `SAVE SETTINGS` → `Settings saved successfully` | one save in a disposable DB + re-read `GET /config` |
| Task creation lands in `TO DO` | create one row, re-read `/tasks` |
| `RUN NOW` → 202 + task id | dispatch with the mock model and a live Redis |
| Invoice send/void refusals | create a draft, attempt send under sandbox |
| Site create/regenerate/delete + serving | create in a scratch `SITES_DIR` |
| Memory `ADD` 201 / `DELETE` 200 | one entry, then delete |
| `.env`-not-loaded root cause | restart uvicorn with `.env` exported; `GET /integrations` should show `configured:true` |
| Mobile/responsive behaviour | viewport sweep at 390/768 px |
