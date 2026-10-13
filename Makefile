.PHONY: up down build logs shell-backend migrate seed test lint dev-backend dev-frontend dev-e2e

up:
	docker-compose up -d

down:
	docker-compose down

build:
	docker-compose build

logs:
	docker-compose logs -f

shell-backend:
	docker-compose exec backend bash

migrate:
	docker-compose exec backend alembic upgrade head

seed:
	docker-compose exec backend python scripts/seed_company.py

init-db: migrate seed

test-unit:
	docker-compose exec backend pytest tests/unit/ -v --cov=app --cov-report=term-missing

test-integration:
	docker-compose exec backend pytest tests/integration/ -v -m integration

test: test-unit test-integration

lint-backend:
	docker-compose exec backend ruff check app/
	docker-compose exec backend mypy app/

lint-frontend:
	docker-compose exec frontend npm run lint

lint: lint-backend lint-frontend

# E2E_SQLITE (fix-4 §5): the fixture-seeding specs need the SQLite lab DB.  It is passed through
# ONLY when set, via the `env` command — never as an expanded `E2E_SQLITE=...` assignment prefix
# (a shell assignment built by expansion is not parsed as one) and never as the `:-` literal form,
# which exports an EMPTY string on unset and defeats the specs' "unset -> repo-relative default
# (<repo>/.e2e-lab.sqlite)" law, turning a runnable lab into named skips.
e2e:
	cd e2e && $${E2E_SQLITE:+env E2E_SQLITE="$$E2E_SQLITE"} npx playwright test

reset-db:
	docker-compose exec backend alembic downgrade base
	docker-compose exec backend alembic upgrade head
	$(MAKE) seed

# ── W4-ship (S2-ci): no-Docker dev targets ────────────────────────────────────────────────────
# No Docker on this host, so the compose targets above cannot serve a local dev loop.  Each
# target first stops a previously started standalone frontend: `taskkill //F` is broken on
# this host (A7 trap 5), so the process is matched by command line via CIM instead.  `$$` is
# Make's escape for a literal `$`, so the ${VAR:-default} forms are evaluated by the shell at
# run time — a caller's exported DATABASE_URL / API_KEY / NEXT_PUBLIC_* / BASE_URL wins over
# the default (the rest of the environment passes through to the child by inheritance).

dev-backend:
	powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { \$$_.CommandLine -match 'start-standalone|standalone.server.js' } | ForEach-Object { Stop-Process -Id \$$_.ProcessId -Force }"
	cd backend && DATABASE_URL="$${DATABASE_URL:-sqlite+aiosqlite:///./polsia-dev.sqlite}" CLAUDE_CLI_MOCK=true SANDBOX_MODE=true API_KEY="$${API_KEY:-dev-key}" ../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

dev-frontend:
	powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | Where-Object { \$$_.CommandLine -match 'start-standalone|standalone.server.js' } | ForEach-Object { Stop-Process -Id \$$_.ProcessId -Force }"
	cd frontend && NEXT_PUBLIC_API_KEY="$${NEXT_PUBLIC_API_KEY:-dev-key}" NEXT_PUBLIC_API_URL="$${NEXT_PUBLIC_API_URL:-http://localhost}" npm run build && npm start

dev-e2e:
	cd e2e && BASE_URL="$${BASE_URL:-http://localhost}" $${E2E_SQLITE:+env E2E_SQLITE="$$E2E_SQLITE"} npx playwright test
