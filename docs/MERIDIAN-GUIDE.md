# MERIDIAN — Complete Guide (v1, wave 9 tree)

> **What this is.** Meridian (renamed from MYPOLSIA) is a self-hosted, owner-operated **autonomous
> AI business-operations platform**: a FastAPI backend, a Celery/Redis task queue, a Next.js dashboard,
> a PostgreSQL database (26 tables), and ten agents that plan, research, draft, send and account —
> always behind a policy firewall (sandbox, approvals, caps, kill switch, taint escalation).
>
> **What it is not.** It is not a company-in-a-box, not an accountant, not a payment processor, not a
> domain registrar, and not a host. It is the *operations brain and cockpit*; the legal entity, bank,
> credentials, products, suppliers, domains and compliance stay with the owner.
>
> This guide was written from the code, the live app, and independent validation agents. Every
> capability statement is tied to a file/route/test or marked as needing credentials. Claims are
> deliberately conservative — "cannot" is stated as plainly as "can".

## 0. Rename note (MYPOLSIA → Meridian)

The product brand is **Meridian** everywhere it is user-visible: README/docs, dashboard title and
top bar, page metadata, API `app_name`, config default, notification text, and the guide.
**Technical identifiers deliberately keep their historical names** so imports, env contracts, the
database and Docker images do not break: `PolsiaError`, `POLSIA_*` environment variables (e.g.
`POLSIA_KILL_SWITCH`), `polsia_*` tables/containers, and the upstream comparison document
(`COMPARISON-POLSIA.md`, which cites the reference product *Polsia*). A word-boundary rename was used,
so `PolsiaError` is untouched while the word "Polsia" became "Meridian". This is a brand rename, not a
schema break.

---

## 1. What Meridian is (in one page)

Meridian runs a company's **operations loop**:

1. **Plan** — the Orchestrator reads the company profile, KPIs, yesterday, budget and task board and
   emits a dependency-aware task list for the day (morning) or a summary (evening).
2. **Dispatch** — each task is routed to a specialist agent *only if* the task's declared side-effect
   class matches that agent's permissions, the kill switch is open, the company is not paused, and the
   budget/caps allow it. Refusals are named and cost **zero** model calls.
3. **Execute** — the agent runs a bounded tool loop (DeepSeek V4.1-first; Claude Code CLI fallback),
   returns a **schema-validated** result, and the worker persists a truthful ledger row
   (tokens, cost, tools used, verdict).
4. **Persist** — tasks, runs, activity, memory, social posts, email logs, finance records, sites and
   reports land in PostgreSQL (26 tables) with dedupe keys and audit rows.
5. **Control** — every external mutation passes the policy firewall: sandbox (default **ON**: labelled
   drafts, never fake sends), caps (per channel/day/hour), approval tokens for irreversible actions,
   taint escalation for untrusted content, and a kill switch.
6. **Observe** — the dashboard shows live activity over an authenticated WebSocket, tasks, runs,
   agents, finance, sites, memory, ops and daily/evening reports.

Everything is **single-tenant and owner-scoped** by design. There is no sign-up, no multi-tenant
billing, no marketplace.

---

## 2. Architecture & stack

| Layer | Technology | Notes |
|---|---|---|
| API | FastAPI (async, Python 3.12) | 113–114 operations / 92 paths; OpenAPI at `/openapi.json`; `/apidocs` proxy for Swagger |
| Auth | `X-API-Key` header (+ short-lived WS tickets) | `API_KEY` empty = open install; wrong non-blank key = 401; WS tickets are HMAC-derived, 30 s TTL, capped |
| Worker | Celery 5 + Redis 7 | `acks_late`, prefetch 1, claim/lease/heartbeat/reapers; worker `--concurrency=1 --pool=solo` in the shipped compose |
| DB | PostgreSQL 16 (SQLAlchemy async ORM) | 26 tables; Alembic head **0020**; SQLite supported for tests/demo |
| Scheduler | Celery beat, **17 entries**, opt-in profile | Disabled by default (`profiles: ["scheduler"]`, `restart: no`) so nothing dispatches model work without the owner |
| Frontend | Next.js 14 App Router + TypeScript + Tailwind | 23 routes; live activity feed; no client-side secrets beyond the API key |
| Model runtime | **DeepSeek-first** (OpenAI-compatible `/chat/completions`, tool calling, vision) | Key stored server-side (`backend/data/deepseek_api_key.json`, 0600), masked, never logged |
| CLI fallback | Claude Code CLI (`claude -p … --output-format json`) | Per-invocation workspace, credential env stripped, bounded turns/time |
| Memory | ChromaDB primary + SQL `LIKE` fallback | Run-derived memories with tags/withholding rules |
| Observability | `activity_log` ledger, ops endpoints, WS feed, JUnit-gated tests | Numbers are measured or "unknown"; never fabricated |

Container stack (compose): `backend`, `frontend`, `celery_worker`, optional `celery_beat`
(scheduler profile), `postgres`, `redis`, `nginx` (single origin :80), `backup` sidecar.

---

## 3. Agents and orchestration (deep dive)

### 3.1 The fleet

Nine dispatchable agents plus one service-invoked planner and one schema-only deployment draft:

| Agent | Persona / purpose | Registry tools | Channels | Change classes |
|---|---|---|---|---|
| **orchestrator** | The planner: morning plan as a DAG, evening summary; reads company facts. Cannot fetch/post/spend by role. | `macro_snapshot`, `pricing_catalog`, `phone_read`, `signals_read`, `fx_convert` | — | READ, INTERNAL_WRITE |
| **business_planning** | Strategy: the few moves that change trajectory; reasons from given context | `macro_snapshot`, `pricing_catalog`, `fx_convert` | — | READ, INTERNAL_WRITE |
| **competitor_research** | Market intelligence with provenance/URLs; fetches pages and searches the web | `web_fetch`, `web_search`, `research_fetch`, `signals_read`, `domain_health`, `link_status` | web | READ, INTERNAL_WRITE |
| **social_media** | The company's public voice; drafts/posts to six platforms as labelled previews in sandbox | `signals_read` | twitter, linkedin, facebook, instagram, tiktok, youtube | + EXT_REVERSIBLE |
| **ads_management** | Paid acquisition from measured ad metrics; **cannot mutate live budgets** | — | google_ads, meta_ads | + EXT_REVERSIBLE |
| **email_outreach** | Finds fitting prospects and sends compliant email; drafts in sandbox | `link_status` | email | + EXT_REVERSIBLE |
| **code_generation** | Plans or ships one reviewable change; reads/writes a run workspace; GitHub PRs are approval-gated, **never merges** | `code_write`, `code_read`, `code_list`, `github_repo`, `github_branch`, `github_commit`, `github_pr` | github | all four |
| **customer_support** | Reads the support inbox and replies; phone read-only | `phone_read` | email | + EXT_REVERSIBLE |
| **finance** | Owns revenue/spend facts and alerts; Stripe reads, refunds approval-gated | `macro_snapshot`, `pricing_catalog`, `fx_convert` | stripe | + EXT_IRREVERSIBLE |
| **company_planner** (service-invoked) | Interactive propose/confirm planner; not a plan target | `macro_snapshot`, `pricing_catalog` | — | READ, INTERNAL_WRITE |
| **deployment** (schema only) | Draft shape exists; **no agent class/router/table** | — | — | — |

Enforcement is at **call time** (`tool_registry.execute_tool`), not in prompt text: an agent asking for
a tool outside its allowlist gets a named `E_CONFIG` and no execution. Outputs are schema-validated,
with a bounded repair attempt; a refused payload is never stored as completed.

### 3.2 How one run works

Context build (9 layers: identity, tool doctrine, company, business state, yesterday/memories, today's
tasks, extra context, task brief; 12k token planning budget, 160k ceiling) → provider chosen once per
run → tool loop (registry tools, max 4 DeepSeek iterations, max 10 metered calls/run) → schema
validation + one repair → verdict → worker persists `agent_runs`, usage, artifacts, memory, side
effects. Per-run budget guard refuses before a provider call when the **provable worst case** exceeds
the remaining cap; a capped run is not retried.

### 3.3 Orchestration: plan → DAG → queue

- Morning plan creates tasks with `dedupe_key(agent_type, bucket(title))`, `source="orchestrator"`;
  dependencies are **titles**, resolved to ids in a second pass; roots dispatch after commit;
  dependents are released by the worker's completion hook; a model-authored cycle is detected by
  reachability, released as roots, and recorded; dependents of a failed parent are **failed by name**
  (`dependency_blocked`).
- Routing check **before construction**: a task's declared `metadata.side_effect` must fit the routed
  agent's classes — scheduling "refund a customer" on `business_planning` is refused with **zero model
  calls** (measured $0.0000).
- Evening cycle produces the summary; no next-run time is ever claimed from a stopped beat.

### 3.4 Celery lifecycle

claim (atomic; stale takeover) → pause gate → budget gate → run row committed **before** the model call
→ heartbeat (300 s, lease 900 s) → ownership re-check before settle → retry ladder (timeouts/exit/JSON/
DB/Redis/Integration; countdown ≤600 s) → terminal settle with usage merge (a late failure can never
erase the interruption record) → reapers for stale claims/runs, stranded pending, paused re-arm and
72 h approvals. A kill-switch freeze denies dispatch before any model call.

### 3.5 Task state machine

`pending → in_progress → awaiting_approval → completed | failed | cancelled | rejected`; terminal
states have no outgoing edge; two explicit re-arms (`failed → pending` manual retry,
`awaiting_approval → pending` token attach); every transition writes an audit row.

---

## 4. Safety & control (why it can be left running)

| Control | What it does |
|---|---|
| **Sandbox** (default ON) | Every external action is a labelled draft: no provider call, no fake id, `drafted_not_sent` recorded. Turning it off requires an owner edit + restart. |
| **Approvals** | HMAC single-use tokens (TTL 1 h; consumed in a dedicated transaction; replay = `POLSIA-E121`) for every irreversible external action, any action from a tainted run, and values ≥ $25. Minting is owner-key only and audited. |
| **Kill switch** | `POLSIA_KILL_SWITCH=freeze` refuses all dispatch/tool calls; `throttle` refuses external-class agents; direct-effect endpoints check it before transport. |
| **Caps** | Day/hour channel caps (email 50/day/15 hour; twitter 12/3; others 3–10/day), per-run $2, day $25, $1 reserve; an uncountable cap refuses by name rather than reading 0. |
| **SSRF/egress** | DoH resolution, public-IP-only, ports 80/443, pinned dial, redirect origin lock. |
| **Taint** | Untrusted content is wrapped/labelled; a tainted run escalates every external action and taint is sticky. |
| **Secrets** | One secret store; DeepSeek key 0600, masked; query-string credentials scrubbed from logs; export redacts tokens; access logs path-only. |
| **Audit** | `activity_log` + `agent_runs` + `consumed_approvals`; every refusal names its code and fix. |

---

## 5. Features (screen by screen)

| Route | What it does |
|---|---|
| `/dashboard` | Health/IDLE, Auto Mode window, business KPIs (measured vs configured), tasks, daily report, media, website/domain card, phone, social, outreach, ads, activity feed (WS), chat rail |
| `/tasks`, `/tasks/[id]` | Task board, filters, create task, run now, status transitions, attachments, run history, retired attempt error |
| `/runs`, `/runs/[id]` | Run ledger, per-run cost/tokens/tools/verdict, artifacts viewer |
| `/agents` | Roster, per-agent status, manual trigger (routing-gated) |
| `/planner` | Company planner propose → confirm gate |
| `/schedules` | Cadence entries (create/pause/edit) that beat turns into tasks |
| `/finance` | AI spend (partial/floor semantics, never fake $0.00), MRR/ARR, snapshot, expenses, unit economics, human-rate planning |
| `/invoices` | Create/send/void invoices; send goes through the compliance floor |
| `/sites` | Generate static sites (classic/modern/bold), regenerate, preview pages, package download, custom-domain **record**, deploy (Render dry-run/live gated) |
| `/social` | Drafted/published posts and schedule states |
| `/outreach`, `/prospects` | Prospect list, statuses, suppression-aware send path, unsubscribe handling |
| `/support` | Inbox items, composer (draft), inbound webhook |
| `/phone` | Read-only Phone Link inbox/calls; SMS send refuses unless a paired driver exists |
| `/ads` | Campaign/metric rows from completed ads runs; no live budget mutation |
| `/memory` | Owner memory store + search + stored tags/withheld-claim labels |
| `/ops` | Capabilities matrix, agent quality, artifacts, approvals, sandbox state, cleanup tools |
| `/settings` | Company/profile/KPIs, model key, integrations (real status/test), emails, night tasks, data export/wipe, dashboard editor |
| `/usage` | Spend/usage views and CSV/ZIP exports |
| `/capabilities` | Capability manifest rendered from code |
| `/apidocs` | Swagger UI for the API |

---

## 6. User guide

### 6.1 Install (Docker, recommended)

1. `cp .env.example .env`; set **`API_KEY`** (32+ chars) and keep `SANDBOX_MODE=true` for the first run.
2. `docker compose up -d --build`; wait until backend healthy.
3. `docker compose exec backend alembic upgrade head`; `docker compose exec backend python scripts/seed_company.py`.
4. Open `http://localhost` (nginx) — the dashboard.
5. Optional periodic work: `docker compose --profile scheduler up -d celery_beat` **only** when you want
   cycles/sweeps (they dispatch model work and cost money).

### 6.2 First-run configuration

Model key (Settings → AI model: DeepSeek key) → company profile (name, mission, website, sender legal
name/postal address — required before any email) → integrations (SendGrid/Stripe/Render/GitHub/Ads) →
emails/inbox → create a site/prospects/tasks → optionally Auto Mode (a bounded window) → arm beat.

### 6.3 Daily operation

Morning plan (if beat armed or manual trigger) → review tasks/approvals in `/ops` → read the daily
report → check finance partial/floor labels → approve parked actions → review outreach/social drafts →
evening summary. Everything the agents "did" is in the run ledger; nothing is claimed without a row.

### 6.4 No-Docker development path

Backend: `.venv` + `DATABASE_URL=sqlite+aiosqlite:///./polsia.db` + `alembic upgrade head` + seed, run
uvicorn. Frontend: `npm ci` then `npm run dev` with `NEXT_PUBLIC_API_URL`/`NEXT_PUBLIC_API_KEY`;
production `npm run build && npm start` (standalone). The README documents the proxy layout (:3000 +
:8000 behind :80).

### 6.5 Backups, export, wipe

Docker volumes (5) + `make restore-drill` (never against the live stack) + scheduled `backup` sidecar;
`GET /api/v1/data/export` (ZIP/CSV with manifest) and the data-wipe endpoint (owner explicit).

---

## 7. What Meridian can do today (capability ledger)

- Run agents with a real tool loop and **truthful** cost/token accounting; refuse by name when a key,
  credential, cap or approval is missing.
- Plan a day as a dependency-aware DAG; route, dispatch, release, retry, reap and block honestly.
- Send compliant email (footer + opt-out + suppression) and receive verified inbound mail (SendGrid
  ECDSA/OAuth); read/send Stripe webhooks; refund `pi_` payments with approval; open GitHub PRs
  (dry-run first); deploy a generated site to Render (dry-run default, gated live); post text to six
  social platforms; read phone messages read-only.
- Generate and serve static sites; regenerate with publishing warnings; record a custom domain (not
  DNS); package/download; verify DNS/TLS read-only.
- Track revenue/expenses/invoices; report `null`/partial/floor honestly; export data; search memory.
- Enforce sandbox, approvals, kill switch, caps, SSRF, taint, and a full audit ledger.

## 8. What it needs to run a business entirely

| Requirement | Meridian today | Gap / owner action |
|---|---|---|
| **Legal entity, EIN, permits, tax registration** | nothing (out of scope) | owner/accountant |
| **Bank + payments** | Stripe reads/webhook/refund adapters | owner Stripe account, live keys, tax/chargeback process |
| **Domain + DNS** | records hostname only; read-only DNS/TLS check | registrar/DNS is owner-side; a DNS/hosting adapter is not built |
| **Hosting/deploy** | Render adapter (needs service) | Render account/key or Hostinger path; file-level hosting control not built |
| **Business email domain** | SendGrid send + inbound; SPF/DKIM/MX are owner-set | email provider account + DNS records |
| **Mailbox management (Workspace/M365)** | **none** (no Gmail/Graph/IMAP/SMTP connector, no OAuth) | build a connector + OAuth consent; not in the product today |
| **Product/suppliers/inventory/shipping** | research and drafting only; no orders/stock/shipping API | owner suppliers, 3PL/shipping accounts, inventory system |
| **Bookkeeping/accounting/tax filing** | expense/revenue/invoice records + export | accountant; ledger is operational, not books-of-account |
| **Customer support** | inbox + reply + compliance floor | real inbox provider, SLAs, human escalation |
| **Marketing** | drafts + six platform adapters (text only) | ad accounts (budget mutation refused), media uploads refused |
| **Model** | DeepSeek key (or Claude CLI) | owner key, spend cap, acceptable-use |
| **People/operator** | approvals, sandbox, kill switch, reports | an owner must review and decide |

**Verdict:** Meridian can run the *operations loop* of a small online business (planning, research,
copy, email, sites, reporting, finance tracking) with an owner in the loop. It **cannot** be the legal
entity, bank, accountant, registrar, host, inventory or mailbox — and it should not be sold as one.

---

## 9. 100 things Meridian CAN do (today, with the stated caveats)

1. Draft a morning operating plan as a task DAG.
2. Draft an evening summary of the day's outcomes.
3. Create scheduled task rows; run a task immediately.
4. Route a task to the right agent and refuse a mismatched side-effect class.
5. Show the full 10-agent roster with per-agent status.
6. Show 17 registry tools and their declared arguments.
7. Fetch a specific URL safely (DoH/SSRF floor, 8k excerpt).
8. Web-search via the DeepSeek search tool (metered).
9. Fetch via DeepSeek research fetch (metered).
10. Read macro snapshot and pricing catalog facts.
11. Convert currencies with dated ECB rates.
12. Read configured signal headlines.
13. Check a hostname's DNS A/AAAA + TLS certificate (read-only).
14. Check a URL's status and Wayback history (read-only).
15. Read the owner's phone inbox/calls (read-only; no codes).
16. Generate a static website (classic/modern/bold) for the company profile.
17. Regenerate a site and receive publishing warnings.
18. Preview generated site pages over nginx.
19. Download the site package.
20. Record a custom domain string on a site (not DNS).
21. Plan a Render deploy and read the exact planned call (dry-run).
22. Deploy a site to Render when configured, permitted and approved.
23. Draft social posts (labelled drafts in sandbox).
24. Post text to Twitter/X, LinkedIn, Facebook, Instagram, TikTok, YouTube when configured and sandbox off.
25. Schedule social content for later.
26. Draft cold emails with footer/opt-out and AI disclosure.
27. Send email when SendGrid is configured and sandbox off.
28. Refuse a send to a suppressed/unsubscribed address by name.
29. Receive verified inbound SendGrid mail into the support inbox.
30. Reply to a support message (draft; send gated).
31. Store and search owner/company memories.
32. Withhold memory rows whose claims failed review.
33. Record a prospect and update its status.
34. Mark a prospect unsubscribed via signed link.
35. Track email logs with provider ids and reply stamps.
36. Create an invoice.
37. Send an invoice email through the compliance floor.
38. Void an invoice.
39. Record revenue snapshots and expenses.
40. Show MRR/ARR/active subscribers when measured, `null` otherwise.
41. Show AI spend as measured, partial/floor, or unmeasured — never fake $0.00.
42. Enforce an AI day budget and per-run cap.
43. Refuse a run whose provable worst case exceeds the cap.
44. Show token/cost/tool usage per run.
45. Export all data as CSV/ZIP with a manifest.
46. Wipe data with an explicit owner action.
47. Approve a parked irreversible action with a single-use token.
48. Refuse a replayed/expired approval by name.
49. Rotate approval tokens by rotating the secret.
50. Freeze the fleet with the kill switch.
51. Throttle external agents while leaving reads available.
52. Pause the company and release claims/refund attempts.
53. Resume paused work with re-arm sweeps.
54. Recover an expired worker claim (lease takeover).
55. Retry a transient failure with backoff.
56. Fail dependents by name when a parent fails.
57. Detect and release a model-authored task cycle.
58. Show a live activity feed over authenticated WebSocket.
59. Show task history with retired attempt errors.
60. Show run artifacts and downloadable logs.
61. Show a capabilities matrix derived from code.
62. Show agent quality (fleet-only) with failure recency.
63. Show sandbox state and wire refusal reasons.
64. Read integration status and masked secrets.
65. Save provider credentials through Settings (secret store).
66. Test an integration and get the provider's real answer/refusal.
67. Configure email notification address and switches.
68. Configure night-task settings.
69. Configure the dashboard layout.
70. Configure company KPIs (clearly labelled "configured").
71. Configure Auto Mode window.
72. Create a cadence schedule (once/daily/weekdays/weekly).
73. Let beat turn a due schedule into a task (when armed).
74. Trigger a single agent manually.
75. Use the Company Planner propose→confirm flow.
76. See the single live confirm gate and its token.
77. Refuse a superseded/expired confirm token.
78. Use the API directly with an API key.
79. Mint a short-lived WebSocket ticket.
80. Get 401 + WWW-Authenticate for a missing/wrong key.
81. Get named 404/409/413/429/502/503/504 domain errors.
82. Discover the API through OpenAPI/Swagger.
83. Use CORS from the allowed dashboard origin.
84. Run with SQLite for development/tests.
85. Run with PostgreSQL in Docker for production.
86. Run migrations to head 0020.
87. Seed company data.
88. Back up volumes with the sidecar.
89. Run a restore drill against a copy (not live).
90. Run a smoke acceptance command.
91. Run the full backend test gate (5,528 passing at wave 9).
92. Run frontend typecheck/lint/jest.
93. Run with the Claude CLI instead of DeepSeek (if installed/authenticated).
94. Run fully mocked in CI (`CLAUDE_CLI_MOCK=true`), honestly marked partial.
95. Use provider adapters that classify answered vs no-answer faults (no silent success).
96. See `outcome_unknown` on ambiguous mutations instead of a lying "failed".
97. Get SQL scrubbed from client error messages for range errors (422) while detail is logged.
98. Get a request id on refusals matching `X-Request-ID`.
99. Have an outbound stop honored at send time (per-send suppression re-check).
100. Have every refusal name a code, message and how-to-fix.

## 10. 100 things Meridian CANNOT do (and why)

1. Create or manage a legal entity — out of scope.
2. File taxes — out of scope.
3. Be your bookkeeper of record — operational ledger only.
4. Open a bank account — out of scope.
5. Buy a domain — owner action only.
6. Change DNS/nameservers — no DNS-write adapter.
7. Point a domain at a host — owner/registrar step.
8. Manage Hostinger/website hosting files — no host API/FTP/SSH adapter (Render only).
9. Install WordPress/SSL on your host — out of scope.
10. Manage Google Workspace/Microsoft 365 mailboxes — no Gmail/Graph/OAuth/IMAP/SMTP connector.
11. Read your Gmail/M365 inbox — connector absent.
12. Send as your Workspace mailbox — connector absent.
13. Create email mailboxes/users — provider admin API absent.
14. Manage calendars/contacts — no connector.
15. Create social accounts — owner action; platforms also restrict automation.
16. Bypass platform rate limits/ToS — refused by design.
17. Post images/video to X — named refusal.
18. Post media generally where adapter lacks it — no upload path.
19. Thread/reply chains on X — named refusal.
20. Mutate live ad budgets — hard-coded named refusal.
21. Create ad campaigns live — ads adapter reads metrics; creation not wired.
22. Manage Google Ads billing — out of scope.
23. Purchase inventory — no purchasing/payment rails.
24. Manage stock counts — no inventory module.
25. Book shipping labels — no carrier integration.
26. Track packages — no carrier integration.
27. Handle returns/refunds end-to-end — refund adapter exists but workflow/human process is owner's.
28. Pay suppliers/contractors — no payout rails.
29. Run payroll — out of scope.
30. Produce compliant accounting statements — no double-entry ledger.
31. File sales tax — out of scope.
32. Guarantee product safety/labeling — owner/compliance.
33. Make legal marketing claims for you — copy firewall blocks unverified claims, not a legal review.
34. Replace a human customer-support team — email reply only.
35. Provide phone voice/IVR support — SMS/phone read-only, send needs a paired driver.
36. Send SMS without a paired driver — named `E_PHONE_SEND_UNPAIRED`.
37. Read phone verification codes — deliberately withheld.
38. Answer chat with a model — chat is deterministic routing, not an LLM chatbot.
39. Execute arbitrary shell commands from agents — allowlist + workspace only.
40. Merge a PR — not expressible.
41. Force-push — refused.
42. Delete GitHub repositories — no tool.
43. Create a deployment agent run — no class/registry/routes.
44. Wire the DAG planner module — zero production callers.
45. Schedule the morning plan itself via `orchestration/planner.py` — unwired.
46. Run with no model key or CLI — measured pre-dispatch refusal.
47. Run without Redis — broker required.
48. Run without Postgres in the Docker stack — DB required (SQLite for dev only).
49. Be multi-tenant — single owner by design.
50. Sign up users / bill subscriptions — out of scope.
51. Provide an SLA — self-hosted, no guarantee.
52. Recover data from a failed disk automatically — backups exist but restore is manual.
53. Encrypt secrets at rest beyond file permissions — no KMS/vault integration.
54. Provide SSO/RBAC — single API key.
55. Audit per-user activity — one owner identity.
56. Guarantee identical output across runs — LLM non-determinism.
57. Guarantee factual accuracy of research — provenance links, human review required.
58. Avoid hallucination entirely — schema/tools reduce, not eliminate.
59. Run unbounded agent loops — iteration/call/character caps.
60. Exceed per-channel caps — refused; caps are conservative.
61. Enforce a global hourly budget — no such policy (day/per-run only).
62. Count an uncountable usage channel — named `cap_unknown`, refuses.
63. Send from an unauthenticated domain reliably — deliverability needs SPF/DKIM/DMARC.
64. Guarantee email inbox placement — provider/reputation matter.
65. Parse arbitrary inbound email providers — inbound adapter is SendGrid-shaped; JSON path for others.
66. Accept unsigned inbound mail — 401.
67. Bypass approval for irreversible actions — no.
68. Bypass sandbox without an owner edit+restart — no.
69. Escape taint escalation — sticky.
70. Fetch private/intranet URLs — SSRF floor refuses.
71. Use non-80/443 ports for tools — refused.
72. Follow cross-origin redirects — refused.
73. Read files outside the run workspace — refused.
74. Publish a site to your domain automatically — DNS/host step missing.
75. Verify DNS for you beyond read-only lookup — no write.
76. Manage SSL certificates — host's job.
77. Generate photos/video assets — no image/video generator.
78. Edit audio/video — no media pipeline.
79. Translate arbitrary content with a dedicated MT service — only through model prompts.
80. OCR scanned documents — vision describes images, no OCR pipeline.
81. Sign contracts/e-signatures — out of scope.
82. Send physical mail — out of scope.
83. Do outbound cold calls — no telephony.
84. Integrate Slack/Discord/Teams — no connectors.
85. Integrate QuickBooks/Xero — no accounting integration.
86. Integrate Shopify/WooCommerce/Etsy/Amazon — no store/catalog integration.
87. Sync orders/customers from a store — no store connector.
88. Use real-time ad platform APIs for optimization — reads only.
89. Guarantee no data loss on worker kill — measured accounting is designed for it, but disk/infra failures are host risk.
90. Run without Docker on production scale — noHA/scaling orchestration.
91. Provide native mobile apps — web only.
92. Provide a public consumer-facing storefront — generated static pages only, no checkout.
93. Take payments on the generated sites — no commerce backend.
94. Manage shipping rates/checkout — no commerce.
95. Do A/B testing at scale — no experiment engine.
96. Guarantee SEO rankings — generates pages, not rankings.
97. Run continuous 24/7 cycles by default — beat is off until armed; that is intentional.
98. Spend without the owner's key/cap — model key required; sandbox default.
99. Recover rotated credentials automatically — owner rotates.
100. Replace the owner's judgment or liability — the owner remains responsible.

---

## 11. Cost model (realistic)

- **Model**: DeepSeek V4.1 pricing; the platform caps per-run $2 and per-day $25 by default. A normal
  small-business day of drafts/research is cents-to-low-dollars; the cap is a ceiling, not a bill.
- **Infra**: 1 small VPS (4 GB) for Postgres+Redis+backend+worker+nginx ≈ $10–25/mo; Render deploy
  free/starter; backups storage.
- **Providers**: SendGrid free tier/paid; domain ~$10–20/yr; email/Workspace ~$6–12/user/mo; Stripe fees
  2.9%+30¢; ads budgets separate; product samples/shipping.
- **Operator time**: approvals + weekly review; the system is designed for an owner in the loop.

## 12. Security & risk (honest)

- **Sandbox default is the main safety**, but flipping it off + configuring keys makes actions real.
- **Rotate before exposure**: leaked Stripe webhook secret (wave-6 finding), old DeepSeek key in
  backups, `APPROVAL_SECRET` (a transcript printed it), the default `API_KEY`, and the default Postgres
  password. API key is inlined in the frontend bundle — treat the dashboard as owner-only.
- **No Vault/KMS**: secrets are files with 0600 + masked surfaces; full-disk encryption is the host's job.
- **Worker/DB outages**: designed for measured accounting and honest `unmeasured`, not for HA.
- **Compliance**: email footer/opt-out/suppression are implemented, but CAN-SPAM/GDPR/ADS terms and ad
  platform automation rules are the owner's responsibility.
- **LLM risk**: research may be wrong; schema/prompt/taint controls reduce but do not eliminate it. Read
  provenance and approve irreversible actions.

## 13. Where the project stands (wave 9)

- Waves 1–9 landed; wave 9 commit `9de716e`; backend gate **5,528 passed / 0 failed / 0 errors /
  12 skipped**; frontend **152 suites / 1,331 tests** (wave 8); ruff both pins + mypy clean.
- Remaining planned waves: 10 adversarial/fault injection · 11 system validation & research synthesis
  (incl. domain/email integration design) · 12 leave-no-defect I · 13 code completion/contract
  enforcement II · 14 pre-release regression/release readiness · 15 **owner-gated** accounts/keys/
  domains/purchases/deploy.
- Known carries: finance unit-economics ratio (priced completions), SendGrid dedupe-fallback decision,
  structured provider error context, F-45-R1 nginx error log channel, sentinel's wave-9 lane, PG-only
  shipped-bound drills, owner credential rotations.

## 14. Quick FAQ

**Can it run my business while I sleep?** It can draft/plan/track overnight *if* beat is armed and a
model key is present; external sends remain drafts unless sandbox is off and caps/approvals allow.
**Does it need 24/7 uptime?** No; beat is opt-in and cycles self-gate. The dashboard is the cockpit.
**Is it safe to expose?** Only after rotating API key/Postgres/Stripe/approval secrets and keeping the
sandbox on until you have reviewed a week of drafts.
**Can it manage my domain/email?** Not today: domain control and Workspace/M365 mailbox management are
not implemented; SendGrid send/inbound and read-only DNS/TLS checks are.
**What is the single most honest sentence?** *Meridian is a self-hosted AI operations cockpit for a
small online business, with a strict refuse-by-name safety model — not a replacement for the legal,
financial and infrastructure services that make a business real.*
