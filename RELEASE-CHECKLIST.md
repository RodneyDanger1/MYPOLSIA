# RELEASE-CHECKLIST — the end-to-end "is it ready" list

Run top to bottom before any push that claims "ready". Every item is a **command + expected
result**; anything else = not ready. Gates and live drills are **parent/owner-run** (this repo's
convention: writers never run docker/live; the wave verifier + parent own the definitive pass).

## 1. Repo hygiene (2 min)
- [ ] `git status --short` — only the intended files (no strays, no `.env`, no `backups/`).
- [ ] `.env` is ignored: `git check-ignore .env` prints a path.
- [ ] Secret scan over tracked content — expect **no matches** except documented placeholders:
      `git grep -nE "(sk_live_[0-9a-zA-Z]{10,}|whsec_[0-9a-zA-Z]{20,}|AKIA[0-9A-Z]{16})"`
      (`dev-key`, `polsia_password` and empty values are the intentional defaults).
- [ ] No mock/demo/stub paths introduced: `git diff <base>..HEAD --stat` reviewed; every new
      handler either works against the real system or refuses **by name**.

## 2. Static gates (backend)
```bash
cd backend
ruff check app/ celery_app/ tests/          # → "All checks passed!" (CI hard gate)
mypy app/                                    # → "Success: no issues found" (project bar;
                                             #    CI keeps mypy advisory with `|| true`)
python -m pytest tests/unit tests/redteam tests/parity -q   # → 0 failed / 0 errors
```
- [ ] Full backend suite green with the expected tally (junit xml attached to the wave receipt);
      skipped count named (testcontainers tests skip without Docker — integration runs in CI).
- [ ] Coverage gate holds: `python -m pytest tests/unit -q --cov=app` ≥ the ratchet in
      `pyproject.toml` (`fail_under`).

## 3. Static gates (frontend)
```bash
cd frontend
npm test                                     # jest — run it TWICE; both runs green (flaky checks)
npx tsc -p tsconfig.json --noEmit            # → exit 0
npm run build                                # `next build` → exit 0 (own gate, not folded into tests)
```
- [ ] Full jest suite green ×2, same totals both runs.
- [ ] `next build` shows every route; new routes appear (phone `/phone`; capabilities surface) and
      no prerender-baked empty state regression (client components where the repo pattern says so).

## 4. CI on the push
- [ ] `ci.yml` green (lint job: ruff hard gate, mypy advisory; unit job: `pytest tests/unit -q` +
      coverage variant).
- [ ] `integration.yml` green (testcontainers suite exits 0 with real Postgres/Redis; Playwright E2E
      on Node 24 — `node:sqlite` needs ≥ 22).
- [ ] `docker-build.yml` green (all images build; stack boots; health-checked).
- [ ] The four standing CI invariants hold: ruff pin matches CI; `setup-node` 24; recursion tests
      platform-dynamic; integration `loop_scope` fix intact.

## 5. Live stack drills (parent/owner; needs Docker + the real `.env`)
- [ ] `docker compose config -q` → exit 0 (parses with the real `.env`; phone mount env row included).
- [ ] `docker compose up -d` → all services healthy; `curl http://localhost/api/v1/health` →
      `{"status":"ok"}`; one dashboard page renders **real** rows (not its empty state).
- [ ] **Sandbox honesty:** with `SANDBOX_MODE=true`, trigger one send-capable action → it lands as a
      **labelled draft**, not a send; the app-wide sandbox banner shows.
- [ ] **Docs auto-hide:** set `SANDBOX_MODE=false`, restart backend → `/docs`, `/redoc`,
      `/openapi.json` = 404; API root `"docs": null`; flip back → 200s.
- [ ] **Rate limit:** burst `curl` the health/unsubscribe/webhook routes → tail of `429`s with the
      named `E_RATE_LIMITED` envelope (not 500/502); other routes unaffected.
- [ ] **Security headers:** `curl -sI http://localhost/` shows CSP + `X-Frame-Options` +
      `nosniff`; open the routes and confirm zero CSP console violations.
- [ ] **WS log:** load the dashboard (feed binds) → `docker compose exec nginx tail -20
      /var/log/nginx/access.log` has **no** `/ws/activity?key=` lines; the feed still updates live.
- [ ] **Backups:** `docker compose up -d backup` → logs `cycle start` → `database dump written` →
      4× `volume archive written`; `backups/` shows the `.sql` + four tars; `tar tzf
      backups/backend_data.tgz` lists files; `docker compose stop backup` logs a clean stop.
      `make backup` still refuses by name when a volume is missing.
- [ ] **Data export/wipe:** `GET /api/v1/data/export` downloads a ZIP that opens (manifest + CSVs +
      JSON); `DELETE /api/v1/data?dry_run=true` reports counts and wipes nothing.
- [ ] **Restore drill** on a scratch compose project (README "Restore drill") — never the live stack.

## 6. V16 feature drills
- [ ] **Phone (fixture):** with an E5-style fixture store mounted via `PHONE_LINK_DB_DIR`,
      `/api/v1/phone/summary|messages|calls` return real fixture rows; null ≠ 0 enforced; export zip
      opens and contains `messages.json`, `calls.csv`, `contacts.json`.
- [ ] **Phone (real store, parent-only):** point `PHONE_LINK_DB_DIR` at the real Phone Link store →
      `/phone/summary` shows real threads/unread; `/phone` page + dashboard card render real data;
      `codes` route writes its `activity_log` audit row; activity tick twice → **one** event.
- [ ] **Phone refusal:** unmount/point at a missing path → 503 `E_PHONE_UNAVAILABLE` **by name**
      everywhere (routes + card + page states), never fabricated data, never a 500.
- [ ] **Capabilities:** `GET /api/v1/ops/capabilities` equals the registry-derived set (parity pin
      green); agent contexts carry their real tool list; the UI page renders agents → tools →
      actions honestly (empty registry → honest empty state).
- [ ] **Docs truth at close:** README links resolve (USAGE.md, DEPENDENCIES.md, RELEASE-CHECKLIST.md,
      NEXTSTEPS.md, PHONELINK-MCP-TEST.md all present); `PHONELINK-MCP-TEST.md` status line matches
      shipped reality; every `make` target named in docs exists (`make -n <target>`).

## 7. Release actions (after all boxes)
- [ ] Commit with the wave summary (gates + live-drill results stated honestly, skips named).
- [ ] Push; watch CI to green (§4).
- [ ] Rollback point recorded: the previous commit = the rollback base; backups exist in `backups/`.
- [ ] Repo description/topics updated if the wave changed what the repo is.
- [ ] NEXTSTEPS.md updated: items crossed off with their evidence; new in-flight items listed.

**Not ready if:** any gate has failures not named as pre-existing, any drill shows a fabricated
state (empty-as-error, 0-as-null, success-on-refusal), any doc claims a feature the frozen tree
does not have, or any secret appears in tracked content.
