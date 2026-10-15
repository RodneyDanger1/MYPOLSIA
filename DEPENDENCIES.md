# MYPOLSIA — Dependencies & Requirements (everything needed to run it properly)

This file is the complete, link-by-link setup guide. The short version: **MYPOLSIA runs as a Docker
Compose stack** (backend + frontend + Postgres + Redis + workers + nginx), so on Windows the chain is
**WSL2 → Docker Desktop → (GNU) Make → the stack**. Python/Node are only needed for running tests or
developing outside Docker.

## 1) Required — core runtime

| # | Dependency | Why | Install (Windows + winget) | Verify |
|---|---|---|---|---|
| 1 | **Windows 10/11 (x64)** with **virtualization enabled in BIOS** | host OS | — | `systeminfo` shows Hyper-V requirements met |
| 2 | **WSL2** | Docker Desktop's backend on Windows | In an **admin** PowerShell: `wsl --install` then reboot ([guide](https://learn.microsoft.com/windows/wsl/install)) | `wsl --status` |
| 3 | **Docker Desktop** (WSL2 backend) | runs the entire stack (`docker compose up`) | `winget install -e --id Docker.DockerDesktop` ([download](https://www.docker.com/products/docker-desktop/), [WSL2 backend docs](https://docs.docker.com/desktop/windows/wsl/)); after install: Settings → Resources → enable WSL integration | `docker --version` and `docker compose version` |
| 4 | **GNU Make for Windows** | `make init-db`, `make test`, `make backup`, … | `winget install -e --id ezwinports.make` ([ezwinports](https://sourceforge.net/projects/ezwinports/)) — if `make` isn't on PATH, use its full path or run the underlying commands directly (see README §Make commands) | `make --version` (expect GNU Make 4.x) |
| 5 | **Git** | clone the repo | `winget install -e --id Git.Git` ([git-scm.com](https://git-scm.com/download/win)) | `git --version` |

**Hardware minimums:** 8 GB RAM (16 GB comfortable), ~10 GB free disk for images + Postgres data,
TCP port **80** free on localhost (nginx publishes it).

## 2) Required — to actually use the agents

| Dependency | Why | Get it | Notes |
|---|---|---|---|
| **DeepSeek API key** | the default agent runtime | [platform.deepseek.com](https://platform.deepseek.com) → API keys | entered in the app after first start (stored server-side), or preset in `.env` |
| **(or) Claude Code CLI** | fallback runtime | [docs.anthropic.com/claude-code](https://docs.anthropic.com/claude-code) → `claude login` | optional; DeepSeek-first by design |

## 3) Dev/test-only (skip if you only run the stack)

| Dependency | Why | Install | Verify |
|---|---|---|---|
| **Python 3.12** | backend tests/dev without Docker | `winget install -e --id Python.Python.3.12` ([python.org](https://www.python.org/downloads/)) | `python --version` |
| **Node.js ≥20** (≥22 recommended; **24 for CI parity**) | frontend dev; the Playwright E2E suite uses `node:sqlite` (Node ≥22) | `winget install -e --id OpenJS.NodeJS.LTS` ([nodejs.org](https://nodejs.org)) | `node --version` |
| Docker running (from §1) | integration tests spin real Postgres/Redis via **testcontainers**; E2E starts the stack | — | `docker info` |

## 4) Optional — real integrations (all start in sandbox/test mode)

| Integration | What for | Link |
|---|---|---|
| **Stripe CLI** | local webhook forwarding (`stripe listen`) | `winget install -e --id Stripe.StripeCli` · [docs.stripe.com/stripe-cli](https://docs.stripe.com/stripe-cli) |
| Stripe account (test mode is free) | billing, invoices | [dashboard.stripe.com/register](https://dashboard.stripe.com/register) |
| Twitter/X developer app | social posting | [developer.x.com](https://developer.x.com) |
| SendGrid | outreach email sending | [sendgrid.com](https://sendgrid.com) |
| Google Ads API | ads management agent | [developers.google.com/google-ads](https://developers.google.com/google-ads/api/docs/get-started/introduction) |
| Meta Marketing API | ads management agent | [developers.facebook.com/docs/marketing-apis](https://developers.facebook.com/docs/marketing-apis) |
| **Windows Phone Link** | future "phone" features (Hermes-side bridge today — see `PHONELINK-MCP-TEST.md`) | built into Windows 10/11 |

## 5) First run (after §1–§2)

```bash
git clone https://github.com/RodneyDanger1/MYPOLSIA.git
cd MYPOLSIA
cp .env.example .env            # then edit .env (see next line)
#   API_KEY=<your own key>  ·  SANDBOX_MODE=true  ·  DEEPSEEK_API_KEY=<optional, can be set in-app>
export NEXT_PUBLIC_API_KEY=dev-key    # must match API_KEY (build-time inlined)
docker compose up -d
make init-db
# Open http://localhost  ·  health: http://localhost/api/v1/health → {"status":"ok"}
```

## 6) Verify the whole chain in 30 seconds

```bash
wsl --status                      # WSL2 installed
docker --version && docker compose version
make --version
curl http://localhost/api/v1/health
```

## 7) Troubleshooting quick table

| Symptom | Cause | Fix |
|---|---|---|
| `port 80 already in use` | another web server | stop it, or change nginx's published port in `docker-compose.yml` |
| `docker: command not found` / daemon errors | Docker Desktop not running | start Docker Desktop; ensure WSL integration for your distro is ON |
| `make: command not found` | Make not on PATH | use the full WinGet path, or run the make target's underlying commands (README §Make commands) |
| `wsl --install` fails | virtualization off in BIOS | enable VT-x/AMD-V, retry as admin |
| integration tests skipped | testcontainers missing / Docker down | `uv pip install "testcontainers[postgres,redis]"`, start Docker |
| Playwright `No such built-in module: node:sqlite` | Node < 22 | install Node ≥22 (`winget install -e --id OpenJS.NodeJS`) |
| `npm ci` fails on a stale lockfile | lockfile drift | `npm install` (same fallback the CI uses) |

## 8) CI parity (what GitHub Actions installs)

`.github/workflows/` (ci.yml, integration.yml, docker-build.yml) install exactly the pinned versions in
`backend/requirements-dev.txt` (pytest 9.1.1 · pytest-asyncio · ruff · mypy) on Python 3.12 + Node 24,
and run: ruff+mypy, unit tests, testcontainers integration, Playwright E2E, and a full image build +
health-checked stack boot. Local tools matching those pins = local results predict CI results.
