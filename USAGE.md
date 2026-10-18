# MYPOLSIA — the Operator's Guide (USAGE.md)

The complete "what it is / how to run it / how to work it" guide. Setup lives in
[DEPENDENCIES.md](DEPENDENCIES.md); the skimmable tour is [README.md](README.md); the living
to-do map is [NEXTSTEPS.md](NEXTSTEPS.md). This file is the depth: what the system actually does,
what you do day to day, and where every feature lives.

---

## 1. What MYPOLSIA is

MYPOLSIA is an autonomous **business-ops platform**: a FastAPI + Celery backend and a Next.js 14
dashboard behind an nginx reverse proxy, with an agent fleet that plans, drafts, and (when you let
it) executes business work — social posting, outreach email, ads, support, finance, invoices, code
generation — under a policy firewall, a sandbox, cost budgets, and an approval flow.

```
nginx (127.0.0.1:80)
  /      → Next.js 14 (frontend)
  /api/  → FastAPI (backend, REST + WebSocket /ws/activity)
              ├── PostgreSQL 16        (22 tables: tasks, runs, reports, invoices,
              │                         memory rows, company config, activity_log, …)
              ├── Redis + Celery       (broker + task queue: `agents`, `scheduler`,
              │                         `maintenance` queues + beat)
              └── ChromaDB             (vector memory store)
```

### The orchestrator + nine agents

Nine agent types live in `backend/app/agents/crew_factory.py` (`AGENT_MAP`) — the orchestrator plus
eight specialists. An agent run is one model call through the runtime (§1.2) that returns structured
JSON; the worker persists it, attaches its artifacts (ads spend, revenue facts, memories…), and the
activity feed reports it.

| Agent (`agent_type`) | What it actually does |
|---|---|
| `orchestrator` | **Tool-less planner.** Morning: reads context, emits `{summary, key_focus, tasks[]}` — the morning cycle **materialises those tasks** and files the plan in `daily_reports.morning_plan`. Evening: one aggregate summary of the day. It cannot fetch, post or spend — by design (a planner that can act is a planner that can be injected). |
| `business_planning` | Strategy/KPI reasoning for the plan (runs via the morning plan; no beat entry of its own). |
| `competitor_research` | Competitor profiles + sourced claims (via the morning plan; source URLs are scheme-validated and unverifiable claims are dropped + counted). |
| `social_media` | Drafts/publishes posts through the firewall; real threading (replies carry the provider's own mention id — fabrication is gone); mentions sweep every 2 h. |
| `email_outreach` | Prospect research + cold-email sequences; CAN-SPAM compliance gate; follow-up sweep every 3 h for unanswered sends. |
| `customer_support` | Reads the support inbox, drafts/sends replies through the compliance bridge (`send_bridge` — one gate, fail-closed). |
| `ads_management` | Google/Meta campaign + metric work; metrics persist (deduped by platform id / campaign+date); ads↔Stripe sync every 6 h. |
| `code_generation` | Feature work → PRs via the GitHub integration (needs `GITHUB_TOKEN`; refuses by name without it). |
| `finance` | Stripe revenue sync, spend tracking, failed-payment leg; revenue facts read + attached into finance runs. |

The **maintenance** lane is not an agent: it's a set of Celery sweeps that recover the fleet
(§4.13) — task-lease sweep, stranded-pending reconciler, stale-run closer, paused-release re-arm,
publish healthcheck, activity retention.

### The runtime — DeepSeek-first, Claude CLI fallback

Every agent run goes through the provider-selectable runtime:

- **Primary: DeepSeek** (`deepseek-flash`, OpenAI-compatible API at `https://api.deepseek.com`;
  `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_REASONING_EFFORT` / `DEEPSEEK_TIMEOUT_SECONDS`
  in `.env`). The API key is **entered in the app** — the dashboard's onboarding card or
  Settings → **DeepSeek Key** — and stored server-side at `DEEPSEEK_KEY_FILE`
  (`data/deepseek_api_key.json`, the `backend_data` volume). A key saved in the UI **wins** over
  `DEEPSEEK_API_KEY` in the environment; it is never logged or returned (the API answers masked
  status only), and the verify button makes a real zero-token-cost `GET /models` call and reports
  its genuine result.
- **Fallback: Claude Code CLI** — agents shell out to `claude -p "…" --output-format json`; no
  Anthropic API key is used. `claude login` on the host writes `~/.claude`, mounted read-only into
  the containers. A path that doesn't resolve is the named `E_CLI_MISSING` refusal — never a silent
  fallback to some other binary.
- Tests/CI set `CLAUDE_CLI_MOCK=true` (agents return a stub; no subprocess) — that flag is how the
  shipped suite runs anywhere.

### Sandbox vs live — what `SANDBOX_MODE` really gates

- `SANDBOX_MODE=true` (default, and the state you should stay in while learning the system):
  **no real side effect happens** — no post, email, ad change, payment, or PR. Every external
  action a run attempts is recorded as a **labelled draft**, never as a success.
- `SANDBOX_MODE=false`: the same gates now let real sends through — still bounded by the policy
  firewall: per-channel **send caps** (at cap → `E_RATE_LIMITED`; a channel with no counting source
  refuses by name instead of guessing), **approval escalation** for irreversible/high-value
  actions (≥ $25), and the **kill switch** `POLSIA_KILL_SWITCH` (`throttle` = external-action
  agents pause, reads continue; `freeze` = all dispatch refused — takes effect without a redeploy).
- **Cost budgets** (`AGENT_RUN_COST_CAP_USD` $2.0/run, `AGENT_DAILY_COST_CAP_USD` $25/day) are
  enforced per dispatch: a denied entry is **skipped with its reason**, never silently dropped.
- The four **supervision tiers** (auto / review / approve / human-only) and the exact code path
  that enforces each are documented in [`docs/SUPERVISION.md`](docs/SUPERVISION.md).

---

## 2. Install (condensed)

The full link-by-link setup guide — WSL2, Docker Desktop, GNU Make, Python, Node, Git, Stripe CLI,
with install + verify commands and a troubleshooting table — is **[DEPENDENCIES.md](DEPENDENCIES.md)**.
The short chain:

```bash
# 1. Windows 10/11 + WSL2 + Docker Desktop + GNU Make (DEPENDENCIES.md §1)
git clone https://github.com/RodneyDanger1/MYPOLSIA.git
cd MYPOLSIA
cp .env.example .env        # edit: API_KEY (change it), SANDBOX_MODE=true stays until you're ready
export NEXT_PUBLIC_API_KEY=dev-key   # MUST match API_KEY — the frontend key is inlined at BUILD time
docker compose up -d        # backend, worker, beat, postgres, redis, nginx, backup sidecar
make init-db                # alembic upgrade head + seed company data
# Open http://localhost  ·  health: http://localhost/api/v1/health → {"status":"ok"}
```

Two that bite (both measured, both in DEPENDENCIES/README): `NEXT_PUBLIC_API_KEY` must match
`API_KEY` or every page renders its empty/error state while `curl` looks healthy; and there is a
no-Docker dev path (`make dev-backend` / `make dev-frontend` / `make dev-e2e`).

---

## 3. First-run checklist (do these in order)

1. **`.env` edited before first boot** — at minimum `API_KEY` (change it) and `SANDBOX_MODE=true`.
   Optional for later: Stripe, SendGrid, Twitter/X, ads, GitHub keys (blank = integration disabled;
   the product stays usable and every code path that needs the credential refuses **by name**).
   Tighten the file once: POSIX `chmod 600 .env`; Windows
   `icacls .env /inheritance:r /grant:r "%USERNAME%:(R,W)"`.
2. **Stack up + health** — `docker compose up -d`, then `curl http://localhost/api/v1/health` →
   `{"status":"ok"}`. `docker compose config -q` first if you edited compose yourself.
3. **Database initialized** — `make init-db` (migrations + `scripts/seed_company.py`).
4. **First configuration — Settings** (`/settings`):
   - **Company tab:** Company Name; Industry; Product Type; **Website URL** (shown exactly as
     stored — edit it here to replace the placeholder); **Sender Legal Name** + **Sender Postal
     Address** (the CAN-SPAM sender identity outreach/invoice compliance refusals point at — set
     these before enabling any real email); Mission / Vision / Description / Target Market / Value
     Proposition; **Timezone**; **Morning Cycle Hour** (company-local — takes effect without a beat
     restart); Notification Email. The **Pause all dispatches** switch lives here too.
   - **Owner Profile** (inside the Company tab): owner skills, hours/week, budget, goal, risk
     tolerance, channels ready, market locale. Blank numeric boxes stay honestly NULL ("not set" ≠
     0).
   - **DeepSeek Key tab:** paste the key; the status line and verify button tell you the real
     state. (The dashboard shows a DeepSeek onboarding card until a key is active.)
   - **Emails tab:** notification-email behaviour (completed / failed / awaiting approval /
     requeued), cycle-report email. The support **inbox** is filled via
     `POST /api/v1/support/inbox` — there is no mail-poll (IMAP) setting.
   - **Scheduler tab / Night task section:** night-task hour + enable (OFF by default; ≤ 5 rows per
     tick).
5. **Sandbox banner + first run** — the app-wide banner states plainly when sandbox mode is ON.
   Run one agent from **Agents** (`Run now`) and watch **Activity** on the dashboard: in sandbox
   you'll see the labelled draft, not a send. Then read §4 to work it for real.

---

## 4. How to work it, day to day

### 4.1 Dashboard (`/dashboard`) — your morning start
Left rail: Polsia status card + **Auto Mode** panel + business mini-panel. Middle: Tasks preview +
**Daily Report** (the morning plan and evening summary filed by the cycles; the Documents overlay
expands it). Right: Social / Outreach / Ads widgets + the **send-caps counter**. Then the live
**ActivityFeed** (WebSocket) and the **ChatRail**. Widgets compose through the V12 editor — show/
hide/reorder/restore, stored in your browser; a hidden widget makes no request. **Auto Mode** opens
a bounded autonomous window (1 h … 5 d, one live window) during which drafts/internal work runs
automatically; external sends still obey sandbox + caps.

### 4.2 The morning plan (what happens at your configured hour)
The morning cycle fires **hourly** and self-gates to your company-local hour (default 06:00).
Inside the window it: claims the once-per-day tick (a redelivery exits as a named skip with zero
spend), runs the orchestrator to produce the plan, **creates the tasks it returns** (up to the
per-day cap) plus the fixed daily work — finance revenue snapshot, customer-support morning sweep,
social morning check — and files the plan in `daily_reports.morning_plan`, which the dashboard's
Daily Report shows. The evening cycle does the same for a company-local day aggregate summary.
Budget denial per dispatch is skipped **with its reason** on the task list.

### 4.3 Tasks board (`/tasks`) — the work queue
Every dispatch shows as a task with an honest status (`pending` → `in_progress` → terminal;
`awaiting_approval` parks the row — see §4.5). The detail sheet (`/tasks/{id}` deep-links
fetch-by-id, including ids outside the loaded board) offers the single recovery action the row
really has — **Run** it, or **Review approval** — or states why there is none. Approving mints a
fingerprint-scoped, single-use approval token and re-queues the parked row.

### 4.4 Planner (`/planner`) — the business core's front door
`GET /planner/status` on mount; **Propose** builds a candidate plan (claims source-validated —
bad-scheme URLs are refused, unverifiable claims are dropped and counted); **Confirm** records the
decision (`confirmed_idea` / checklist state on the company config). It is the deliberate,
owner-driven planning surface next to the autonomous morning plan.

### 4.5 Approvals & the supervised path
When the policy firewall escalates (irreversible action, tainted run context, or value ≥ $25), the
worker parks the task as `awaiting_approval` and notifies you (bell/notification email if
configured). You mint a **single-use HMAC approval token** for the named action fingerprint —
from the Tasks sheet's Review-approval section, or `POST /api/v1/approvals` directly — signed with
`APPROVAL_SECRET` (set it in `.env` before issuing any). With no approval channel configured, the
same branch **fails closed** to a hard deny (`E_POLICY_DENIED`). Tier enforcement map:
[`docs/SUPERVISION.md`](docs/SUPERVISION.md).

### 4.6 Activity feed — the honest log
The dashboard feed is live (`/ws/activity`; the nginx access log deliberately does not log the
socket URL). Every meaningful runtime fact lands here: runs, task transitions, approval waits,
requeues, backups, phone events (V16). The publish breaker (a Redis outage skips-and-counts instead
of stalling) is visible on `/usage` (§4.12) and its 15-minute worker-side healthcheck logs a
warning while anything is non-clean.

### 4.7 Schedules (`/schedules`) — recurring work
CRUD for the owner's recurring task prompts. Every enabled row with a due `next_run_at` becomes ONE
task per due instant (dedupe key included) via the 15-minute cadence tick; `next_run_at` is
recomputed from the company-local wall clock (DST-safe, never "+24 h").

### 4.8 Social (`/social`) + the Social widget
Draft and publish posts through the firewall; published rows are recorded (that's what feeds the
per-channel send caps). Replies really thread on the provider's own mention id — the old fabricated
`mention:{index}` behavior is gone, and with nothing to reply to the action refuses by name. The
2-hourly mentions sweep creates the follow-up work. LinkedIn has no send adapter (refuses honestly).

### 4.9 Outreach (`/outreach`) + the Outreach widget
Prospect list (server-paged) and the compliance-gated sequence: sends carry the CAN-SPAM footer
built from **Sender Legal Name + Sender Postal Address** (the refusals name those exact fields
until set), unsubscribe links derive from your **Website URL**, and the 3-hourly follow-up sweep
creates one deduped follow-up task per overdue, unanswered, `status='sent'` email. The public
unsubscribe route is rate-limited and token-gated.

### 4.10 Support (`/support`) — the inbox mirror
Read the support inbox (filled via `POST /api/v1/support/inbox`), draft and send replies. Replies
ride the same compliance bridge and count against the email channel's caps — the one send path,
fail-closed.

### 4.11 Ads (`/ads`) and Finance (`/finance`) + Invoices (`/invoices`)
- **Ads:** campaign surface; the 6-hourly ads↔Stripe sync, and completed ads runs persist their own
  campaigns/metrics (deduped: campaign by provider id with a `(platform, name)` fallback, metric by
  `(campaign_id, date)`). One named residual: `roas` reads 0 until the run schema carries
  `revenue_usd` (README "Known gaps").
- **Finance:** the seven KPI cards from `GET /finance/summary` plus revenue / expenses / invoices
  lists — a field that was never measured prints **null, never a fabricated 0**. Finance runs read
  real revenue facts (30-day window) and attach them; failed payments get their own leg.
- **Invoices:** create → send → (paid / overdue / void). Send renders + delivers through the
  firewall — in sandbox it refuses with `E_SANDBOX_BLOCKED`, and a row flips to `sent` **only on a
  confirmed provider delivery** (never optimistically). The dunning ladder self-gates hourly on the
  company-local hour; each step fires at most once (activity-row ledger), so late/re-delivered ticks
  are harmless. Email gate: an invoice email is approval-gated when the amount asked reaches the
  threshold; plain reminders stay ungated. **Stripe webhook** (`POST /api/v1/finance/stripe/webhook`)
  needs `STRIPE_WEBHOOK_SECRET` — without it the endpoint returns a named 500 refusal, never a
  silent unverified accept.

### 4.12 Usage (`/usage`) — runs ledger + ops panel
The agent-runs ledger (filters + `GET /runs/export?format=csv`, whose `X-Polsia-Row-Count` is the
full filtered count by contract), plus the **OpsPanel**: agent-quality reads, the activity publish
breaker stats, and a reset. Process boundary, stated not hidden: the breaker counters are
per-process — the panel reports the API process; the worker reports its own every 15 minutes.

### 4.13 Maintenance (automatic, `/usage` + Activity show the results)
Every 5 min: task-lease sweep (a dead worker's claim requeues), stranded-pending reconciler
(crash between scheduler commit and broker hand-off re-dispatches), stale agent-run closer
(`E_RUN_STALE`, no fabricated cost), paused-release re-arm (resume nudge exactly once). Daily
03:00: activity retention (default 90 days). Every 15 min: publish healthcheck. Missed/expired
ticks collapse into the next one by design (`expires` on every beat entry).

### 4.14 Memory (`/memory`) and the chat rail
Memory is explicit: search on request, honest states (never-searched / searching / named failure /
real empty), with add/forget controls. Behind the scenes every agent run reads its relevant
memories (`resolve_agent_memories`) and writes back what it learned (`remember_run`) — all nine
agent types via the base layer. The dashboard **ChatRail** talks to the orchestrator with a
confirm-card flow for anything consequential.

### 4.15 Settings — the rest of the control panel
Below the form card: **Data** (export ZIP of everything; dry-run wipe counts; a real wipe needs the
single-use confirm token minted for the dry-run fingerprint), **Pricing**, **Site package**
(download the site bundle ZIP), **Capabilities** (V16 — the link to the read-only agent → tool →
action map, `/capabilities`), **Night task**, **Owner profile**, and the DeepSeek key section.
`PUT /config` is strict: an unknown key is a 422 naming it (`extra_forbidden`) — typos never save
silently.

### 4.16 Backups, export, wipe
`make backup` (stack up) snapshots Postgres + the four data volumes into `backups/`, refusing by
name when a volume is missing (an empty tar is a green-looking, worthless backup). The compose
**backup sidecar** does the same on a 24 h loop with 14-day retention. Restore is **executable**:
`make restore-drill SRC=backups` starts one throwaway `postgres:16-alpine` container (no published
ports, never the compose project), loads the dump, verifies table/row counts against a manifest
parsed from the dump itself, extracts every volume archive, and always tears it down via trap —
exit 0 PASS / 1 FAIL / 2 refusal. `DRY=1 make restore-drill` prints the plan and touches nothing;
`make restore SRC=backups` still prints the optional full-stack scratch-project steps. Data
export/wipe: see §4.15. Full detail: README "Volumes & Backup".

---

## 5. Phone in Polsia — **live (V16)**

Status: **live since V16** — proven against the real Phone Link store on 2026-10-15
(`wave-v16/v-final/live-drill2.txt`: P1 `store_status='ok'`, unread 57, threads 10 · P4 codes
10 rows · P5 export zip carries `messages.json` + `calls.csv` + `contacts.json`). The Hermes-side
bridge predates it — see [PHONELINK-MCP-TEST.md](PHONELINK-MCP-TEST.md). The app's own native read
path:

- **Mount:** `docker-compose.yml` bind-mounts the Windows Phone Link store **read-only** at
  `/phone-link:ro`; host root from **`PHONE_LINK_DB_DIR`** (blank = the measured Windows default).
- **Service:** `phone_link_service` — SMS/MMS union, honest FILETIME→local conversion, contact-name
  resolution; **refuses by name** (`E_PHONE_UNAVAILABLE`, 503) when the mount/store is absent —
  never fabricates.
- **Routes** (key-auth, `{ok,data}` envelope): `GET /api/v1/phone/summary` · `…/messages?limit&
  thread_id` · `…/calls?limit&missed_only` · `…/codes?within_hours` (**key-only + audit-logged** —
  it exists for auth flows) · `…/export` (streaming zip: messages.json, calls.csv, contacts.json).
- **Visibility:** a ≤5-minute tick publishes "SMS received: …" / "Missed call: …" into the activity
  feed (dedupe by kind+id — a re-run publishes nothing twice); dashboard **Phone** card + `/phone`
  page (16th nav route: recent/thread/search/calls + download).
- **Honest limits:** read-only by default, Windows-only, Phone Link app must run for fresh sync.
- **Send (V17):** `POST /api/v1/phone/send` is the one write path — a CDP driver against a paired
  Google Messages web session. It is **off by default**: sandbox mode refuses it by name
  (`E_SANDBOX_BLOCKED`), an unpaired driver refuses (`E_PHONE_SEND_UNPAIRED`), a missing store/CDP
  refuses (`E_PHONE_UNAVAILABLE`), and a send that did not happen is never reported as one. Every
  attempt — sent or refused — leaves exactly ONE `activity_log` row (masked recipient, body length;
  the body text is never stored), committed before a refusal raises.
  **Deployment note (measured):** the driver must run where it can reach the desktop Chrome's
  loopback CDP port, so the send path is served by the **host-run API** (`make dev-backend`); the
  Docker stack answers `E_PHONE_UNAVAILABLE` ("driver is missing") by name — the container cannot
  reach a loopback CDP socket, and this project does not expose the debug port to the LAN.
  The final send is owner-gated: launch the automation Chrome (dedicated profile,
  `--remote-debugging-port=9223`), confirm `python scripts/phone_send_driver.py --check` says
  `paired:true`, set `SANDBOX_MODE=false`, then `POST /api/v1/phone/send`.

## 6. Capabilities — agents see their real toolbox (**live, V16**)

Status: **live since V16** (frozen interface H-C1; real-store drill P6 `agents=10 registry_tools=3
with_tools=5`, `wave-v16/v-final/live-drill2.txt`). The capability manifest is **derived from
the single registry sources** (`policy_firewall.AGENT_CAPABILITIES` + `tool_registry`) — never
hand-copied — and is injected into each agent's assembled context (`base_agent`), so every agent
knows its real tools (descriptions included), action classes, and channels; the orchestrator gets
the full cross-agent map. `GET /api/v1/ops/capabilities` serves the same shape for tooling
(`{agents:[{agent_type, tools, action_classes, channels}], registry_tools}`), with a drift pin
(registry change → parity pin fails). A read-only **Capabilities** page (`/capabilities` — the 16th nav route since V17; also linked
from Settings) renders it honestly (empty registry → honest empty;
never fabricated rows).

---

## 7. Operating safely (what the hardening actually does)

- **Docs auto-hide**: `/docs`, `/redoc`, `/openapi.json` are 404 when `SANDBOX_MODE=false`
  (production posture); the API root stops advertising them.
- **Loopback bind**: nginx publishes `127.0.0.1:80` only — nothing is on your LAN. Before ever
  publishing this stack beyond localhost: rotate `API_KEY`/`NEXT_PUBLIC_API_KEY` (they must match;
  rebuild the frontend image) — see README "Known gaps" F-02.
- **Rate limits** on the three public routes (health, unsubscribe, Stripe webhook), named
  `E_RATE_LIMITED` 429s; **CSP + `X-Frame-Options` + `nosniff`** at server scope; `/ws/` is
  excluded from the access log.
- **Secrets**: `.env` is yours to tighten once (§3.1); the DeepSeek key store is tightened after
  every write (`chmod 600`, or `icacls … /inheritance:r` on Windows) and a warning names the file
  when neither mechanism applies. No secret is ever logged or returned by the API.
- **Strict edges**: unknown config keys → 422 naming the key; malformed timestamps → 422 naming the
  field; money columns refuse NaN/Inf; dirty DB rows refuse by name.

## 8. Where to learn more

| For | Read |
|---|---|
| Feature tour (skimmable) | [README.md](README.md) |
| Install / troubleshooting | [DEPENDENCIES.md](DEPENDENCIES.md) |
| Supervision tiers → enforcing code | [docs/SUPERVISION.md](docs/SUPERVISION.md) |
| Acceptance testing model | [docs/ACCEPTANCE_TESTING.md](docs/ACCEPTANCE_TESTING.md) |
| Release readiness | [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) |
| What's next / what's done | [NEXTSTEPS.md](NEXTSTEPS.md) |
| Phone bridge (Hermes side + V16 plan) | [PHONELINK-MCP-TEST.md](PHONELINK-MCP-TEST.md) |

*Every statement above is checked against the shipped code and config (routes, settings,
beat schedule, page components). Where a feature is in flight, it says so by name — this file is
maintained with the waves, and the wave receipts (campaign records) carry the per-claim evidence.*
