# RELEASE-CHECKLIST — the end-to-end "is it ready" list

Run top to bottom before any push that claims "ready". Every item is a **command + expected
result**; anything else = not ready. Gates and live drills are **parent/owner-run** (this repo's
convention: writers never run docker/live; the wave verifier + parent own the definitive pass).

## 1. Repo hygiene (2 min)
- [x] `git status --short` — base `3873450` + exactly the wave-V17 lane edits (L1–L5 files); no `.env`,
      no `backups/`, no strays. Evidence: `wave-v17/l5-release-ops/out/checklist-1-hygiene.txt`.
- [x] `.env` is ignored: `git check-ignore .env` prints `.env`; `git ls-files --error-unmatch .env`
      → path not tracked. (same evidence file)
- [x] Secret scan over tracked content — **one match only**, the AWS documentation placeholder
      `AKIAIOSFODNN7EXAMPLE` in `backend/tests/unit/test_fix3_scanner.py` (a fixture asserting that
      credential shapes are flagged); zero `sk_live_*`, zero `whsec_*`, zero real `AKIA*` keys.
      Command: `git grep -nE "(sk_live_[0-9a-zA-Z]{10,}|whsec_[0-9a-zA-Z]{20,}|AKIA[0-9A-Z]{16})"`
      (`dev-key`, `polsia_password` and empty values are the intentional defaults).
- [x] No mock/demo/stub introduced in this lane: `scripts/restore.sh` either works against the real
      system or refuses **by name** (13 pins, `backend/tests/unit/test_v17_restore_script.py`); sibling
      lanes' diffs are evidenced in their own receipts.

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
- [ ] Coverage gate holds: `cd backend && python -m pytest tests/unit -q --cov=app` ≥ the ratchet in
      `backend/pyproject.toml` — `[tool.coverage.report] fail_under = 87`, measured at 87.5461%
      (4,984 of 5,693) with `core = "sysmon"` in `[tool.coverage.run]`. The config is the single
      truth; the V18 release-ops lane verified it is coherent (no stale "75" target — that number is
      only named as the project's *original* declared target, now met) and did **not** run the gate.
      The full run stays parent/verifier-owned. Evidence: `wave-v18/l5-smoke-release/raw/coverage-grep.txt`.
- Executed locally in V17 (L5): `ruff check tests/unit/test_v17_restore_script.py` → "All checks
  passed!" (`wave-v17/l5-release-ops/out/checklist-2-5-gates.txt`). `mypy app/`, the full pytest run
  and the coverage ratchet are **UNRUN in this lane** — the verifier runs them once for the wave.

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
- Frontend gates are **not executed in this lane** (verifier L6 runs them once for the wave); this
  lane's only FE-adjacent claim is that no FE file was touched by it.

## 4. CI on the push
- [ ] `ci.yml` green (lint job: ruff hard gate, mypy advisory; unit job: `pytest tests/unit -q` +
      coverage variant).
- [ ] `integration.yml` green (testcontainers suite exits 0 with real Postgres/Redis; Playwright E2E
      on Node 24 — `node:sqlite` needs ≥ 22).
- [ ] `docker-build.yml` green (all images build; stack boots; health-checked).
- [ ] The four standing CI invariants hold: ruff pin matches CI; `setup-node` 24; recursion tests
      platform-dynamic; integration `loop_scope` fix intact.

## 5. Live stack drills (parent/owner; needs Docker + the real `.env`)
The read-only subset was executed by the V17 release-ops lane on 2026-10-18 — evidence in
`wave-v17/l5-release-ops/out/` (`checklist-2-5-gates.txt`, `checklist-5-ws-log.txt`,
`restore-drill-real.txt`, `cleanup-proof.txt`). Nothing below POSTs, mutates or restarts the stack.

- [x] `docker compose config -q` → exit 0 (parses with the real `.env`; phone mount env row included).
- [ ] `docker compose up -d` → all services healthy; one dashboard page renders **real** rows (not
      its empty state).
  - [x] `curl -fsS http://localhost/api/v1/health` → `{"status":"ok"}` (GET-only).
  - [ ] Page-render half — needs a browser session (parent live-check).
- [ ] **Sandbox honesty:** with `SANDBOX_MODE=true`, trigger one send-capable action → it lands as a
      **labelled draft**, not a send; the app-wide sandbox banner shows. *(owner — the probe is a POST)*
- [ ] **Docs auto-hide:** set `SANDBOX_MODE=false`, restart backend → `/docs`, `/redoc`,
      `/openapi.json` = 404; API root `"docs": null`; flip back → 200s. *(owner — needs a restart)*
- [ ] **Rate limit:** burst `curl` the health/unsubscribe/webhook routes → tail of `429`s with the
      named `E_RATE_LIMITED` envelope (not 500/502); other routes unaffected. *(owner — deliberate burst)*
- [ ] **Security headers:** `curl -sI http://localhost/` shows CSP + `X-Frame-Options` +
      `nosniff`; open the routes and confirm zero CSP console violations.
  - [x] Header half — `HTTP/1.1 307` + `Content-Security-Policy: default-src 'self'; …object-src
        'none'` + `X-Frame-Options: SAMEORIGIN` + `X-Content-Type-Options: nosniff`.
  - [ ] Console half — needs a browser session (parent live-check).
- [ ] **WS log:** load the dashboard (feed binds) → the nginx access log has **no**
      `/ws/activity?key=` lines; the feed still updates live.
  - [x] Command corrected: in this image `/var/log/nginx/access.log` is a **symlink to `/dev/stdout`**,
        so the old `docker compose exec nginx tail -20 /var/log/nginx/access.log` **blocks forever**
        (observed: 180 s timeout). Read it as `docker compose logs --since <nginx start> nginx | grep -c
        '/ws/activity?key='` instead.
  - [x] Current run: **0** matches since the container start (`2026-10-15T23:21:51Z`) and
        `access_log off` in the `/ws/` location (`nginx/nginx.conf:127`). The 365 historical matches in
        the same container's log all predate that restart (pre-fix run) — they are history, not a live
        leak. Only 2 log lines exist since the restart, so no WS traffic has been exercised.
  - [ ] Feed-still-updates half — needs a browser session (parent live-check).
- [ ] **Backups:** `docker compose up -d backup` → logs `cycle start` → `database dump written` →
      4× `volume archive written`; `backups/` shows the `.sql` + four tars; `tar tzf
      backups/backend_data.tgz` lists files; `docker compose stop backup` logs a clean stop.
      `make backup` still refuses by name when a volume is missing.
  - [x] Artifact half — `backups/` holds `polsia-2026-10-14.sql` (346,917 B) + `backend_data.tgz`,
        `chroma_data.tgz`, `celery_beat_data.tgz`, `redis_data.tgz`; `tar tzf` lists files in each.
  - [ ] Sidecar cycle/stop leg — parent (L5 must not start compose services).
- [ ] **Data export/wipe:** `GET /api/v1/data/export` downloads a ZIP that opens (manifest + CSVs +
      JSON); `DELETE /api/v1/data?dry_run=true` reports counts and wipes nothing. *(owner — the DELETE
      verb is owner-run even with `dry_run=true`)*
- [x] **Restore drill** — EXECUTED by `make restore-drill SRC=backups` on 2026-10-18 against the real
      `backups/` artifacts: **PASS** — 23/23 public tables, 7/7 core row-count checks
      (`activity_log` 58, `agent_runs` 42, `tasks` 41, `memory_entries` 0, `task_schedules` 0,
      `chat_messages` 17, `company_config` 1), 4/4 volume archives with 14/14 members materialised,
      teardown logged, **no drill container left behind** (`docker ps -a` clean). Never touched the
      compose project; never published a port.
      Evidence: `wave-v17/l5-release-ops/out/restore-drill-real.txt` + `cleanup-proof.txt`.
      The optional full-stack boot (README "Restore drill", steps 1–5) remains a parent live-check.
- [x] **One-command acceptance (`make smoke`)** — executed 2026-10-18 against the live stack
      (`GET`/`HEAD` only): **8 PASS** (health `{"status":"ok"}`; CSP + `SAMEORIGIN` + `nosniff`;
      401 `E_AUTH` without a key; 200 envelope with it, 20 items; capabilities `agents=10
      sorted=True registry_tools=3`; phone `store_status=ok unread=54 threads=10`; backup artifacts
      1 dump + 4/4 archives), **1 named SKIP** (`/docs` is served by the Next app at the nginx base —
      `--api-base` checks the API itself), **2 named FAIL**: `phone-media` (the route is absent from
      the running image — L2 lands it this wave; rebuild, then re-run) and `server-header`
      (`Server: nginx/1.31.6` — nginx discloses its version; **HANDOFF** to the nginx owner:
      `server_tokens off;` at `http{}` scope, then reload). Exit 1 from the script (make reports a
      failed recipe as exit 2), as designed.
      Evidence: `wave-v18/l5-smoke-release/raw/live-smoke.txt`; pins
      `backend/tests/unit/test_v18_smoke_script.py` (14 tests: 13 shim/static + 1 live).
      **Re-run `make smoke` at settle**, after the backend image is rebuilt with the V18 media route.

## 6. V16 feature drills
- [x] **Phone (fixture):** with an E5-style fixture store mounted via `PHONE_LINK_DB_DIR`,
      `/api/v1/phone/summary|messages|calls` return real fixture rows; null ≠ 0 enforced; export zip
      opens and contains `messages.json`, `calls.csv`, `contacts.json`.
      Evidence: 138 phone+capability unit tests, exit 0 (`wave-v17/l5-release-ops/out/checklist-6-phone-capabilities.txt`).
- [x] **Phone (real store, parent-only):** point `PHONE_LINK_DB_DIR` at the real Phone Link store →
      `/phone/summary` shows real threads/unread; `/phone` page + dashboard card render real data;
      `codes` route writes its `activity_log` audit row; activity tick twice → **one** event.
      Evidence: `wave-v16/v-final/live-drill2.txt` lines 15–21 — P1 `store_status='ok'`, unread 57,
      threads 10 · P2 messages · P3 calls 50/missed 38 · P4 codes 10 rows · P5 export zip members.
      Executed 2026-10-15 (V16 close).
- [x] **Phone refusal:** unmount/point at a missing path → 503 `E_PHONE_UNAVAILABLE` **by name**
      everywhere (routes + card + page states), never fabricated data, never a 500.
      Evidence: `wave-v16/v-verify/VERIFY.md:37` — hostile-absent / hostile-partial 6/6 each, five
      routes 503 with the missing file NAMED; the named-refusal pins are inside the 138-test run above.
- [x] **Capabilities:** `GET /api/v1/ops/capabilities` equals the registry-derived set (parity pin
      green); agent contexts carry their real tool list; the UI page renders agents → tools →
      actions honestly (empty registry → honest empty state).
      Evidence: `wave-v16/v-final/live-drill2.txt:22-23` — P6 `agents=10 registry_tools=3 with_tools=5`,
      sorted; drift simulation flips the parity pin (`wave-v16/v-verify/VERIFY.md`).
- [x] **Docs truth at close:** README links resolve (USAGE.md, DEPENDENCIES.md, RELEASE-CHECKLIST.md,
      NEXTSTEPS.md, PHONELINK-MCP-TEST.md all present); `PHONELINK-MCP-TEST.md` status line matches
      shipped reality; every `make` target named in docs exists (`make -n <target>`).
      Evidence: `wave-v17/l5-release-ops/out/docs-link-target-audit.txt` — every markdown link in the
      six docs resolves; every `make <target>` named in them exists (`make -n` clean); zero
      "shipping/in flight" phone claims remain.

## 7. Release actions (after all boxes)
- [ ] Commit with the wave summary (gates + live-drill results stated honestly, skips named).
- [ ] Push; watch CI to green (§4).
- [ ] Rollback point recorded: the previous commit = the rollback base; backups exist in `backups/`.
- [ ] Repo description/topics updated if the wave changed what the repo is.
- [x] NEXTSTEPS.md updated: items crossed off with their evidence; new in-flight items listed
      (§3b — the V17 block: restore drill executed, checklist status, docs truth pass).

**Not ready if:** any gate has failures not named as pre-existing, any drill shows a fabricated
state (empty-as-error, 0-as-null, success-on-refusal), any doc claims a feature the frozen tree
does not have, or any secret appears in tracked content.
