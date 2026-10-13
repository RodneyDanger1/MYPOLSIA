# Polsia — AI Business Agent Platform

An autonomous multi-agent system that runs your business on autopilot. Nine specialized AI agents handle social media, competitor research, email outreach, ads management, customer support, code generation, business planning, finance, and orchestration — all powered by Claude Code CLI in headless mode.

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
     │  (17 tables)    │   │  (task queue)    │  │  (vector mem) │
     └─────────────────┘   └──────────────────┘  └───────────────┘
```

### The 9 Agents

| Agent | Responsibility | Schedule |
|-------|---------------|----------|
| **Orchestrator** | Writes morning plan + evening summary | Morning: hourly, self-gated to the configured company-local hour (default 06:00) — Evening: 20:00 UTC |
| **Business Planning** | Strategy, KPIs, growth recommendations | Via the morning plan (no beat entry) |
| **Competitor Research** | Web search, profile updates | Via the morning plan (no beat entry) |
| **Social Media** | Draft + post tweets | Every 2h |
| **Email Outreach** | Prospect finding + cold email sequences | On demand — no beat entry creates it |
| **Customer Support** | Read inbox, draft replies | Every 3h |
| **Ads Management** | Google + Meta campaign optimization | Every 6h |
| **Code Generation** | Ship features, open PRs | On demand |
| **Finance** | Stripe revenue sync, spend tracking | Every 6h |
| **Maintenance** | Task-lease sweep — requeues tasks whose worker died mid-run | Every 5 min |
| **Maintenance** | Activity retention — deletes `activity_log` rows older than `activity_retention_days` (default 90) | Daily at 03:00 UTC |

Each agent calls `claude -p "..." --output-format json` as a subprocess. No API key is needed — the Claude Code CLI authenticates via OAuth stored at `~/.claude`.

## Prerequisites

- Docker + Docker Compose
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed and authenticated (`claude login`)
- (Optional for real operations) Stripe, Twitter, SendGrid, Tavily, Google Ads, Meta Ads accounts

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

## Configuration

### Essential settings (`.env`)

| Variable | Description |
|----------|-------------|
| `API_KEY` | Secret key for the dashboard API (`X-API-Key` header) |
| `SANDBOX_MODE` | `true` = no real posts/emails/charges (default) |
| `CLAUDE_CLI_PATH` | Path to the `claude` binary. The setting's default is `claude` (found on `PATH`); the container image's ENV sets `/usr/local/bin/claude` (`backend/Dockerfile:25`) |

The Claude CLI authenticates via the `~/.claude` directory on your host, which is mounted read-only into the containers. Run `claude login` on your host once before starting the stack.

### Integrations

Fill in the relevant sections of `.env` to activate each integration:

- **Twitter/X** — Post social content
- **SendGrid + IMAP** — Send cold emails (SendGrid is wired); read support inbox — advertised, not
  implemented: the `IMAP_*` settings are not read by any code path
- **Tavily** — Web search for competitor research — advertised, not implemented: `TAVILY_API_KEY`
  is not read by any code path
- **Google Ads / Meta Ads** — Campaign management (advertised, not implemented:
  `GOOGLE_ADS_CLIENT_SECRET` is not read by any code path; the other Google Ads settings are)
- **Stripe** — Revenue and expense tracking (advertised, not implemented:
  `STRIPE_PUBLISHABLE_KEY` is not read by any code path — `STRIPE_SECRET_KEY` and
  `STRIPE_WEBHOOK_SECRET` are)
- **GitHub** — Open PRs from the code generation agent
- **Deployment (Render)** — advertised, not implemented: `RENDER_API_KEY` is not read by any code
  path (there is no Deployment agent — see `.env.example` and `docs/ACCEPTANCE_TESTING.md`)

## Known gaps (measured, not hidden)

Things this repository declares or advertises that are **not** yet true, recorded here instead of being
quietly relaxed. Each one names its measurement.

- **Backend coverage is 87.5%** (4,984 of 5,693 statements; `pytest tests/unit tests/parity tests/redteam
  --cov=app` with the `sysmon` core pinned in `pyproject.toml` — the truthful tracer for async route
  lines, measured by R26/R27/R28; it was 51.48% at the first measurement and 73.0% before the R17–R32
  hardening waves). This **meets the `fail_under = 75` this project originally declared**; the gate is
  now a **ratchet at the measured floor (87)**: it fails the moment coverage drops below what was
  measured and moves up as the suite grows.
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
  `tests/unit/celery/test_send_cap_producer_worker_e2e.py`. Residuals, named not hidden: channels with
  no counting source — github, google_ads, meta_ads, stripe, render — have their configured caps refused
  by name (`POLSIA-E114`, `cap_unknown`) instead of being read as zero sends; customer_support replies
  are not yet counted; LinkedIn has no send adapter.)
- **Nine advertised settings are read by no code path** (keep-as-reserved vs remove is an owner call):
  `TASK_BATCH_SIZE`, `DB_BACKLOG_HOURS`, `IMAP_HOST`/`IMAP_USER`/`IMAP_PASSWORD`, `TAVILY_API_KEY`,
  `GOOGLE_ADS_CLIENT_SECRET`, `STRIPE_PUBLISHABLE_KEY`, `RENDER_API_KEY`. Each carries a
  `RESERVED — no consumer yet` label where it is defined, with its intended purpose named (R23), rather
  than being left advertising. Three former members of this list are now honest: `DEBUG` and `LOG_JSON`
  are consumed by `app/main.py` (R22), and `CELERY_WORKER_CONCURRENCY` is documented as owned by the
  docker-compose launch command (`--concurrency=4`), not the app (R20/R23).

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
│   │   ├── core/            # DB, Redis, ChromaDB, security, retry
│   │   ├── models/          # SQLAlchemy ORM (17 tables)
│   │   ├── schemas/         # Pydantic request/response models
│   │   └── services/        # Business logic layer
│   ├── celery_app/          # Celery worker, Beat schedule, tasks
│   ├── alembic/             # Database migrations
│   └── tests/
│       ├── unit/            # SQLite in-memory, CLAUDE_CLI_MOCK=true
│       └── integration/     # Testcontainers (real Postgres + Redis)
├── frontend/
│   └── src/
│       ├── app/             # Next.js 14 pages (9 routes)
│       ├── components/      # ActivityFeed, MetricsCard, AgentStatusGrid, Sidebar
│       ├── hooks/           # useActivityFeed (WebSocket), useAgentStatus (polling)
│       └── lib/             # Typed API client
├── e2e/                     # Playwright end-to-end tests
├── nginx/                   # Reverse proxy config
├── scripts/                 # init_db.sh, seed_company.py
├── docker-compose.yml
├── docker-compose.ci.yml    # CI override (CLAUDE_CLI_MOCK=true, no nginx)
└── Makefile
```

## CI/CD

Three GitHub Actions workflows run automatically:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | PR + push to main | Lint (ruff, mypy, ESLint), unit tests |
| `integration.yml` | Push to main | Integration tests (testcontainers), Playwright E2E |
| `docker-build.yml` | Push to main | Build all images, health-check the stack |

All CI jobs set `CLAUDE_CLI_MOCK=true` — no Claude credentials are needed in GitHub Actions.

## Turning off sandbox mode

When you're ready to let agents operate for real:

1. Set `SANDBOX_MODE=false` in `.env`
2. Fill in the relevant API keys for the integrations you want active
3. Restart the stack: `make down && make up`

Agents will now post to Twitter, send emails, create ad campaigns, and issue Stripe operations.
