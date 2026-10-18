# NEXTSTEPS — everything remaining, in full detail (maintained through wave V17)
Repo: https://github.com/RodneyDanger1/MYPOLSIA (public). This document is the living map of what is
DONE, what is IN FLIGHT, and exactly what comes NEXT — with where/when/why for each item.

## 0. Where things stand (as of the V16 close; V17 in flight — see §3b)
- **Landed & committed (pushed):** V1–V16 — the full Polsia core: FastAPI + Celery backend, Next.js UI,
  agent orchestration, firewalled tool-calling (DeepSeek-first runtime), compliance-gated outreach,
  Stripe billing/webhooks, finance & planning, memory layer, activity feed, ops console, hardened nginx,
  scheduled backups, phone-in-Polsia (live-proven), capability maps, operator docs. V15 base `3972097`;
  CI-fix rounds `8e9c148` → `a002100` (all three GitHub workflows fully green at `a002100`).
- **V15 close evidence (in history):** repair verifier — backend **2301 passed / 0 failed / 0 errors
  / 12 skipped** (`junit`), FE **627/627 ×2**, `tsc` 0, `next build` 0; parent close-out gate (backend
  + integration + ruff + mypy + jest + tsc + build). Every V15 item PASS or repaired (see §2).
- **Wave V16 ✅ CLOSED — all 6 lanes + riding verifier PASS** (see §3). Two real bugs were found ONLY by
  the live drill against the real Phone Link store — the compose env wiring and the activity-ledger
  trim — both fixed, regression-pinned, and re-proven live (tick7 = tick8 = 0 republished).

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
one audit row per attempt; the live send is owner-gated (see `USAGE.md` §5 Send).

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

## 3b. WAVE V17 — IN FLIGHT (this section is updated by the release-ops lane)
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

## 4. Open offers / owner decisions (nothing built unasked)
- **E7 carry-forward findings (V15.5 receipts; not yet ordered):** re-arm clears `paused_release` on
  broker acceptance → an accept-then-lost message can strand a `pending` non-scheduler row (only
  `source='scheduler'` rows have the 30-min reconciler net) · `_clear_paused_markers` unlocked
  read-modify-write on `metadata_json` · pause→release→re-arm burns `tasks.attempts` (a later lease
  expiry can terminal-fail a row that mostly sat queued) · `integrations._day_window` documented UTC
  but uses `date.today()` (process-local). Details: `wave-v16/e7-delta-sweep/FINDINGS.md`.
- **Phone SENDING** (no local send API): optional path = pair Google Messages web to this PC, then drive
  it via the user's own browser (CDP). **Scheduled in V17** — `POST /api/v1/phone/send` (CDP driver,
  sandbox-blocked by default, refusals by name); the real-send proof needs the owner's paired browser
  (parent live-check). Out of scope in V16.
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
