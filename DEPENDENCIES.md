# MYPOLSIA — Dependencies & Requirements (everything needed to run it properly)

This file is the complete, link-by-link setup guide. The short version: **MYPOLSIA runs as a Docker
Compose stack** (backend + frontend + Postgres + Redis + workers + nginx), so on Windows the chain is
**WSL2 → Docker Desktop → (GNU) Make → the stack**. Python/Node are only needed for running tests or
developing outside Docker. Once it boots, the operator's guide is [USAGE.md](USAGE.md) and the
readiness pass is [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md).

## 1) Required — core runtime

| # | Dependency | Why | Install (Windows + winget) | Verify |
|---|---|---|---|---|
| 1 | **Windows 10/11 (x64)** with **virtualization enabled in BIOS** | host OS | — | `systeminfo` shows Hyper-V requirements met |
| 2 | **WSL2** | Docker Desktop's backend on Windows | In an **admin** PowerShell: `wsl --install` then reboot ([guide](https://learn.microsoft.com/windows/wsl/install)) | `wsl --status` |
| 3 | **Docker Desktop** (WSL2 backend) | runs the entire stack (`docker compose up`) | `winget install -e --id Docker.DockerDesktop` ([download](https://www.docker.com/products/docker-desktop/), [WSL2 backend docs](https://docs.docker.com/desktop/windows/wsl/)); after install: Settings → Resources → enable WSL integration | `docker --version` and `docker compose version` |
| 4 | **GNU Make for Windows** | `make init-db`, `make test`, `make backup`, … | `winget install -e --id ezwinports.make` ([ezwinports](https://sourceforge.net/projects/ezwinports/)) — if `make` isn't on PATH, use its full path or run the underlying commands directly (see README §Make commands) | `make --version` (expect GNU Make 4.x); `make -n init-db` dry-runs a target without executing it |
| 5 | **Git** | clone the repo | `winget install -e --id Git.Git` ([git-scm.com](https://git-scm.com/download/win)) | `git --version` |

**Hardware minimums:** 8 GB RAM (16 GB comfortable), ~10 GB free disk for images + Postgres data,
TCP port **80** free on localhost — nginx publishes `127.0.0.1:80` (loopback only, never the LAN).

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
| `check_deps.py` (repo root — no install) | audits what the build needs vs what your venv can import (stdlib import check) | — | run it with the repo venv: `.venv\Scripts\python.exe check_deps.py` → per-module OK/MISSING |

## 4) Optional — real integrations (all start in sandbox/test mode)

| Integration | What for | Link |
|---|---|---|
| **Stripe CLI** | local webhook forwarding (`stripe listen`) | `winget install -e --id Stripe.StripeCli` · [docs.stripe.com/stripe-cli](https://docs.stripe.com/stripe-cli) |
| Stripe account (test mode is free) | billing, invoices | [dashboard.stripe.com/register](https://dashboard.stripe.com/register) |
| Twitter/X developer app | social posting | [developer.x.com](https://developer.x.com) |
| SendGrid | outreach email sending | [sendgrid.com](https://sendgrid.com) |
| Google Ads API | ads management agent | [developers.google.com/google-ads](https://developers.google.com/google-ads/api/docs/get-started/introduction) |
| Meta Marketing API | ads management agent | [developers.facebook.com/docs/marketing-apis](https://developers.facebook.com/docs/marketing-apis) |
| **Windows Phone Link** | read-only phone data: the **Hermes-side bridge works today**; the Polsia-side read path (read-only store mount via `PHONE_LINK_DB_DIR`, `GET /api/v1/phone/*`, `/phone` page) is **live since V16** — proven against the real store 2026-10-15 (`PHONELINK-MCP-TEST.md`). The optional send driver additionally needs Google Messages web paired in a browser | built into Windows 10/11 |

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

First configuration (company profile, sender identity, owner profile, DeepSeek key) is the checklist
in [USAGE.md](USAGE.md) §3; the day-to-day guide is §4 of the same file.

## 6) Verify the whole chain in 30 seconds

```bash
wsl --status                      # WSL2 installed
docker --version && docker compose version
make --version
curl http://localhost/api/v1/health
make smoke                        # the full GET-only acceptance (needs $API_KEY exported)
```

`make smoke` is the one-command gate: health, security headers, the 401/200 key gate, capabilities,
phone summary + media, `/docs` vs `SANDBOX_MODE`, the proxy `Server` header, and the backup
artifacts — one named PASS/FAIL line each, exit 0 only when all pass. `make smoke-dry` prints the
plan without making a request. It never writes and never starts/stops a service.

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
| `/api/v1/phone/*` answers 503 `E_PHONE_UNAVAILABLE` | Phone Link store not mounted / empty (the phone feature, live since V16) | set `PHONE_LINK_DB_DIR` to the store root and `docker compose up -d`; keep the Phone Link app running for fresh sync (`PHONELINK-MCP-TEST.md`) |
| `make restore-drill` exits 2 with `REFUSED: a container named "polsia-restore-drill-pg" already exists` | a previous drill was interrupted before its trap could tear down | `docker rm -f polsia-restore-drill-pg`, then re-run; the script never reuses or replaces an existing container |
| `make restore-drill` exits 2 with `docker is not available on PATH` | Docker Desktop not running / CLI not on PATH | start Docker Desktop; verify `docker ps` works in the same shell |
| `make smoke` reports `FAIL auth-200: no key in $API_KEY` | the key is read from the **environment** only (never `.env`) | `export API_KEY=…` in the same shell, then re-run; `make smoke-dry` shows whether the key is present |
| `make smoke` reports `FAIL server-header: Server: nginx/x.y.z` | nginx advertises its version (`server_tokens` defaults to `on`) | add `server_tokens off;` at `http{}` scope in `nginx/nginx.conf`, then `docker compose exec nginx nginx -s reload` |

## 8) CI parity (what GitHub Actions installs)

`.github/workflows/` (ci.yml, integration.yml, docker-build.yml) install exactly the pinned versions in
`backend/requirements-dev.txt` (pytest 9.1.1 · pytest-asyncio 1.4.0 · ruff **0.17.0** · mypy 1.13.0 —
the exact pins CI installs) on Python 3.12 + Node 24,
and run: ruff+mypy, unit tests, testcontainers integration, Playwright E2E, and a full image build +
health-checked stack boot. Local tools matching those pins = local results predict CI results.
Local gates: `ruff check app/ celery_app/ tests/` is the **hard** lint gate (CI fails on it);
`mypy app/` is the project's clean bar (CI keeps mypy advisory with `|| true` while the
reconstruction settles).
