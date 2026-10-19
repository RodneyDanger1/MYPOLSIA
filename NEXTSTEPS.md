# NEXTSTEPS — everything remaining, in full detail (maintained through wave V21)
Repo: https://github.com/RodneyDanger1/MYPOLSIA (public). This document is the living map of what is
DONE, what is IN FLIGHT, and exactly what comes NEXT — with where/when/why for each item.

## 0. Where things stand (V19 close at base `c2e1261`; V20 open at base `8fa2b38`)
- **Landed & committed (pushed):** V1–V19 — the full Polsia core: FastAPI + Celery backend, Next.js UI,
  agent orchestration, firewalled tool-calling (DeepSeek-first runtime), compliance-gated outreach,
  Stripe billing/webhooks, finance & planning, memory layer, activity feed, ops console, hardened nginx,
  scheduled backups, phone-in-Polsia (live-proven), capability maps, operator docs. V15 base `3972097`;
  CI-fix rounds `8e9c148` → `a002100` (all three GitHub workflows fully green at `a002100`).
- **V16/V17/V18 are closed and committed.** V18 closed at `257e20c` with CI fully green: backend
  **2522 passed / 0 failed / 0 errors / 13 skipped**, integration **11/0/0/0**, jest **698/698 ×2**
  (the V19 plan's base line; `wave-v18/` carries the per-lane evidence).
- **Wave V19 is CLOSED** — all five lanes landed and the riding verifier PASSed every one (its final
  confirming gate: 2578/0/0/13, matching the parent's independent run); CI all-green at `c2e1261`.
  Details and the three post-audit fixes are in §3d.
- **Wave V20 is SETTLED** — base `8fa2b38`; all five lanes + verifier PASSed, with the V12 pin family
  re-pinned by the parent post-audit (`wave-v20/`). Details in §3e.
- **Wave V21 is SETTLED** — base `e618342` (at that base: backend **2694 passed / 0 failed / 0
  errors / 13 skipped**, e2e **7/7**, smoke **12 checks**, `fail_under = 88` —
  `wave-v21/V21-PLAN.md`). All five lanes + verifier PASSed (frozen-tree **2715/0/0/13**, coverage
  **92.19%**); settlement items closed by the parent. Details in §3f.
- **Wave V22 is IN FLIGHT** — base `4f6a834`. Eight seats: W1 wiring audit, W2 honesty audit, four
  writers (tools persistence · phone thread view · wiring fixes · proof/coverage), docs/completeness
  (L5), riding verifier (L6). Lane status by receipt in §3g; the wave is **not settled** until L6's
  gate and the parent's rebuild/live checks land.

## 1. THE PHONE — see `PHONELINK-MCP-TEST.md` (its own standalone file)
Status: Hermes-side MCP **works today** (10 tools, live-proven; archive export to
`Desktop\PhoneLink-Archive`). **The Polsia-side integration is LIVE (wave V16, live-proven against the
real store):** read-only mount (`PHONE_LINK_DB_DIR`; container target `/phone-link`), `E_PHONE_UNAVAILABLE`
refusal by name (503, both body and header), `GET /api/v1/phone/*` routes (codes = key-only +
audit-logged, values never logged), streaming export ZIP (messages.json + calls.csv + contacts.json),
orchestrator activity feed (per-kind dedupe ledger; convergent — tick7 = tick8 = 0), dashboard Phone
card, `/phone` page (+nav; 16 routes). **Send (V17):** `POST /api/v1/phone/send` — a policy-gated
CDP driver against a paired Google Messages web session; **off by default**, refusing by name
(`E_SANDBOX_BLOCKED` / `E_PHONE_SEND_UNPAIRED` / `E_PHONE_UNAVAILABLE` / `E_VALIDATION`) and writing
one audit row per attempt; the live send is owner-gated (see `USAGE.md` §5). **Codes panel
(V19):** the audited route now has an owner-facing consumer on `/phone` — click-to-fetch only, masked
to the last two digits, reveal/copy with immediate re-mask, no client-side persistence, one audit row
per fetch (count + window, values absent). **Send bridge (V19):** optional host companion bridge so
the Docker stack can send through the host's paired browser — token, loopback bind by default, OFF
unless `PHONE_SEND_BRIDGE_URL` is set (USAGE §5.2). **Agent tool (V20) — SETTLED ✅:** `phone_read` is a
read-only, firewalled registry tool returning the newest ≤10 threads plus (by `thread_id`) capped
messages, **codes never** (code-shaped bodies withheld behind a named marker), granted to
`orchestrator` + `customer_support` only; one real-store firewalled call is in
`wave-v20/l2-agent-phone-tool/RECEIPT.md` (`ok=true`, `store_status=ok`, `thread_count=10`). USAGE §5.3
has the contract.

## 2. WAVE V15 — CROSSED OFF (all verified at V15 close)
- **2R-01…2R-09 ✅** — rate-limit zones + named 429 on the three public routes · docs auto-hide when
  sandbox is off (+ root() honesty) · `access_log off` on `/ws/` · ZIP export temp-file streaming
  (`FileResponse` + cleanup-on-disconnect) · secret-store `icacls` tightening on Windows + named warning
  · non-ASCII `API_KEY` boot warning (codepoints, never the key) · planner `source_url` scheme
  validator (unverified claims dropped + counted) · CSP + `X-Frame-Options` + `nosniff` at server scope
  · `PricingSection` safe-href (import-only).
- **W3 3-01 / 3-02 / 3-03 ✅** — stale `agent_runs` closer (`E_RUN_STALE`, no fabricated cost) · durable
  once-per-(cycle, local-day) claim committed before spend (no migration; `activity_log` ledger) · pause
  truly pauses + re-arm exactly once (`rearm_paused_released_tasks` tick).
- **W4 4-01…4-07 ✅** — CS compliance bridge (`send_bridge`; AST-proven zero direct sends) · ads metrics
  reader + **attach** (both-platform; google-first when both configured) · Stripe revenue readers +
  **attach** into finance runs · social real threading (fabricated `mention:{index}` ids killed) ·
  memory family wired both ways (**resolve + remember_run**; all 10 agents via the base layer) ·
  owner-profile Settings intake (null semantics, never a fabricated 0) · site-package download control
  (false copy deleted). *(4-02/4-03/4-05 attaches were completed by E3 + the repair unit; 4-04's copy leg
  by the repair unit.)*
- **W5 5-01…5-06 ✅** — invoice email gate keys on the amount asked (reminders stay ungated) · task
  deep-links fetch-by-id (labelled misses) · dev-key boot warning preserved (**verify-only**) ·
  scheduled daily backups (compose sidecar + `scripts/backup.sh`) · strict config keys
  (`extra=forbid`, 422 names the key) · strict timestamps (`StrictInstant`/`StrictDate`).
- **P2-01…P2-03 ✅** — WebsiteCard `javascript:` sink killed (7 hostile cases pinned) · CommandPalette
  copy "fourteen-route IA" · DocumentsOverlay counter renders only a real server total.
- **CI fixes ✅** — ruff pinned 0.8.4 → **0.17.0** (local == CI) · `setup-node` 20 → **24** (Playwright
  `node:sqlite`) · recursion tests platform-dynamic (Linux 8 MB stack vs Windows) · integration suite
  fixed (`session loop_scope`; asyncpg different-loop root cause; exits 0 with real containers).

## 3. WAVE V16 — CROSSED OFF (all lanes + verifier PASS; details in `wave-v16/v-verify/VERIFY.md`)
- **Phone family (E5 backend + W3 UI, P-01…P-06) ✅.** Read-only store mount (`PHONE_LINK_DB_DIR`,
  `/phone-link:ro`), `phone_link_service` (refuses `E_PHONE_UNAVAILABLE` 503 by name),
  `GET /api/v1/phone/{summary,messages,calls,codes,export}` (H-P1), ≤5-min activity tick
  ("SMS received: …" / "Missed call: …"), dashboard Phone card, `/phone` page (+nav; MENU now 15
  routes), docs/doctrine. **Live drill on the real store: ALL PASS** (summary ok / messages /
  calls+missed / codes key-only / export ZIP / activity ticks convergent).
- **Capability-visibility family (E6 + W4) ✅.** Capability manifest **derived from the single
  registry sources** injected into each agent's assembled context (budget rebalanced, zero
  truncation for all 10 agents); orchestrator gets the cross-agent map;
  `GET /api/v1/ops/capabilities` (H-C1) behind a drift pin; Capabilities UI page (reached via
  Settings; honest empty/refusal states).
- **V15-delta sweep (E7, read-only) ✅.** Findings cards delivered; F-01 (backup.sh untracked) was
  already fixed by the parent CI rounds; F-02..F-06 carried in §4.
- **Readiness/docs (E8) ✅.** `USAGE.md` (operator guide, 16 features), README feature tour,
  `DEPENDENCIES.md` completeness pass (env↔settings zero gaps; all `make -n` dry-runs OK),
  `RELEASE-CHECKLIST.md`, `PHONELINK-MCP-TEST.md`, this file, `.env.example` phone row.
- **Verifier (V) ✅.** Lanes PASS by name; backend 2389/3F (exactly the parent pins) → after pins
  **2390/0F/0E/12S**; FE 653/653 ×2; tsc 0; next build 0; probes 22 phone / 7 activity /
  9 capabilities (drift simulation flips it → derived-not-copied proven).
- **Two live-caught fixes (only a real drill could find these):** the compose env wiring
  (`env_file` injected the host path into the container — now a fixed `PHONE_LINK_DB_DIR: /phone-link`
  override) and the activity-ledger trim (string order kept stragglers / starved `call`; now a
  per-kind numeric budget — tick7 = tick8 = **0 republished** on the real store).
- **Gate at V16 close:** backend 0F/0E, ruff + mypy clean, FE jest ×2, `tsc` 0, `next build` 0,
  and **CI (`ci.yml`, `integration.yml`, `docker-build`) fully green** at `a002100`.

## 3b. WAVE V17 — CLOSED (base `27a353e`; details in `wave-v17/`)
- **Restore drill — NEW, executed ✅.** `scripts/restore.sh` + `make restore-drill SRC=backups` prove a
  backup restores: one throwaway `postgres:16-alpine` container (`polsia-restore-drill-pg`, **no
  published ports**, never the compose project), dump loaded into a fresh `polsia` database, public
  table count + core-set row counts verified against a manifest parsed from the dump itself, every
  volume archive extracted with each listed member proven present, and trap teardown (`docker rm -f`)
  on every exit path. Refusals by name (exit 2) for missing/corrupt artifacts, docker absent, or a
  name collision. Pins: `backend/tests/unit/test_v17_restore_script.py` (13 tests).
- **Release checklist execution.** §1 (hygiene/secret scan), the locally-safe §2 item (ruff on the new
  pin) and §5 read-only probes (`docker compose config -q`, security headers, health) are executed in
  the V17 receipt with evidence; §4 CI and the owner-run §5 drills (sandbox flip, rate-limit burst,
  full-stack restore) stay with the parent/owner.
- **Docs truth pass.** Phone + capabilities are stated as **live (V16)** with the real-store drill
  citation; `make restore-drill` is documented in README/USAGE/DEPENDENCIES.

## 3c. WAVE V18 — CLOSED (base `257e20c`; details in `wave-v18/`)
- **One-command acceptance — NEW, executed ✅.** `scripts/smoke.sh` + `make smoke` (and
  `make smoke-dry`) are the release checklist as one GET/HEAD-only command: health, the three
  security headers, the key gate both ways (401 `E_AUTH`, then 200 envelope), the sorted
  capabilities manifest, phone summary + media (real store or a NAMED refusal — a raw 500 and a
  plain 404 are FAILs), `/docs` against `SANDBOX_MODE`, the proxy `Server` header, and the backup
  artifacts. One `PASS/FAIL <name>: <detail>` line per check; exit 0/1 (2 = usage refusal). The key
  is read from the environment only and never printed. Pins:
  `backend/tests/unit/test_v18_smoke_script.py` (14 at V18; **22 at V19** after the codes/bridge
  pins: static + executed dry run + curl-shim behaviour + a live run that skips with a reason).
  Live run 2026-10-18: 8 PASS / 1 named SKIP (docs at the nginx base) / 2 named FAIL
  (`phone-media` absent from the running image until the V18 media lane is built; `server-header`
  `nginx/1.31.6`) — **both FAILs are green on the V19 re-run, below**.
- **Coverage ratchet — verified coherent ✅ (gate NOT run here).** `fail_under = 87` in
  `backend/pyproject.toml` is the single truth (measured 87.5461%, 4,984 of 5,693, `core = "sysmon"`);
  the "75" that survives in comments/README is named only as the *original declared* target, now met.
  The gate command is `cd backend && python -m pytest tests/unit -q --cov=app` (what CI runs); the
  full measured run also spans `tests/parity tests/redteam`. The full run stays parent/verifier-owned.
- **HANDOFF — nginx discloses its version (CLOSED at the V19 base).** V18's live run saw
  `Server: nginx/1.31.6`; `nginx/nginx.conf` now carries `server_tokens off;` at `http{}` scope, and
  the V19 L5 live re-run reads `Server: nginx` (masked) — the check is green.
- **V18 live-run FAILs, re-measured at V19 (L5):** both are closed on the running stack —
  `phone-media` answers `200 parts=17 truncated=False thread=7` (the V18 media route is in the image)
  and `server-header` is masked. The remaining `docs` SKIP is the known nginx↔Next `/docs` routing;
  `--api-base` checks the API itself.

## 3d. WAVE V19 — SETTLED (all five lanes + verifier; CI green at `c2e1261`)

Lane L5 wrote the first half of this section while L1–L4 were still running; the in-flight markers are
now resolved below, and the three defects the verifier's frozen-tree gate found (parity rows for the
wired ads route · the t14 module-import time-bomb · the bridge's pre-drain Windows RST) were fixed
post-audit and re-verified (see `wave-v19/v-verify/VERIFY.md` and `v-final/SETTLE-NOTES.md`).

- **One-command acceptance extended — SETTLED ✅ (L5).** `scripts/smoke.sh` now runs **12 checks**.
  The two new ones are `phone-codes` (`GET /api/v1/phone/codes?limit=1`; `200 data.codes[N]` with a
  **count only** — values are never printed, and a failure withholds the body because it may contain
  them; named 503/401 refusals are green, a raw 500 is a FAIL) and `send-bridge` (when
  `PHONE_SEND_BRIDGE_URL` is exported, `GET <url>/health` must answer `200 {status:"ok"}`; unset is
  a **named SKIP** naming the variable and the default-OFF posture; a scheme-less URL is a FAIL
  without a call). `--dry-run` still makes **zero** requests — a PATH `curl` shim records every call
  and stays empty. Exit contract unchanged (0/1/2).
- **Live run — SETTLED ✅ (L5, 2026-10-18, GET/HEAD only).** **10 PASS / 2 named SKIP / 0 FAIL**,
  exit 0: `phone-codes` → `200 codes=1 shape=True (values never printed)`; `send-bridge` → named
  SKIP (URL unset); `phone-media` → `parts=17`; `server-header` → `Server: nginx` masked. The two
  V18 FAILs are closed. The `docs` SKIP is the nginx↔Next routing (pass `--api-base` to check it).
  Raw: `wave-v19/l5-smoke-docs/raw/live-smoke.txt` / `raw/dry-run.txt`.
- **Docs truth — SETTLED ✅ (L5).** `USAGE.md` §5.1 (codes panel: click-to-fetch, mask last two
  digits, reveal/copy re-mask, no client-side persistence, one audit row per fetch) and §5.2 (bridge:
  the three-command host flow, token rules, the LAN-exposure caveat); `DEPENDENCIES.md` bridge row +
  smoke/troubleshooting; `README.md` phone row + acceptance paragraph; `RELEASE-CHECKLIST.md` two new
  parent live-checks; `Makefile` smoke comment. Coverage statement re-checked against
  `backend/pyproject.toml` (`fail_under = 87`) — no doc claims a different floor.
- **Codes panel — SETTLED ✅ (L1).** `PhoneCodesPanel` on `/phone` (fetch-on-click only, mask-last-2,
  reveal/copy re-masks, null media `when` → "time unknown"); 25 new FE pins; the verifier's independent
  panel probe 7/7 (idle = zero fetches, one click = one audited GET, zero storage calls).
- **API solidity sweep — SETTLED ✅ (L2).** Four fixes (uncoded `PUT /config` 500; invoice JSON NaN
  write/read laundering; `UtcDatetime` across 25 schema fields + `iso_utc()` for exports/CSV; the ads
  `list_metrics` DEBT **wired** as `GET /api/v1/ads/campaigns/{id}/metrics` — parity rows added by the
  parent); 68/68 refusal-uniformity probes, totals honesty and the fuzz matrix clean; 18 new pins.
  Carried cards are listed in §4.
- **Send bridge — SETTLED ✅ (L3).** `scripts/phone_send_bridge.py` (loopback default, env-only token,
  constant-time compare, named refusals) + `phone_send_service` transport selection (243/0 diff — the
  direct path is untouched); 81/81 focused pins; live refusal matrix 9/9 with zero resets (the
  pre-drain RST the verifier caught was fixed and re-proven); the owner 3-command flow is in USAGE §5.2.
- **FE↔API contract — SETTLED ✅ (L4).** All 29 FE GETs captured live and fed to the shipped parsers:
  29 ACCEPTED / 0 MISMATCH (fixtures banked under `__tests__/fixtures/v19/`); three debts closed
  (invoices Send/Void disabled-with-reason, feed refusal codes, raw anchors → `next/link`); 53/53 pins.
- **Coverage gate — NOT RUN here (by design).** `fail_under = 87` stays the single truth (measured
  87.5461% at the base: 4,984 of 5,693). This lane's new tests only add coverage; the verifier/parent
  runs `cd backend && pytest tests/unit -q --cov=app` at settle, and the floor only moves up.

## 3e. WAVE V20 — SETTLED (all five lanes + verifier; base `8fa2b38`; receipts under `wave-v20/`)

L5 wrote this block from `wave-v20/V20-PLAN.md` plus the lane receipts on disk; at that moment L1's
receipt still read “IN PROGRESS”. **Settled afterwards: all five lanes landed with complete receipts**
and the verifier PASSed every one. Its frozen-tree gate caught the last V12 pin family (a fifth fileset
my lane brief had missed) — the parent re-pinned all five deliberately, post-audit, with attribution
comments (`88/0/0/0` and `101/0/0/0` across focused runs).

- **API typing — the five V19 cards (L1) — SETTLED ✅.** `wave-v20/l1-api-typing/RECEIPT.md` lists four
  work rows: L2-08 unknown `status=` → the same named `E_VALIDATION` 422 as tasks/invoices/support (and
  the identical hole in `ads` + `social` + the export route) · L2-09 the two defensive `RuntimeError`s
  (`dashboard.py:94`, `chat_service.py:456`) → named `E_DB`, plus the `/dashboard/reports` fabricated
  `total: 0` on an absent count row · L2-06 the **9** live `/api/v1` dict routes (the tenth is `main.py`
  `/`, outside `/api/v1`) → real pydantic models publishing `$ref` schemas, with a schema-lint pin
  enforcing `response_model` or a named 12-entry exemption set · L2-07/L2-11 → honest totals: `pricing`
  gains a true `total` beside its page-length `count`, and the 7 `limit`-only lists expose the full
  filtered count on `X-Polsia-Total` with their bare-array bodies untouched. **Proof:** RED 45
  failures at the base → **81/81** new pins green; the verifier checked 25/25 file hashes and re-ran
  the before/after wire probe: **20/26 responses byte-identical**, the 6 diffs named (2 intended
  `pricing.total`, 3 intended refusals, 1 from the concurrent `phone_read`). **Wire compatibility**
  held — no client-visible shape changed except the two documented additions. Handoff noted: `main.py`
  `expose_headers` should gain `X-Polsia-Total` for cross-origin readability (no browser consumer
  today).
- **`phone_read` agent tool (L2) — SETTLED ✅** (`wave-v20/l2-agent-phone-tool/RECEIPT.md`, complete).
  Fourth registry tool (`TOOL_SPECS` + `TOOL_GUIDANCE` + `_RUNNERS`), reusing
  `phone_link_service.summary()`/`messages()` read-only. Arguments `{"thread_id"?: int, "limit"?: int}`
  — default **5**, hard cap **10** (above it clamps; below 1 / non-integer / bool is `E_VALIDATION`).
  Result `{ok:true, tool:"phone_read", result:{store_status, threads[{thread_id, who, last_when,
  unread}], messages[{when, dir, who, text}], truncated, note}}`: threads always (newest ≤10, with an
  honest `truncated`/`note` when cut), messages only when `thread_id` is given. **Codes never:** no
  `code`/`codes` key at any depth, `phone_link_service.codes()` never called (trip-wired), and a
  code-shaped message body is replaced by the named marker `[message body withheld: it looks like a
  verification code — codes are not available to tools]`. **Allowlist: `orchestrator` +
  `customer_support` only** — the other eight agents get `E_POLICY_DENIED` / `POLSIA-E114` (pinned for
  all eight); an absent or corrupt store returns `E_PHONE_UNAVAILABLE`, never an empty list. Proof:
  RED **29/29 failed** → GREEN **68/68** on the lane's four files, neighbours **80/0**, ruff/mypy
  clean, plus **one real-store firewalled call** (`probes/live_phone_read_probe.py`: `ok=true`,
  `store_status=ok`, `thread_count=10`, `unread_total=10`, `truncated=true`). Deferred, stated: four
  V12-era pin files outside L2's territory fail by design (**9 failed / 95**) until the parent applies
  `REQUESTS-v12-pins.md`; the live `GET /ops/capabilities` shows the new grants only **after the settle
  rebuild** (the running image is the base build).
- **e2e depth + coverage ratchet (L3) — AUTHORED / MEASURED (receipt complete; live runs blocked on the
  stale build).** `e2e/v20-codes-panel.spec.ts` (5 tests) pins the V19 codes panel end to end — idle zero
  requests, one click = one `GET …/codes?limit=10&within_hours=24`, masked rows, Reveal/Copy/ESC, the
  window selector's query, a named 422 refusal inline, and the 503 store gate (panel never mounts). A
  second UI-contract test in `e2e/invoices.spec.ts` pins terminal (paid/void) rows keeping Send + Void
  **disabled** with the reason as visible text. Both files **enumerate (7 tests / 2 files, `--list` exit
  0)** but have **never run green**: the served FE predates the V19 panel (codes run **4 failed / 1
  passed**; invoices **1 failed**; the served chunk has `phone-codes: 0`) — the live run is a parent
  check after the FE rebuild. **Coverage: L3 measured 86.66% (11,631 statements, 1,551 missed) mid-wave
  → the ratchet was NOT earned; `fail_under = 87` stays and `backend/pyproject.toml` is byte-identical
  (sha256 `98777917…486ffe`).** The parent re-measures on the frozen tree; below 87.0 = add targeted
  tests, never lower the floor. *(Settled afterwards: the frozen-tree run measured **92.07%** and the
  floor moved **87 → 88** — §0's V21 base line and `backend/pyproject.toml:58`.)*
- **Agents-page toolbox (L4) — SETTLED ✅** (`wave-v20/l4-agents-toolbox/RECEIPT.md`, complete). Every
  agent card gains a `TOOLS · N` button (collapsed by default — no tool markup in the DOM) that expands
  to one row per tool: name in the page's mono label style + the registry's own one-line description.
  One capabilities read per roster mount, never one per row; the **shared** parser is reused
  (`useAgentToolbox` imports `capabilitiesFrom`; `toolbox.ts` only indexes the parsed map — no second
  parser). Honest states, each distinct: `tools: []` → “No tools are granted to this agent.”; an absent
  or unreadable row → “The capability map has no readable entry for this agent.” (+ dropped-row count);
  a failed read → `TOOLS — UNAVAILABLE`, a `role="alert"` block with the shared `ApiError.display`
  sentence and a working Retry — **a failure is never rendered as “no tools”**. Keyboard: real
  `<button>` with `aria-expanded`, `aria-controls` while open, Escape collapses and returns focus.
  Proof quoted from the receipt: RED **8/8 failed** before product code → GREEN **10/10** pins,
  neighbour suites **98/98** across 6 suites, `tsc --noEmit` exit 0 (0 bytes), safety grep empty. Live
  visual remains a parent check: the served frontend predates the V19 panel (container BUILD_ID built
  Oct 18 18:09), so the V19/V20 UI is visible only after the parent's rebuild at settle.
- **Docs settle (L5) — DONE at this write; L1's status flipped by the parent at settle.** The stale E7
  bullet in §4 replaced with the V17-L1 closed statement (quoted from `wave-v17/l1-fixes-e7/RECEIPT.md`);
  the five V19 API cards restated by receipt state; the V19 §3d blocks and V18 §3c left intact;
  `USAGE.md` gains the "agents can read the phone" subsection (§5.3), `README.md` the phone/agents rows +
  acceptance paragraph, `RELEASE-CHECKLIST.md` the V20 parent live-checks. `DEPENDENCIES.md` unchanged —
  V20 adds no new dependency (L2/L4 are in-app; L3 is test tooling already pinned).

## 3f. WAVE V21 — SETTLED (base `e618342`; receipts under `wave-v21/`)

L5 wrote this block while the wave was running and **re-read it at 08:39 CDT, after L3's receipt
update** (all four writer receipts existed). Status is quoted from those receipts plus L5's own work.
**Settled afterwards:** the verifier gate (L6) PASSed (frozen tree 2715/0/0/13, coverage 92.19%) and
the parent closed the settlement items named at the end of this section — the block below is kept as
the record of what each lane proved.

- **Gate hardening (L1) — RECEIPT PRESENT, DONE ✅** (`wave-v21/l1-gate-hardening/RECEIPT.md`, 4/4 items
  + tests). mypy baseline **4 errors / 2 files** (`phone_send_service.py` ×3, `runs.py` ×1 — the last
  closed by L2 in this wave) → **0 errors / 148 files**; the `|| true` is deleted from `ci.yml` and
  `test_the_mypy_ci_step_is_a_hard_gate` reads the step; **zero** `# type: ignore`, zero overrides,
  `pyproject.toml` untouched. `expose_headers` now carries `X-Polsia-Total` (wire capture in
  `raw/cors-header.txt`). Two exact nginx locations — `location = /apidocs` → backend `/docs` with a
  jsDelivr-scoped CSP plus the two other security headers, `location = /openapi.json` → backend — and
  the backend answers 200/200 with 64 paths (`raw/docs-assets.txt`). Debt item: **no open marker
  existed**; all six sites adjudicated. Tests: 11 new pins (RED 6 → GREEN 11), focused set **152
  passed**. The live nginx reload/curl is the parent's (exact commands in its receipt). Full
  `pytest tests/unit` **aborted at 50% by a 420 s terminal clamp, not by a failure** — the receipt
  says so plainly; the settle run owns the full tally.
- **Run transparency (L2) — RECEIPT PRESENT, DONE ✅** (`wave-v21/l2-run-transparency/RECEIPT.md`).
  Its finding is the important half: **`tools_used` is produced but never persisted** — the runtime
  envelope carries it (`deepseek_runtime.py:1041,1119,1225`), but `usage_from_envelope`, `RunUsage`,
  `RUN_META_KEYS`, `_apply_run_usage` and the `agent_runs` columns all drop it (provenance probe:
  every file:line in §0). The frozen interface's honest branch was taken: a NEW `GET
  /api/v1/runs/{id}` serves the frozen list-row fields **plus `num_turns` / `is_error`** (`null` =
  not recorded, never 0/false; malformed at-rest JSON → null, row still serves; missing id → named
  404 `E_NOT_FOUND`). The NEW FE route `/runs/[id]` (`RunDetail.tsx`) renders **“Tool activity: not
  recorded for this run.”**, the two stored counters, and **zero** chips — an off-contract
  `tools_used` in a response is ignored, never rendered. Proof: backend RED 7 failed → GREEN 33
  passed (9 new + 24 U5), broader surface 240 passed; FE 9/9, `tsc` exit 0 (0 bytes); mypy clean on
  both touched files. **Note:** the detail page has no link from the `/usage` ledger yet —
  `src/app/usage/page.tsx` is outside L2's territory; navigate by URL until the parent wires it.
- **Proof depth 2 (L3) — RECEIPT PRESENT, DONE ✅ except the 404 live run** (`wave-v21/l3-proof-depth2/RECEIPT.md`).
  New `e2e/v21-agent-toolbox.spec.ts` (5 tests) is **GREEN live — 5/5 passed** (22.4 s, then 14.4 s
  after one assertion-hardening edit: `main`→`body` so the "no No-tools text" check cannot go
  vacuous), with a RED probe (2 mutated expectations → 2 failed) proving the assertions bite; it pins
  one capabilities read per roster, collapsed-by-default `TOOLS · N`, panel contents and the
  registry's own descriptions, Escape focus return, the two distinct honest empty states, and the
  `UNAVAILABLE` + Retry path. New `e2e/v21-not-found.spec.ts` is **AUTHORED; RED on the stale served
  FE** (source `not-found.tsx` + L4's receipt exist; the running bundle still serves Next's default
  404 — re-checked at 08:35) — parent redeploy, then the green run (1 test; both files enumerate 6/2).
  **Coverage ratchet EARNED:** one CI-exact run measured **92.08%** (`11,678` statements, `925`
  missed, exit 0) and raised `fail_under` **88 → 90** (`backend/pyproject.toml:58`; pre-image
  `pre/pyproject.toml.v20`).
- **UI completeness (L4) — RECEIPT PRESENT, DONE ✅** (`wave-v21/l4-ui-completeness/RECEIPT.md`, 3/3).
  `frontend/src/app/not-found.tsx` (server segment, testids `not-found` / `not-found-home`, h1 "This
  page does not exist", Common-surfaces nav) with 5/5 pins and a production build that prerenders
  `/_not-found` **inside the shell** (20/20 pages, exit 0). The a11y contract of the V20 toolbox is
  now pinned (4/4, product bytes identical). Item 2 resolved the plan's TaskBoard "marker" honestly:
  **no TODO/DEBT marker ever existed** — the plan's grep matched the identifier `TODO_STATUSES`, now
  renamed `OPEN_STATUSES` with a contract comment (53/53 before and after, behaviour unchanged).
- **Docs + smoke (L5) — DONE at this write** (this lane). `scripts/smoke.sh` is **13 checks**: the
  new `api-docs` check requires `GET /apidocs` → 200 with a Swagger-UI marker **and**
  `GET /openapi.json` → 200 with an `openapi` key (through the proxy, or at `--api-base`). Its only
  SKIPs are `SANDBOX_MODE=false` (docs auto-hidden by design) and a 404 pair when the repo's own
  `nginx/*.conf` has no `/apidocs` location (a genuine pre-V21 build); once the config has the route,
  404 is a **FAIL** naming the reload/rebuild. Proven live in this lane: the same script printed
  `SKIP` at **08:13:29** (no route in the config) and `FAIL api-docs: docs=404 … schema=404` at
  **08:14:10** (L1's route written at 08:14:08, nginx not yet reloaded), with the original 12 checks
  unchanged and the dry run still making zero requests. Four shim branches (PASS / no-marker FAIL /
  shipped-route 404 FAIL / pre-V21 SKIP) are banked in `raw/api-docs-branches.txt`. Docs: README
  (feature-tour API-docs row, 13-check acceptance, mypy hard-gate CI row), USAGE §7.2 + §7.1 13-check
  list, DEPENDENCIES §6/§8 + two api-docs troubleshooting rows, RELEASE-CHECKLIST (V21 live-checks),
  this file (§3f + the §4a ledger). Coverage statements now match `backend/pyproject.toml:58`
  (`fail_under = 90`, measured 92.08% by L3) — README/USAGE/RELEASE-CHECKLIST still said 87 when this
  lane opened, and were re-anchored twice as V20's 88 became V21's 90.
- **Verifier (L6) — SETTLED ✅** (receipt: `wave-v21/v-verify/VERIFY.md`). All five lanes PASS; frozen-tree
  gate **2715 tests / 0 failed / 0 errors / 13 skipped**, coverage **92.19%** run by the verifier itself;
  `mypy app` exit 0 (148 files) re-run by the verifier; ruff/jest/tsc clean. No BLOCKER/MAJOR findings.

**Settlement items — closed by the parent after the verifier's report:**
1. **Parity rows for L2's `/api/v1/runs/{run_id}`** — added by L2 in its own territory (both parity files,
   declared in its receipt); the verifier saw them green at freeze.
2. **Smoke pins now name the 13th check** — `CHECK_NAMES` gained `api-docs` (parent, at settle); the
   shim branches were already banked by L5.
3. **Served stack was pre-wave** — rebuilt at settle: backend + frontend images, nginx reloaded; `/apidocs`
   200 (Swagger UI), `/openapi.json` 200 (63 live paths), 404 + run detail live-verified.
4. **Territory exceptions confirmed by the parent**: `services/phone_send_service.py` (L1 — reassigned by
   the parent mid-wave), the two parity pins (L2 — the tests mandate the update), the v15 nginx
   `add_header` count pin 3→6 (L1 — the docs CSP needs its own headers; exact count kept).

## 3g. WAVE V22 — IN FLIGHT (base `4f6a834`; receipts under `wave-v22/`)

L5 wrote this block at 09:55 CDT and updated it at **10:42 CDT, 2026-10-19**, after reading the
receipts and matrices that now exist. Status is stated **by receipt existence**. **L6's frozen-tree
gate has PASSED**; the wave remains **unsettled** until the parent's rebuild/live checks and the
commit/CI are done.

- **Wiring audit (W1) — DONE ✅** (`wave-v22/w1-wiring/WIRING-MATRIX.md`, 50 KB, final at 10:04).
  77 backend surfaces enumerated twice (AST + live `/openapi.json`, **0 missing / 0 extra**; 76 HTTP +
  1 WebSocket). Counts: **74 WIRED · 3 API-ONLY-BY-DESIGN · 0 UNWIRED** in the working tree
  (**73 / 3 / 1** at base `4f6a834`); **0 GAP-DEADREAD**; 4/4 registry tools wired; Celery 15 wired +
  2 API-only; ops/dev 1 wired + 4 API-only; **0 frontend calls point at a route the API does not
  serve**. Open findings (W1 F3 numbering): GAP-1 `X-Polsia-Total` (4/5 closed by L3;
  `/dashboard/activity` remains), GAP-2 invoice items (closed by L3, both display and entry),
  GAP-3 the closures are uncommitted, GAP-4 three unimported components, GAP-5 worker counters
  invisible (a feature, not a wire).
- **Honesty audit (W2) — DONE ✅** (`wave-v22/w2-honesty/MOCK-AUDIT.md`, 46 KB, STATUS: COMPLETE).
  **96 hits in 61 unique files** (L6's re-count of the raw artifact; W2's prose said 70); **95/96
  individually triaged** (the exception is a comment line at `deepseek_runtime.py:201`), with **zero
  product-path test doubles** (the only mock switch is the env-gated, CI-only `CLAUDE_CLI_MOCK`,
  which labels its own output). **9 defects (2 HIGH / 3 MEDIUM / 4 LOW)**: D3–D6 were closed by L3 in
  this wave; **D1, D2 (HIGH) and D7–D9 (LOW) remain** — all backend/seed, none owned by a V22 writer.
  Three findings were fixed mid-audit by V22 writers (`/tasks` total, `/ads` total, run-detail
  `tools_used`) and are recorded, not queued. §5 of the audit names three capabilities with no
  owner-facing control (cycle-report email, KPI editor, human hourly rate).
- **Tools persistence (L1) — DONE ✅** (`wave-v22/l1-tools-persist/RECEIPT.md`, 10:06). Additive
  `agent_runs.tools_used` (migration `0010`, proven upgrade/downgrade on SQLite + offline PG SQL);
  the DeepSeek envelope's list persists through `accounting.usage_from_envelope` →
  `agent_tasks._apply_run_usage` (cap 50, named truncation marker); `GET /runs/{id}` serves
  `string[] | null`; `/runs/[id]` renders the three states (chips / "no tools were called" /
  "not recorded"). Proof: RED → GREEN **129** focused backend tests (11 new), FE **15/15**, `tsc`
  0 bytes, mypy **162 files** clean. Residual: `tests/parity/*.py` header comments still narrate the
  old V21 finding (comments only).
- **Phone thread view (L2) — DONE ✅** (`wave-v22/l2-phone-thread/RECEIPT.md`, 09:53). New
  `PhoneThreadView.tsx` behind the existing `GET /phone/messages` (fetch-on-select only, four honest
  states, no storage/console); `parseMessages` now accepts the store's legal `when:null` — the real
  contract gap. Proof: RED **12 failed / 2 passed** → GREEN **14/14**; regression **7 suites / 122
  tests**; scoped `tsc` 0 bytes; one read-only live GET confirmed the payload shape. The served build
  predates it — parent rebuild needed.
- **Wiring fixes (L3) — DONE ✅** (`wave-v22/l3-wiring-fix/RECEIPT.md`, final 10:16). **13 rows
  closed**: ads-metrics expander (the one UNWIRED route), invoice line-item display **and** entry,
  `X-Polsia-Total` for invoices/outreach/support/tasks/ads, memory wording, and W2's D3–D6. Proof:
  **15 suites / 209 tests** + **10 suites / 97 tests** green; `tsc` 0 bytes. Remaining in territory
  (4): the commit itself, `/dashboard/activity` header, the dead-components retire decision, worker
  counters. Out of territory (5): W2 D1, D2, D7, D8, D9 — backend.
- **Proof + coverage (L4) — DONE ✅** (`wave-v22/l4-proof/RECEIPT.md`, 10:21). `e2e/v22-run-chips.spec.ts`
  (5 tests) + `e2e/v22-phone-thread.spec.ts` (5 tests) authored against L1/L2's frozen testids; both
  enumerate (**10 tests / 2 files**), `tsc` scoped 0 bytes. **RED on the served FE** (3 failed / 2
  vacuous passes; 1 failed on the missing `phone-thread-open`) — the assertions bite, and the stubbed
  envelopes are wire-valid. **Coverage 92.10%** (11,711 statements / 925 missed) is below the 92.3 bar,
  so **`fail_under` stays 90** and `backend/pyproject.toml` is byte-identical. The green live run is
  the parent's after the rebuild; the three pre-existing alembic head pins it hit were re-pinned to
  `0010` by the parent (12/12 green).
- **Docs + completeness (L5) — DONE at this write.** `COMPLETENESS.md` created at the repo root —
  §3 (W1 counts + reconciled gaps), §4 (W2 verdict + 9 defects with owners), §5 (last settled gate
  and V22 lane proofs), §6 (owner-gated), §7 (every remaining item with `file:line` + owner).
  `NEXTSTEPS` §3g + §4a refreshed; README/USAGE/RELEASE-CHECKLIST updated only for surfaces whose
  receipts exist.
- **Verifier (L6) — DONE ✅, gate PASSED on the frozen tree** (`wave-v22/v-verify/VERIFY.md`).
  Backend **2,726 tests / 0 failed / 0 errors / 13 skipped** (681 s, junit); scoped jest **23 suites /
  318 tests**; `tsc` **0 bytes**; ruff clean; coverage **92.10% ≥ 90** (no ratchet). Verdicts: L1
  PASS-WITH-NOTES, L2 PASS, L3 PASS-WITH-NOTES, L4 PASS, L5 PASS (after three claim corrections),
  W1 PASS (77/77 rows + 21-row sample), W2 PASS-WITH-NOTES. Unsettled: the wave is uncommitted, the
  served FE predates it (e2e specs RED by design), the W2 audit needs PII redaction, CI has not run.

**Settlement owned by the parent after L6:** rebuild backend + frontend images, reload nginx, run the
e2e sweep and `make smoke` on the rebuilt stack, then commit/push/CI. **The commit is not optional
bookkeeping:** every lane's files are uncommitted — L3's 17 modified/new product files + 6 new FE test
files (its receipt lists them), plus L1's column/migration/API/service/panel files and L2's
`PhoneThreadView.tsx` + test — and without them the ads-metrics route and the totals fixes re-open.
Until that happens the V22 tree is **unsettled** no matter how many lane receipts exist.

## 4. Open offers / owner decisions (nothing built unasked)

### 4a. Debt-marker ledger — the V22 tree

Measured with the brief's own command
`grep -rn -E "TODO|FIXME|XXX|HACK|DEBT" backend/app frontend/src` at **2026-10-19 10:22 CDT**
(raw output banked at `wave-v22/l5-docs-completeness/raw/debt-grep.txt`; the working tree, including
every V22 lane's uncommitted edit). **No open, actionable marker remains in product source.** The
remaining hits, each named and judged:

| # | file:line | text | verdict |
|---|---|---|---|
| 1 | `backend/app/agents/integrations.py:41` | `urn:li:person:XXXX` in `_LINKEDIN_HINT` | **not a marker** — a documented example of the LinkedIn author-URN format; `XXXX` matched the `XXX` pattern |
| 2 | `backend/app/agents/__pycache__/integrations.cpython-312.pyc` | binary match | **build artifact** — the compiled copy of hit 1; excluded by a `--include=*.py` source grep |
| 3 | `frontend/src/__tests__/v19-fe-contract.test.tsx:13` | `* DEBT HALF (RED-first, each pinned on behaviour):` | **historical test docstring** — labels the V19 debt items this test pins as closed; no open work |

The V21 plan named debt markers in three backend files plus `TaskBoard.tsx`
(`wave-v21/V21-PLAN.md` item map). On the tree measured above:

- **`backend/app/api/v1/ads.py`** — no `TODO`/`DEBT` token remains. The plan's marker was the
  `V19/L2 (closes V17/L3-08 + V18 DEBT)` docstring note in `ads_campaign_metrics`; L1 owns the file
  and the current docstring carries the closure without the ledger word.
- **`backend/app/schemas/social.py`** — same: the `V17/L3-08/V18 DEBT` note in `AdMetricResponse` is
  gone from the current tree.
- **`backend/app/agents/integrations.py`** — no `TODO`/`FIXME`/`DEBT` marker exists at this write;
  the only hit is the `XXXX` placeholder in row 1. The plan's "marker" here was not a ledger token on
  this tree, so nothing is claimed closed in this file.
- **`frontend/src/components/tasks/TaskBoard.tsx`** — no `TODO`/`FIXME`/`XXX`/`HACK`/`DEBT` token
  remains. L4's receipt settles the plan's item: **no marker ever existed** — the plan's boundary-less
  grep matched the product identifier `TODO_STATUSES`, which L4 renamed `OPEN_STATUSES` with a
  contract comment (behaviour identical, 53/53 before and after). The `todo` bucket key remains as
  product vocabulary.

Re-run the command on the frozen tree at settle: this ledger is only true of the tree it measured.

- **V19 settlement — CLOSED ✅.** All five lanes + verifier PASS; CI green at `c2e1261`; the three
  post-audit fixes (ads-route parity rows · t14 import-time stamp · bridge pre-drain RST) are in the
  commit. One parent follow-up landed with them: the phone export's stamp/filename are now UTC
  (`phone.py`, previously naive local — L2's handoff).
- **L2 carried cards (V19 API-solidity findings) — CLOSED ✅ in V20 lane L1 (see §3e).** The ten
  routes whose bodies were untyped dicts (the 9 inside `/api/v1`; the tenth is `main.py` `/`) ·
  limit-without-total lists · unknown `status=` answering `200 []` on runs/outreach ·
  the two defensive `RuntimeError`s · `pricing.count` being page length — all five are fixed, pinned
  and verifier-checked (81/81 new pins; 25/25 hashes). Details: `wave-v19/l2-api-solidity/FINDINGS.md`
  · V20 evidence: `wave-v20/l1-api-typing/RECEIPT.md`.
- **E7 carry-forward findings — CLOSED ✅ (fixed in V17 lane L1).** The four findings were ordered,
  fixed and pinned in V17 — `wave-v17/l1-fixes-e7/RECEIPT.md` records every one as **“CLOSED (fixed +
  pinned)”**: RED 4 failed / 8 against the pre-fix bytes → 8/8 green → **10/10** with the hostile pins
  (`backend/tests/unit/test_v17_e7_fixes.py`), regression legs **58 passed** and **35 passed**, ruff
  `All checks passed!`, mypy `Success: no issues found in 4 source files`. Per finding, in the receipt's
  words: **F-03** “The stranded-`pending` net now also covers rows carrying the re-arm's own
  `rearmed_at` evidence, any `source`, once per window.” · **F-04** “Row locks are taken BEFORE the
  payload is read; a concurrent writer's disjoint key now survives (measured lost before).” · **F-05**
  “A pause release refunds the claim's increment (`release_kind="paused"`); plain releases keep the
  shipped semantics.” · **F-06** “`today_utc()` from `app.core.timeutil` (existing helper) instead of
  `date.today()`.” The fixes are in the tree (`maintenance.py`, `task_service.py`, `integrations.py`).
  Residuals stated by that receipt, not hidden: the F-03 net re-feeds a pause-released row scheduled
  for later and the worker refuses it **by name** (`E_TASK_NOT_RUNNABLE`, nothing runs) until it is
  due; PostgreSQL's row-lock path and a real broker accept-then-lose stay parent live-checks (receipt
  “PARENT LIVE-CHECK” 1–3). Original finding cards: `wave-v16/e7-delta-sweep/FINDINGS.md`.
- **Phone send bridge (V19, owner decision):** default **OFF** — with `PHONE_SEND_BRIDGE_URL` unset
  the direct subprocess path is unchanged. Enabling Docker-stack sends means starting
  `scripts/phone_send_bridge.py` with a ≥24-char token from the environment and binding an interface
  the container can reach (`--host 0.0.0.0` or a specific address) — that bind is a deliberate
  LAN-exposure decision, so the token **and** a host firewall rule are then mandatory (USAGE §5.2).
  The real-send proof still needs the owner's paired browser (parent live-check); never put the token
  on a command line.
- **Production Stripe**: the burned live key was expired (correctly); a NEW restricted live key is needed
  when real payments go live (test key + webhook secret are wired in `.env`, never in git).
- **website_url**: still the placeholder in config (`https://yourcompany.com`) — set a real domain in
  Settings → Company when one exists (unsubscribe links depend on it).
- **API_KEY rotation** (F-02 residual): the loopback bind (`127.0.0.1:80`) removes the measured network
  exposure, so rotation is deliberately not executed; do it BEFORE ever publishing the stack beyond
  localhost (set `API_KEY` + `NEXT_PUBLIC_API_KEY` and rebuild the frontend image — they must match).
- **`GOOGLE_ADS_CLIENT_SECRET`** stays RESERVED with no reader (D14 decision) until a Google Ads API
  client is shipped; `CELERY_WORKER_CONCURRENCY` stays compose-owned (`--concurrency=4`).

## 5. Where everything lives
- App: `backend/` (FastAPI + Celery), `frontend/` (Next.js), `nginx/`, `docker-compose.yml`, `Makefile`.
- Docs: `README.md` (tour), `USAGE.md` (operator guide), `DEPENDENCIES.md` (setup), `docs/SUPERVISION.md`
  (tiers), `docs/ACCEPTANCE_TESTING.md`, `RELEASE-CHECKLIST.md`, `PHONELINK-MCP-TEST.md` (this repo's root).
- Campaign records: `Desktop\polsia-work\build\ui-reverse\` (per-wave plans, receipts, verify reports —
  NOT in this repo by design; they carry machine-local paths).
- Hermes-side tools: phone MCP (see `PHONELINK-MCP-TEST.md`), wave-ops skills, CDP drivers
  (`wave-v10/`, gitignored — contains a captured Stripe test webhook secret).
