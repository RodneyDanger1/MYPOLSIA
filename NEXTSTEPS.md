# NEXTSTEPS — everything remaining, in full detail (maintained through wave V19)
Repo: https://github.com/RodneyDanger1/MYPOLSIA (public). This document is the living map of what is
DONE, what is IN FLIGHT, and exactly what comes NEXT — with where/when/why for each item.

## 0. Where things stand (as of the V19 close, base `c2e1261`)
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
unless `PHONE_SEND_BRIDGE_URL` is set (USAGE §5.2).

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

## 4. Open offers / owner decisions (nothing built unasked)
- **V19 settlement — CLOSED ✅.** All five lanes + verifier PASS; CI green at `c2e1261`; the three
  post-audit fixes (ads-route parity rows · t14 import-time stamp · bridge pre-drain RST) are in the
  commit. One parent follow-up landed with them: the phone export's stamp/filename are now UTC
  (`phone.py`, previously naive local — L2's handoff).
- **L2 carried cards (V19 API-solidity findings, ordered-not-built):** ten routes whose bodies are
  untyped dicts (no OpenAPI schema) · limit-without-total lists · unknown `status=` answers `200 []`
  on runs/outreach while other routes 422 · two defensive `RuntimeError`s · `pricing.count` is page
  length, not a true total. Details: `wave-v19/l2-api-solidity/FINDINGS.md`.
- **E7 carry-forward findings (V15.5 receipts; not yet ordered):** re-arm clears `paused_release` on
  broker acceptance → an accept-then-lost message can strand a `pending` non-scheduler row (only
  `source='scheduler'` rows have the 30-min reconciler net) · `_clear_paused_markers` unlocked
  read-modify-write on `metadata_json` · pause→release→re-arm burns `tasks.attempts` (a later lease
  expiry can terminal-fail a row that mostly sat queued) · `integrations._day_window` documented UTC
  but uses `date.today()` (process-local). Details: `wave-v16/e7-delta-sweep/FINDINGS.md`.
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
