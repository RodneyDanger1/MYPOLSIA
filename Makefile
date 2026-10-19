.PHONY: up down build logs shell-backend migrate seed init-db test test-unit test-integration lint lint-backend lint-frontend e2e reset-db dev-backend dev-frontend dev-e2e backup restore restore-drill smoke smoke-dry

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

# ── W10 (H1/D4): backup / restore ─────────────────────────────────────────────────────────────
# `make backup` snapshots the LIVE stack into backups/ (gitignored): a pg_dump of the postgres
# service (backups/polsia-<date>.sql) plus one tar per DATA volume — backend_data (the DeepSeek
# key store + the chroma parent), chroma_data (the vector store), celery_beat_data (the beat
# clock), redis_data (the broker AOF).  Volume names carry the compose PROJECT prefix
# (`polsia_` — the repo directory's name); each one is checked with `docker volume inspect`
# BEFORE the tar because `docker run -v <name>:/d` CREATES a missing volume — without the guard
# a mistyped name would tar an EMPTY volume into a green-looking, worthless backup.  These two
# targets use the `docker compose` v2 plugin binary; the older targets above keep their historic
# `docker-compose` spelling.  `$$` is Make's escape for a literal `$` (the file's existing
# style), so `$$(date +%F)` / `$$(pwd)` are evaluated by the shell at run time.
BACKUP_VOLUMES = backend_data chroma_data celery_beat_data redis_data

backup:
	mkdir -p backups
	docker compose exec -T postgres pg_dump -U $${POSTGRES_USER:-polsia} $${POSTGRES_DB:-polsia} > backups/polsia-$$(date +%F).sql
	@echo "backup: backups/polsia-$$(date +%F).sql written"
	@for vol in $(BACKUP_VOLUMES); do \
		docker volume inspect polsia_$$vol >/dev/null 2>&1 || { echo "make backup: refusing — docker volume polsia_$$vol not found; is the stack up (docker compose up -d)?" >&2; exit 1; }; \
		docker run --rm -v polsia_$$vol:/d -v "$$(pwd)/backups":/b alpine tar czf /b/$$vol.tgz -C /d . || exit 1; \
		echo "backup: backups/$$vol.tgz written"; \
	done

# `make restore SRC=backups` is INSTRUCTIONS-AS-TARGET: it verifies the dump exists, then PRINTS
# the scratch-project drill — the README ("Volumes & Backup") carries the full steps.  It never
# touches the live stack: restore is a DRILL, run against a scratch compose project.
restore:
	@if [ -z "$(SRC)" ]; then \
		echo "make restore: SRC is required — SRC=<dir holding a make-backup output>, e.g. make restore SRC=backups" >&2; exit 1; \
	fi
	@ls "$(SRC)"/polsia-*.sql >/dev/null 2>&1 || { \
		echo "make restore: no polsia-*.sql found in $(SRC) — run 'make backup' first, or point SRC at a real backup dir" >&2; exit 1; \
	}
	@echo "RESTORE IS A DRILL — run it against a SCRATCH compose project, never the live stack. Full steps: README.md, 'Volumes & Backup'."
	@echo "  1. scratch project in its own dir, start from empty: docker compose -p scratch down -v"
	@echo "  2. seed each data volume from its tar: docker run --rm -v scratch_<volume>:/d -v \"$$(pwd)/$(SRC)\":/b alpine sh -c \"tar xzf /b/<volume>.tgz -C /d\""
	@echo "  3. load the dump: docker compose -p scratch up -d postgres && docker compose -p scratch exec -T postgres psql -U $${POSTGRES_USER:-polsia} $${POSTGRES_DB:-polsia} < $(SRC)/polsia-<date>.sql"
	@echo "  4. boot the scratch stack: docker compose -p scratch up -d"
	@echo "  5. smoke: curl -fsS http://localhost/api/v1/health && load one dashboard page"

# `make restore-drill SRC=backups` EXECUTES the restore for real: scripts/restore.sh starts a
# THROWAWAY postgres:16-alpine container (name polsia-restore-drill-pg, no published ports, never
# the compose project), loads the dump, verifies table/row counts against a manifest parsed from
# the dump itself, extracts every volume archive, and always tears the container down via trap.
# Exit 0 = PASS, 1 = FAIL (a named check failed), 2 = refusal (bad/missing artifacts, docker
# absent, or a container with that name already exists).  DRY=1 prints the plan and touches nothing.
restore-drill:
	@sh scripts/restore.sh --src "$${SRC:-backups}" $${DRY:+--dry-run}

# `make smoke` is the ONE-COMMAND acceptance for a RUNNING stack: scripts/smoke.sh issues GET/HEAD
# requests only — health, the three security headers, the key gate both ways (401 E_AUTH without a
# key, 200 envelope with it), the sorted capabilities manifest, the phone summary + media surface
# (real store or a NAMED refusal, never a raw 500), /docs against SANDBOX_MODE, the proxy's Server
# header, and the backup artifacts — and prints one named `PASS/FAIL <check>: <detail>` line each.
# Exit 0 = every check passed, 1 = a check failed, 2 = usage refusal.  It never writes and never
# touches the compose project.  The key comes from the ENVIRONMENT ($API_KEY by default), never
# from `.env`: `export API_KEY=…` first (an inline assignment lands in shell history).
#   make smoke                                            # base http://127.0.0.1
#   make smoke SMOKE_ARGS="--api-base http://127.0.0.1:8000"   # check the API's own /docs too
smoke:
	@sh scripts/smoke.sh $${SMOKE_ARGS:-}

smoke-dry:
	@sh scripts/smoke.sh --dry-run
