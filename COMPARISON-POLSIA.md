# MYPOLSIA vs the real Polsia — an honest comparison

*Written 2026-10-19 against the live stack at `http://127.0.0.1`. **Updated after wave V24 landed**
(settled on `51affcf` + the V24 tree: backend **2,903 tests / 0 failed**, jest **918/918**, Playwright
**43 passed / 8 skipped / 0 failed**, smoke **13/13**, live drills **15/15**). Every claim is sourced:
product code, `COMPLETENESS.md`, the wave receipts, live API/page checks, and web research performed
through the **DeepSeek Search & Fetch API** (§8). Where something is still not true it is marked
**missing**; what V24 shipped is marked **✓ V24** with its proof.*

---

## 1. What the real Polsia is

Polsia (polsia.com, founder Ben Broca) is a hosted, multi-tenant SaaS that sells itself as "AI that
runs your company while you sleep": you describe an idea; an orchestrator agent plans, then
specialist agents execute — planning docs, code, website/app builds, hosting and database
provisioning, payments setup, Meta ads, cold outreach, social content, customer support, and a
nightly autonomous task.

**Its stack (from the founder's own talk):** Render for web services, **GitHub** for source history,
**Neon** for Postgres, and "a consistent set of MCP tools layered on top" for every company it spins
up. Specialized agents include a cold-outreach agent with its own lead-gen MCP and a small CRM, and an
ads agent that manages Meta.

**Commercials:** $49/mo (plans from $19 hosting-only to $999 Venture), **one task credit = one
agentic task**, 5 credits/month included (one nightly autonomous task part of the subscription),
extra credits metered (~$1/run), a **20% revenue share** on business revenue routed through Stripe
Connect, and the user keeps IP ownership. A free trial exists.

**Reality check (user reviews):** Trustpilot sits around 3.4 (some pages 2.6) with recurring themes —
rapid credit consumption, websites that stopped working, failed deployments, broken dashboards, and
"little or no revenue after months." It is a genuinely ambitious product with real availability and
outcome complaints; the marketing is stronger than the reliability record.

## 2. Side-by-side capability matrix

| Capability | Real Polsia | MYPOLSIA (live, V24 build) | Verdict |
|---|---|---|---|
| Orchestrator + specialist agents | ✓ (9 agents, orchestrator plans) | ✓ 10 agents (orchestrator, planning, research, social, email, support, ads, code, finance, planner) with real prompts + a shared pipeline | **parity** |
| Autonomous cadence | ✓ one nightly task; extra runs cost credits | ✓ morning/evening daily cycles + scheduler lanes, 15-min/5-min ticks, auto-mode advisory lock; **no per-run credit meter** | **parity+** (we don't meter) |
| Agent know-how | ✓ proprietary prompts, MCP tools | ✓ doctrine layers + firewalled registry of **8 tools** incl. **✓ V24 `web_search`** (DeepSeek Search API) and **✓ V24 code write/read/list** — proven live: run 238 recorded `["web_search","web_fetch"]` | **parity** |
| Builds websites | ✓ generates and **deploys** sites/apps | ✓ **V24**: profile-driven generator (HTML-escaped, script-free, self-contained) served at `/sites/<slug>/` through nginx, plus a `/sites` page (create/list/preview); **no cloud deploy** | **parity on building; deploy is the remaining gap** |
| Writes code | ✓ writes product code and iterates | ✓ **V24**: firewalled workspace (`code_write`/`code_read`/`code_list`, traversal/absolute/symlink refusals, text ≤256 KiB); no git/CI tool yet | **near-parity** |
| Deploys / provisions infra | ✓ Render + Neon + GitHub provisioning per company | **missing** — single compose stack, no provisioning adapter | **biggest real gap** |
| Source history | ✓ GitHub per company | ~ repo is a git repo, but no agent-facing GitHub push tool | gap (P1) |
| Payments | ✓ Stripe Connect onboarding, revenue share | ✓ Stripe integration + webhook + invoicing/subscriptions (test key; live key owner-gated); no Connect onboarding | **near-parity** (live key + Connect = owner step) |
| Ad management | ✓ Meta ads agent with real spend | ~ reads + **✓ V24 measured `revenue_usd`/`roas`** (None when unmeasured, never a fabricated 0); **writes still unproven** | **partial** — writes gated |
| Cold outreach | ✓ lead-gen MCP + mini CRM + sending | ✓ outreach agent, compliance gating (CAN-SPAM footer, unsubscribe), SendGrid — sandbox-gated; website URL required for footers | **near-parity** (needs live email + domain) |
| Social | ✓ posts | ✓ social agent + posts UI | parity |
| Customer support | ✓ replies to inbox | ✓ support agent + owner inbox + filing form | parity |
| Research / planning | ✓ | ✓ planning docs, competitor research, macro snapshot + **✓ V24 `web_search`** live | **parity** |
| Runnable tasks | ✓ each task is an agentic run | ✓ **V24**: `POST /tasks/{id}/run` + **Run this task** on pending rows; exactly one run per dispatch (worker claim is the mutex); terminal rows refused 409 `E_TASK_NOT_RUNNABLE` | **parity** |
| God Mode / override | ✓ advertised | ~ task create/run, chat console, agent triggers — no single cockpit | **partial** |
| Multimodal (image input) | ✓ (screenshots, charts) | **missing** — text only, despite DeepSeek V4.1-Flash vision | gap (P1) |
| Phone / SMS / calls | ✗ | ✓ **unique**: Phone Link read (threads, calls, codes, media) + host send bridge + `phone_read` agent tool | **we only** |
| Self-hosted, no revenue share | ✗ (SaaS, 20% take) | ✓ your box, your Postgres, your data; no credits, no take rate | **we only** |
| Open API + docs | ✗ (no public API docs) | ✓ 76-route REST API, Swagger at `/apidocs`, parity pins | **we only** |
| Honesty guarantees | unknown; reviews cite silent breakage | ✓ named refusals everywhere, no fabricated zeros, "not recorded" states, verified by gates | **we only** |
| Test/quality evidence | not public | ✓ **2,903 backend / 918 FE / 43 Playwright / 13-check smoke / mypy+ruff hard gates** | **we only** |
| Multi-tenant onboarding | ✓ signup → company in minutes | ✗ single-tenant by design | deliberate difference |

## 3. What Polsia has that we lack — and what improving it would take

Ordered by real-world impact for you:

1. **Infrastructure provisioning (Render + Neon + GitHub).** Polsia's headline trick is that an agent
   can stand up a working web service, database and repo for a company. We now have a **site generator
   + hosting on your own box** (V24) and the full compose stack; we do not have cloud provisioning.
   *Why it matters:* "spin up a real, public business" vs "spin up a page on my box."
   *Plan:* a `deploy` adapter (Render/GitHub/Neon APIs) behind a firewalled tool + owner-approved
   credentials; an optional integration, never a requirement. (P1, large.)
2. **Vision input.** DeepSeek V4.1-Flash is natively multimodal; our task form is text-only.
   *Why:* "here's a screenshot of my competitor's landing page — beat it" is table stakes in 2026.
   *Plan:* attach images to tasks (content parts on user messages), thumbnails + size caps, stored
   with the task; the runtime already speaks the API. (P1, medium.)
3. **God Mode cockpit.** One surface to see and override everything an agent is doing. We have the
   pieces (triggers, task run, chat, ops panel) but no single control room. (P2, medium.)
4. **Lead-gen + CRM depth.** Polsia ships a lead-gen MCP and a mini CRM. We have outreach + a support
   inbox + compliance, but no prospect database/enrichment loop. (P2, medium; compliance-sensitive.)
5. **Real ad writes.** Our ads agent reads Google/Meta and reports honest revenue/ROAS (V24); actual
   campaign creation/adjustment is sandbox-gated and unproven. (P1 gated by owner keys; medium.)
6. **Business email + domain provisioning.** We send through SendGrid with a compliance footer; no
   mailbox or domain provisioning. (P2, small.)
7. **Credit/usage metering.** Polsia meters every agentic task; we meter cost per run instead and give
   unlimited tasks. That's a *feature for you*, not a gap — noted so the difference is deliberate.
8. **Public availability engineering.** Polsia's reviews show failed deployments; our equivalent risk
   is the (single) compose stack — we mitigate with backups/restore drills and CI, not multi-region.

## 4. What MYPOLSIA has that the real Polsia cannot (or does not) offer

- **Full ownership, zero take rate.** No 20% revenue share, no credit meter — your hardware, your DB,
  your data, your IP.
- **A personal phone bridge.** SMS threads, calls, verification codes and media from Phone Link, plus a
  `phone_read` agent tool with codes structurally absent and a host send bridge. Polsia has no phone
  concept.
- **An open, documented, tested API.** 76 routes, Swagger UI at `/apidocs`, parity pins that fail CI if
  the surface drifts.
- **Honesty as a contract.** Every failure is named; "no data" is never rendered as 0; unmeasured
  values are dashes; agent refusals are first-class products (the activity feed shows them verbatim).
- **Verifiable quality.** 2,903 backend tests, 918 frontend tests, 43 Playwright specs, a 13-check
  one-command smoke, hard mypy/ruff gates, and a 92% coverage floor — all in CI, all reproducible
  locally. Polsia publishes no such evidence.
- **Firewalled tool-calling.** Per-agent allowlists with policy codes; an agent literally cannot call a
  tool it was not granted, and the refusal is audited.
- **Sandbox mode.** A single switch that turns every external action into a named refusal — safe by
  default.

## 5. What V24 landed (all verified on the rebuilt stack)

Six lanes on `51affcf`, settled by the parent; the tree is committed green and the stack is live.

| Seat | Delivered | Proof |
|---|---|---|
| **L1 — runnable tasks** | `POST /api/v1/tasks/ID/run` (202 dispatched), **Run this task** on pending cards + detail, New Task copy states the wait | Live drill: created → 202 → completed in 18.3 s; second dispatch while pending produced **exactly 1 run**; completed-row rerun **409 `E_TASK_NOT_RUNNABLE`** |
| **L2 — agent tools** | Firewalled **`web_search`** on DeepSeek `POST /v1/tools/search` (app's own key resolution, named refusals `E_VALIDATION/E_CONFIG/E_AUTH/E_BALANCE/E_RATE_LIMIT/E_UNAVAILABLE`, honest `[]`), **code write/read/list** workspace | Live `competitor_research` run: 5 ranked results + `polsia.com` fetched; run ledger `tools_used: ["web_search","web_fetch"]`; smoke `registry_tools=8` |
| **L3 — website builder** | Profile-driven site generator, `/sites/<slug>/` hosting via nginx, `/sites` page (create/list/preview), nav entry | Live: POST 201 → page 200 with the requested title, script-free, `styles.css` 200; `GET /sites` lists it |
| **L4 — ads truth** | `revenue_usd` measured-or-`None`; `roas` only when revenue **and** spend are measured | Drill: honest empty when no campaigns; unit suite pins None ≠ 0 |
| **L5 — UI polish** | 30 s focus refresh, one activity read for two consumers, hidden-tab polling stopped, DOM-shaped skeletons, working Retry, lazy chat rail | Measured before/after in the lane receipt; jest 918/918 |
| **L6 — backend hardening** | N+1 removals (2-statement channel snapshot, one-statement spend sum, one-lookup dedupe), **migration 0012** indexes, corrupt-JSON reads refuse by name instead of 500ing, timeout audit | Gate 2,903/0/0/13; migration `0012 (head)` applied to live Postgres |

**Settle fixes (parent, both pinned):** nginx answered `/sites` with its own 301 into the static mount's
404 — an exact `location = /sites` now hands the page to Next (pin tests 21/21 green). The V18
`get_or_create` interleaving test now patches the write-path ORM seam created by L6's split
(`_get_report_orm`); same forced race, 9/9 green.

**Honest residuals:** `ori_question` rides only when the model supplies it (no task-title seam in
`invoke()`); the code workspace is one root, not per-run; sites are self-hosted, not deployed; no
agent-facing GitHub tool yet.

## 6. Where to improve next, and why (roadmap)

**P0 — before selling anything (owner actions + small code):**
1. Stripe **live** key + Connect onboarding flow (billing is the one thing a business cannot fake).
2. Set the real **website URL** (compliance footer refuses by name until then) and rotate the API key.
3. Keep backups verified (already daily + restore drill).

**P1 — parity with Polsia's spine (code):**
4. **Deploy/provision adapter** (Render/GitHub/Neon) behind firewalled tools with owner-approved creds —
   the last thing between "MYPOLSIA can build a site" and "MYPOLSIA can launch a business."
5. **GitHub push tool** for the code agent (real source history), then a code-review agent pass.
6. Vision input on tasks (DeepSeek V4.1-Flash already supports it).
7. Verified ad writes (Meta/Google) with budgets + kill switch.

**P2 — differentiation and polish:**
8. God Mode cockpit (single override surface over runs/tools/approvals).
9. Lead-gen/enrichment loop with compliance rails.
10. Multi-tenant mode **only if** you want to sell it as SaaS — otherwise stay single-tenant; it is the
    better product for you (no 20% take, full data ownership).

## 7. How to verify every claim here

```
cd backend && ../.venv/Scripts/python.exe -m pytest tests/unit tests/parity tests/redteam   # 2903/0/0/13
cd frontend && npx jest          # 918/918
cd e2e && npx playwright test    # 43 passed / 8 skipped
export API_KEY=…; bash scripts/smoke.sh --base http://127.0.0.1   # 13 checks
curl -s http://127.0.0.1/apidocs | head        # Swagger UI
curl -s http://127.0.0.1/api/v1/ops/capabilities -H "X-API-Key: $API_KEY" | python -m json.tool
```

Live-drill script (tasks run, agent search, site hosting, ads truth):
`Desktop/polsia-work/build/ui-reverse/wave-v24/v-settle/v24-live.sh` — **15/15 passed**.

## 8. Sources

DeepSeek Search queries (via `POST https://api.deepseek.com/v1/tools/search`): "Polsia AI platform
features website builder deployment God Mode reviews"; "Polsia AI pricing plans credits what
included"; "Polsia AI what can it do build websites apps deploy hosting"; "Polsia AI limitations
complaints not working reviews". DeepSeek Fetch (`/v1/tools/fetch`) pages: salesforge.ai Polsia
review (pricing/credits/20% take/IP), render.com "A sandbox doesn't constrain an API Key" (Render +
GitHub + Neon + MCP stack), polsia.com landing page. Screenshots: `docs/screenshots/` (dashboard,
agents, usage, capabilities, tasks-with-run-action) captured from the live stack.
