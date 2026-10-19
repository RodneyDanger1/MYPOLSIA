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
      **V20 lane L3 measured 86.66% (11,631 statements, 1,551 missed) mid-wave on an unfrozen tree and
      did NOT earn a ratchet move — `fail_under = 87` is byte-identical (`backend/pyproject.toml`
      sha256 `987779171769c37ca1e25fa388ba211ee2a1cfc52b9c9bbeb046fbffaa486ffe`, `cmp` silent vs
      `pre/pyproject.toml.pre`). The parent re-measures on the frozen tree; a settled number below 87.0
      requires targeted tests, not a lowered floor.** The full run stays parent/verifier-owned.
      Evidence: `wave-v20/l3-proof-depth/raw/coverage-run.log`; base artifact
      `wave-v18/l5-smoke-release/raw/coverage-grep.txt`.
- Executed locally in V17 (L5): `ruff check tests/unit/test_v17_restore_script.py` → "All checks
  passed!" (`wave-v17/l5-release-ops/out/checklist-2-5-gates.txt`). `mypy app/`, the full pytest run
  and the coverage ratchet are **UNRUN in this lane** — the verifier runs them once for the wave.
- **V20 known settlement item (must clear before the full suite is green):** L2's receipt hands the
  parent four V12-era pin files outside its territory — `test_v12_registry.py`,
  `test_v12_allowlist.py`, `test_v12_seam_end_to_end.py`, `test_v12_tool_loop_registry.py` — which fail
  by design (**9 failed / 95** in `wave-v20/l2-agent-phone-tool/raw/NEIGHBORS-v12.*`); the exact
  re-pins are in `wave-v20/l2-agent-phone-tool/REQUESTS-v12-pins.md`. The lane's own files are
  **68/68 green**. A "0 failed" claim in §2 is false until those re-pins land.

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
- [x] **One-command acceptance, V19 surfaces (`make smoke`)** — re-run 2026-10-18 against the live
      stack (GET/HEAD only): **10 PASS, 2 named SKIP, 0 FAIL — exit 0**. Both V18 FAILs are closed on
      the running stack: `phone-media` returns `parts=17 truncated=False thread=7`, and
      `server-header` reads `Server: nginx` (masked). New: `phone-codes` → `200 codes=1 shape=True
      (values never printed)`; `send-bridge` → **named SKIP** (`PHONE_SEND_BRIDGE_URL` unset — bridge
      OFF, the default). The `docs` SKIP is the nginx↔Next `/docs` routing, unchanged.
      Evidence: `wave-v19/l5-smoke-docs/raw/live-smoke.txt` (dry run:
      `wave-v19/l5-smoke-docs/raw/dry-run.txt`); pins
      `backend/tests/unit/test_v18_smoke_script.py` (22 tests: 21 shim/static + 1 live).
- [ ] **Verification-codes panel (V19, parent live-check)** — on `/phone`, open the Codes section:
      idle makes **no** request; one click → exactly one `GET /api/v1/phone/codes?...`; rows are
      masked to the last two digits; **Reveal** shows one row (`aria-pressed`); **Copy** copies the
      full code and re-masks; no localStorage/sessionStorage/console value; every fetch writes
      **one** activity row (count + window, values absent). Evidence: browser network tab + feed.
- [ ] **Send bridge (V19, parent live-check; never send an SMS here)** — with
      `PHONE_SEND_BRIDGE_TOKEN` exported (≥24 chars) start `python scripts/phone_send_bridge.py
      --host 127.0.0.1 --port 9227`; `GET /health` → `{status:"ok",paired:…}`; a bad/missing token on
      `POST /send` → `401 E_AUTH`; with `PHONE_SEND_BRIDGE_URL` exported, `make smoke` PASSes
      `send-bridge`; unset → **named SKIP**. The real send stays owner-gated (USAGE §5.2). The
      fail path is already proven with the URL pointed at a dead loopback port: exit 1, one named
      `FAIL send-bridge: … no response` (`wave-v19/l5-smoke-docs/raw/live-bridge-fail.txt`).
- [ ] **`phone_read` through the firewalled path (V20, parent live-check; inside a run, not over HTTP)** —
      after the L2 lane lands and its receipt exists, rebuild the backend image and drive the tool
      **only** through `execute_tool`: `python -c "from app.agents.tool_registry import execute_tool;
      print(execute_tool('phone_read', {}, agent_type='AGENT_TYPE_FROM_L2_RECEIPT'))"` inside the
      backend container. Required: `{ok:true, tool:"phone_read", result:{store_status, threads[]}}`;
      **no code field anywhere in the payload**; a thread-scoped call returns capped
      `messages[]`; an agent the receipt does NOT grant gets the firewall refusal
      (`POLSIA-E114`, "tool 'phone_read' not in allowlist"); with `PHONE_LINK_DB_DIR` pointed at a
      missing path the answer is the named `E_PHONE_UNAVAILABLE` payload, never an empty thread list.
      L2's receipt is **complete** (RED 29/29 → GREEN 68/68 on its four files; one real-store firewalled
      call: `ok=true`, `store_status=ok`, `thread_count=10`, `unread_total=10`, `truncated=true`), so the
      tool-path half is proven at ASGI + live-probe level. What remains **after the settle rebuild** is
      this checklist's wire-level pass: `GET /ops/capabilities` must list `phone_read` on exactly
      `orchestrator` + `customer_support` (the running image is the base build), plus the four
      `execute_tool` legs above on the rebuilt stack. `REQUESTS-v12-pins.md` (four V12-era pin files,
      9 failed / 95) must be applied at settle first.
- [ ] **Agents-page toolbox (V20, parent live-check; visual)** — L4's receipt is **complete**
      (`wave-v20/l4-agents-toolbox/RECEIPT.md`: RED 8/8 → 10/10 pins, neighbours 98/98, `tsc` exit 0),
      but the served frontend is still the V19 build. Rebuild the frontend image, then: open `/agents`,
      expand a card's `TOOLS · N`, and confirm the listed names + descriptions match
      `GET /api/v1/ops/capabilities` for that `agent_type`. Required: collapsed by default (no tool
      markup until expanded); a `tools: []` agent says **“No tools are granted to this agent.”**; an
      absent/unreadable row says “The capability map has no readable entry for this agent.”; a failed
      capabilities read shows **`TOOLS — UNAVAILABLE`** + the shared error sentence + a working Retry
      (never “no tools”); Escape collapses and returns focus. Visual **UNRUN** at this write.
- [ ] **V20 e2e specs against the rebuilt FE (parent live-check)** — after the FE rebuild:
      `cd e2e && npx playwright test v20-codes-panel.spec.ts invoices.spec.ts --reporter=list` against
      `http://127.0.0.1` → expect **7/7** (5 codes-panel + 2 invoices). Both files enumerate today
      (`--list` exit 0, 7 tests / 2 files) but have **never run green**: the served build predates the
      V19 panel (codes **4 failed / 1 passed**; invoices **1 failed**; chunk markers `phone-codes: 0`).
      Also re-check the served chunk contains `phone-codes` + `within_hours` (read-only curl).
      Evidence: `wave-v20/l3-proof-depth/raw/02-codes-vs-stale-live.txt`,
      `raw/03-invoices-terminal-vs-stale-live.txt`, `raw/00-served-build-check.txt`.

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
      (§3e — the V20 block: **all five lanes SETTLED** with complete receipts, verifier PASSed every
      one; the V12 pin family re-pinned by the parent post-audit; the stale E7 bullet in §4 replaced
      with the V17-L1 closed statement; §4's V19 API cards closed in V20).

**Not ready if:** any gate has failures not named as pre-existing, any drill shows a fabricated
state (empty-as-error, 0-as-null, success-on-refusal), any doc claims a feature the frozen tree
does not have, or any secret appears in tracked content.
