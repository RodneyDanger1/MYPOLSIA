# MERIDIAN — Backend / Orchestration Chapter

Scope: the FastAPI + Celery + agent-runtime backend in `backend/`. Every claim carries a `path:line`
reference; `app/...` means `backend/app/...`. Behavioural statements were read from the code that
enforces them (constants quoted with their line). Anything I could not verify is marked in §6.
No repository file was modified.

Naming note: the code's own identifiers are still `Polsia` (`BasePolsiaAgent`, `PolsiaError`,
`POLSIA_*` env names), while `Settings.app_name` reads `"Meridian"` (`app/config.py:18`) and a rename
sweep is in flight in the working tree. This chapter cites identifiers as they exist.

---

## 1. Agent logic and orchestration

### 1.1 The 9-agent roster

The dispatcher is `app/agents/crew_factory.py:55-65` — `AGENT_MAP`, `{agent_type: (module, class)}`,
resolved lazily via `importlib` (`:72-80`) so tests can patch a class method without importing
Tweepy/Stripe/PyGithub. `VALID_AGENT_TYPES` derives from it (`:69`); `task_service.py:56-66`
re-declares the same nine names, pinned equal by
`tests/unit/test_w4_capability_census.py::test_the_hand_typed_task_service_registry_equals_the_dispatcher_registry`.

| # | agent_type | Class (file:line) | Prompt role | `max_turns` | Schema | Registry tools | Classes | Channels |
|---|---|---|---|---|---|---|---|---|
| 1 | `orchestrator` | `OrchestratorAgent` (`orchestrator/agent.py:150-155`) | plan the day as a dependency-aware list (`:37-67`); evening close (`:69-85`) | 40 | `OrchestratorPlan` | `macro_snapshot`, `pricing_catalog`, `phone_read`, `signals_read`, `fx_convert` (`policy_firewall.py:181-182`) | READ, INTERNAL_WRITE | — |
| 2 | `business_planning` | `BusinessPlanningAgent` (`business_planning/agent.py:53-58`) | the few moves that change trajectory (`:26-29`) | 30 | `BusinessPlan` | `macro_snapshot`, `pricing_catalog`, `fx_convert` (`:200`) | READ, INTERNAL_WRITE | — |
| 3 | `competitor_research` | `CompetitorResearchAgent` (`competitor_research/agent.py:120-125`) | market intelligence with provenance (`:45-49`) | 60 | `CompetitorReport` | `web_fetch`, `web_search`, `research_fetch`, `signals_read`, `domain_health`, `link_status` (`:222-223`) | READ, INTERNAL_WRITE | `web` |
| 4 | `social_media` | `SocialMediaAgent` (`social_media/agent.py:97-102`) | the company's public voice (`:67`) | 30 | `SocialMediaOutput` | `signals_read` (`:247`) | READ, INTERNAL_WRITE, EXT_REVERSIBLE | twitter, linkedin, facebook, instagram, tiktok, youtube (`:245`) |
| 5 | `ads_management` | `AdsManagementAgent` (`ads_management/agent.py:128-133`) | paid acquisition from the AD METRICS block (`:32-40`) | 30 | `AdsOutput` | none (`:272-278`) | READ, INTERNAL_WRITE, EXT_REVERSIBLE | google_ads, meta_ads |
| 6 | `email_outreach` | `EmailOutreachAgent` (`email_outreach/agent.py:93-98`) | find and email fitting prospects (`:52`) | 30 | `EmailOutreachOutput` | `link_status` (`:256`) | READ, INTERNAL_WRITE, EXT_REVERSIBLE | `email` |
| 7 | `code_generation` | `CodeGenerationAgent` (`code_generation/agent.py:62-67`) | plan or ship one reviewable change (`:35-39`) | 80 | `CodeGenOutput` | `code_write`, `code_read`, `code_list`, `github_repo`, `github_branch`, `github_commit`, `github_pr` (`:331-332`) | all four | `github` |
| 8 | `customer_support` | `CustomerSupportAgent` (`customer_support/agent.py:182-187`) | read the inbox and reply (`:53`) | 30 | `CustomerSupportOutput` | `phone_read` (`:270`) | READ, INTERNAL_WRITE, EXT_REVERSIBLE | `email` |
| 9 | `finance` | `FinanceAgent` (`finance/agent.py:237-242`) | own revenue, spend, alerts (`:191`) | 30 | `FinanceOutput` | `macro_snapshot`, `pricing_catalog`, `fx_convert` (`:342`) | READ, INTERNAL_WRITE, EXT_IRREVERSIBLE | `stripe` |

Allowlists live in `policy_firewall.py:162-344` (`AGENT_CAPABILITIES`) in two namespaces:
`allowed_tools`/`denied_tools` are Claude-CLI permission-rule strings, `agent_tools` are registry
names (`:149-156`). Enforcement is at call time in `tool_registry.execute_tool`, not in prompt text
(`tool_registry.py:1219-1242`).

**Extra planner.** `CompanyPlannerAgent` (`company_planner/agent.py:72-77`, schema
`PlannerProposalDraft`, tools `macro_snapshot`+`pricing_catalog`, `policy_firewall.py:230-234`) is
deliberately **not** in `AGENT_MAP`: `planner_service.py:51,421` constructs it and calls
`agent.timed_run(...)` (`:429`). The plan schema therefore cannot target it
(`schemas/agent_outputs.py:107-113`), and the fleet map marks it
`[SERVICE-INVOKED — NOT A PLAN TARGET]` (`capability_manifest.py:59-60`).

**Extra deployment.** No class exists — only schemas (`DeploymentDraft`/`DeploymentOutput`,
`schemas/agent_outputs.py:590-599`). `crew_factory.py:67-69` records that adding it to `AGENT_MAP`
"would break `GET /agents/status == 9`"; a plan naming `deployment` fails validation and the morning
cycle skips that entry (`daily_cycle.py:962-978`).

### 1.2 How an agent runs (`BasePolsiaAgent`)

`app/agents/base_agent.py` (2,010 lines) owns the seam:

1. **Run context + evidence reset** — `timed_run` → `set_run_context` (`:1761-1773, 457-507`)
   publishes task title, image parts (≤8 MiB, PNG/JPEG/WebP), `run_id`, `prior_actions`, `attempt`;
   `_reset_run_evidence` (`:1038-1052`) clears the previous run's tool/repair record (run-scoped, like
   `RUN_TAINT`). Images are rebuilt only when the attachment set changed (`:493-496`).
2. **Context build** — `assemble_prompt` (`:806-850`) renders **nine fixed layers**
   (`context_assembler.py:81-91`): identity, tool_doctrine, company, business_state, yesterday
   (`pinned`, cacheable prefix) then memories, today_tasks, extra_context, task_brief (volatile).
   Budget from `settings.planner_context_token_budget` (12,000, `config.py:180`) via
   `budgets_for_total` + `CONTEXT_LAYER_WEIGHTS` (sum 25,800, `base_agent.py:102`); `measure_tokens`
   is a documented estimator (`context_assembler.py:48-59`); ceiling 160,000 (`:109`).
   NON-NEGOTIABLE RULES + the output contract lead the identity layer because `assemble()` truncates
   from the end (`base_agent.py:645-660`).
3. **Prompt** — `collect_structured` (`:1456-1476`): volatile suffix = user turn, stable prefix =
   `system_prompt_static`.
4. **Provider + tool loop** — `_real_call` (`:921-1035`) picks the provider once per logical run
   (`:937`) and opens one `RunCostGuard` per run so retries cannot re-spend the cap (`:985-988`).
5. **Retries** — in-run ladder `claude_cli.run_with_retries` (`claude_cli.py:901-935`), total attempts
   `1 + settings.agent_max_retries` (default 2 → 3, `config.py:136`), jittered backoff
   `base·2^n·U(0.5,1)` capped 30 s (`:889-898`). Transient subtypes/`api_error_status` retried;
   `E_CLI_MISSING` and envelope-shaped `E_CLI_TIMEOUT` (exit 143) are not (`:861-886`); a *raised*
   `E_CLI_TIMEOUT` is (`:924-930`).
6. **Deadline** — per call `claude_timeout_seconds` 300 s (`config.py:124`) intersected with a run-wide
   budget `max(1, min(soft,hard) − settle_reserve − ladder_backoff_total)` (`claude_cli.py:450-464`):
   with soft 840 / hard 900 (`config.py:50-51`), reserve 60 s and 4.5 s of ladder sleep, ≈775 s per
   logical run (arithmetic, not measured). A spent budget refuses **before spawning** (`:704-729`).
7. **Output validation** — envelope → schema with a bounded repair ladder (`base_agent.py:1335-1454`,
   attempts = `settings.claude_repair_attempts`, default 1); a repair re-prompt is refused by name
   when the exchange is not replayable (`:1191-1234`, `E_AGENT_BAD_OUTPUT`). Agents may override
   `repair_model_payload` for deterministic platform limits (X's 280 chars): once, in place, reported
   in `repaired_by` (`:535-557, 1436-1443`).
8. **Persistence + verdict** — the agent writes nothing; it returns a dict plus `RUN_META_KEYS`
   (`:110-131`), mapped onto `agent_runs` by `_apply_run_usage` (`agent_tasks.py:300-390`).
   Measured-or-absent: no envelope ⇒ token/cost keys omitted (`base_agent.py:1479-1534`); a failed run
   carries the same dict on its refusal (`:1536-1603`). `timed_run` ends with `apply_run_verdict`
   (`:1799-1801`) plus `audit_claimed_files`/`audit_claimed_lengths` (`:1781-1798`); a
   `status="refused"` payload raises `E_VALIDATION` so it is never stored `completed`
   (`crew_factory.py:394-403`).

**Memory.** Recall: the worker puts memories on `context["memories"]` (`base_agent.py:1912-1926`);
`layers_for` ranks ≤30, withholds rows tagged for another company (`SCOPE_FOREIGN`) and rows a copy
review marked as unsupportable claims, and states both counts at the **head** of the layer so a budget
cut cannot drop the disclosure (`:703-763`; `memory_service.py:76-151`). Write: `remember_run`
persists a run-derived entry (`memory_service.py:548`), folded in only for completed runs
(`agent_tasks.py:2822-2829`). Chroma is primary with a SQL `LIKE` fallback
(`memory_service.py:234-268`), so memory works without Chroma.

### 1.3 DeepSeek-first vs Claude-CLI fallback

| Fact | Evidence |
|---|---|
| Provider chosen per run by `deepseek_runtime.available()` | `base_agent.py:937`; `deepseek_runtime.py:323-325` |
| Key + storage | stored file → `DEEPSEEK_API_KEY` (`deepseek_runtime.py:309-320`); store = `backend/data/deepseek_api_key.json`, module-anchored, temp-file + `os.replace`, 0600 (`icacls` on Windows), never logged (`secret_store.py:14-33, 106`) |
| Endpoint / model | `POST {base}/chat/completions` on `https://api.deepseek.com`, `deepseek-flash`, `reasoning_effort="none"`, 300 s, `max_tokens` 8192 — `deepseek_runtime.py:121-140`, `config.py:256-271` |
| Tool loop | `deepseek_tool_loop_max_iterations` default 4, clamp 1..12; at the bound with tools pending the run refuses by name (`error_tool_loop`) — `deepseek_runtime.py:342-373, 1908-1930` |
| CLI path | `claude -p … --output-format json`, `permission_mode=dontAsk`, `bare=True`, `--max-turns`, `--max-budget-usd`; concurrency via `claude_max_concurrency` semaphore (`claude_cli.py:475-660, 676-690`); always a per-invocation `cwd` under the CLI workspace root, never the backend dir holding `.env`, with credential-shaped env names stripped (`:519-523, 755-761, 84-145`) |
| Mock flag | `CLAUDE_CLI_MOCK` truthy (env **or** settings) ⇒ **no subprocess**, read on every call; payload from `CLAUDE_CLI_MOCK_RESPONSE` (`base_agent.py:134-143, 518-526`). A non-schema mock result yields the schema's empty shape marked `status="partial"` with a note naming the mock (`:1355-1378`) |
| Images on CLI path | refused (`E_CONFIG`) rather than running blind — `:938-954` |
| Vision on DeepSeek | content parts, PNG/JPEG/WebP, ≤5 MiB each / ≤8 MiB total, re-validated — `deepseek_runtime.py:376-415` |

**Measured on this host:** `claude` is absent from PATH, no DeepSeek key file and no `secrets.json`
exist, and `.env` sets `DEEPSEEK_API_KEY=` empty, `CLAUDE_CLI_MOCK=false`, `SANDBOX_MODE=true` — so
every non-mock dispatch here would be refused at the gate (§6).

### 1.4 Tool registry — 17 tools

`app/agents/tool_registry.py` ships **17** function specs (`TOOL_SPECS`, `:423-903`), all with
`additionalProperties: false`. Declared arguments are **derived** from the specs (`:915-918`); an
undeclared argument is a named `E_VALIDATION` refusal, never a silent drop (`:928-960`). `TOOL_GUIDANCE`
renders one when-to-use line per tool into every agent's `tool_doctrine` layer
(`:335-421`; `base_agent.py:588-610`).

| Tool | Args | Class | Real effect |
|---|---|---|---|
| `web_fetch` | `url`, `max_chars` | READ | pinned GET, 15 s, 200 KiB, 8k excerpt (20k cap), ≤5 redirects, SSRF floor |
| `web_search` | `query`, `count`, `ori_question` | READ | DeepSeek Search API, ≤10 results, metered |
| `research_fetch` | `url`, `max_chars` | READ | DeepSeek Fetch, metered |
| `macro_snapshot`, `pricing_catalog` | — | READ | cached macro; 50-row dated catalog + staleness |
| `phone_read` | `thread_id`, `limit` | READ | Phone Link store; **no codes surface** (`:179-181`) |
| `fx_convert`, `signals_read` | `amount`,`base`,`quote` / `limit` | READ | ECB rates; fixed HN/TechCrunch headlines |
| `domain_health`, `link_status` | `host` / `url` | READ | DoH+TLS 443; HTTP status + Wayback, ports 80/443 |
| `code_read`, `code_list`, `github_repo` | `path` / `repo` | READ | workspace read/listing; repository metadata |
| `code_write` | `path`, `content` | INTERNAL_WRITE | UTF-8 write ≤256 KiB inside the workspace |
| `github_branch` | `repo`, `branch`, `from_branch`, `dry_run` | EXT_IRREVERSIBLE (READ without `from_branch`) | read/create ref; `dry_run` default true |
| `github_commit` | `repo`, `branch`, `message`, `files[]`, `dry_run` | EXT_IRREVERSIBLE (READ unless `dry_run:false`) | Git data API, `force:false` always |
| `github_pr` | `repo`, `title`, `head`, `base`, `body`, `dry_run` | EXT_IRREVERSIBLE (READ unless `dry_run:false`) | opens a PR, never merges |

Fail-closed properties: a name in neither class table is a named `E_CONFIG` and **is not executed**
(`:1169-1185`); `_tool_action_class` refines with the *same* predicate the runner uses (`:1102-1156`);
metered tools are capped at 10 calls **per run** (`:159-160, 268-331`); a result over 100,000 chars is
a named `E_BUDGET_EXCEEDED`, not a truncation (`:1084-1099`); `execute_tool` never raises — every
failure is a JSON refusal so the loop can end honestly (`:1034-1099`).

### 1.5 Orchestrator routing, delegation, DAG and cycles

* **Morning plan** — `daily_cycle._morning_cycle` (`:730`+) runs the orchestrator with
  `metadata={"cycle":"morning"}`, reads `plan["tasks"]` **directly**, creates each task with
  `dedupe_key(agent_type, plan_bucket(title))` + `source="orchestrator"` (`:947`); the agent truncates
  to `settings.planner_max_nodes` (12) (`orchestrator/agent.py:195-203`).
* **Evening summary** — same class, `EVENING_PROMPT`; `plan_mode` honours the declared cycle first,
  word-sniffing only as fallback (`orchestrator/agent.py:158-178`); discarded evening `tasks[]` are
  recorded in `notes` (`:221-230`).
* **Routing check before construction** — `dispatch_preflight` + `task_capability_problems` compare
  the task's declared `metadata.side_effect` to the routed agent's classes
  (`crew_factory.py:221-259, 262-307, 333-362`): scheduling "refund a customer" on
  `business_planning` is refused with **zero model calls** and a measured `$0.0000`.
* **Delegation** = task rows handed to the broker. Roots dispatch after commit; dependents are released
  by the worker's completion hook, which commits the release + `metadata.dag_released_at` **before**
  the hand-off (`agent_tasks.py:1813-1851`; `task_service.py:1879-1996`).
* **Title→id + cycles** — `depends_on` is titles, resolved to ids in a second pass with unresolved
  titles recorded (`daily_cycle.py:995-1018`). A model-authored **cycle** is found by reachability:
  unreachable nodes become roots with `metadata.depends_on_cycle` + `_members` and cleared edges
  (`:1020-1053`). Dependents of a terminal non-completed parent are failed by name with
  `metadata.dependency_blocked` (`agent_tasks.py:1888`+; `task_service.py:2015`+).
* **The unwired planner** — `orchestration/planner.py:1-14` has zero production callers; any claim
  that it materialises the morning plan is false.

### 1.6 Celery lifecycle: claim → lease → retry → reaper → settle

`celery_app/tasks/agent_tasks.py` (3,572 lines); body is `_execute_agent_task` (`:2150-…`).

| Stage | Behaviour | Evidence |
|---|---|---|
| Claim | `claim_task(..., takeover_stale=True)` first; live claim ⇒ `E_TASK_CLAIMED`, non-runnable ⇒ `E_TASK_NOT_RUNNABLE`; both `{"status":"skipped"}`, no side effects. Enqueue is `acks_late`, `prefetch=1`, `reject_on_worker_lost` | `agent_tasks.py:2182-2245`; `task_service.py:1080, 1210`; `celery_config.py:73-75` |
| Pause gate | owner pause ⇒ claim **released** to `pending`, attempts refunded, `paused_release` stamped, activity row | `agent_tasks.py:2247-2316` |
| Budget gate | `assert_budget_allows` at the one choke point; denial ⇒ `rejected` (not `failed`) + dependents failed; `source="manual"` runs past the cap, override recorded | `agent_tasks.py:2318-2400`; `_common.py:516-552` |
| Lease | 900 s; heartbeat every `max(5, lease/3)` = 300 s; a refused heartbeat latches the guard so a side effect is denied `E_RUN_CANCELLED` | `agent_tasks.py:2590-2625`; `policy_firewall.py:1216-1230` |
| Run row | `create_agent_run` committed **before** the model call; failure to create settles the claim | `agent_tasks.py:2532-2541` |
| Execution | `asyncio.to_thread(run_agent_for_task, …)` keeps the heartbeat alive; a parked row with a token replays the approved payload instead | `:2660-2686` |
| Ownership re-check | `heartbeat_task` before the outcome: ended by someone else ⇒ ledger kept, row untouched, `task_outcome_superseded`; not terminal ⇒ abandon, `task_lease_lost` | `:2706-2758` |
| Retry | `RETRYABLE_CODES = {E_CLI_TIMEOUT, E_CLI_EXIT, E_CLI_JSON, E_INTEGRATION, E_DB, E_REDIS}`; a refusal may declare `retryable=False`; countdown `min(600, base·2^(n−1))` (base 20 s); row requeued `pending` + `rearmed_at` | `:89-98, 252-297, 3058-3087` |
| Settle | `finish_agent_run` + `_apply_run_usage`; `failed` iff an error; `awaiting_approval` when gated actions were recorded; `payload_status` in task metadata | `:2760-2990`; `task_service.py:2398` |
| Reapers | stale claims (batch 200; requeue or `failed` at 3 attempts), stale runs (>2 h ⇒ `E_RUN_STALE`), stranded pending, paused re-arm, approvals >72 h | `task_service.py:1521-1648`; `maintenance.py:225-380, 738-1080, 901-1060` |
| Live accounting | run-scoped sink persists cumulative tokens/cost **during** the run, so a SIGKILLed worker still leaves measured spend | `agent_tasks.py:2644-2659`; `deepseek_runtime.py:1868-1892` |

### 1.7 Beat schedule — 17 entries, all off by default

`celery_app/beat_schedule.py:58-218` declares **17** entries; `celery_config.py:70` installs them.
Queues: `scheduler` (morning, evening, night, cadence), `agents` (social 2 h, inbox 3 h at :30,
follow-ups 3 h, ads/stripe 6 h, dunning hourly), `maintenance` (retention 03:00, workspace 04:15, lease
300 s, stranded `*/5`, stale runs `*/5`, paused re-arm `*/5`, publish health `*/15`, phone `*/5`).
`expires` is one full period (240–21600 s) on every entry, so a backlog collapses rather than replaying
stale ticks.

**Why off:** the beat *process* is opt-in. `docker-compose.yml:168-173` puts `celery_beat` behind
`profiles: ["scheduler"]` with `restart: "no"`; `:208-212` records the measured reason — a compose
recreate resurrected beat and "it dispatched scheduler work against a REAL model key with no owner
action", so arming is explicit (`docker compose --profile scheduler up -d celery_beat`). No entry is
deleted. Even when armed, three self-gate: both cycles refuse outside their local hour / after their
once-per-(cycle, local-day) claim (`daily_cycle.py:161-257`), the night task returns
`{"status":"off"}` when disabled (`night_task.py`), dunning returns `{"status":"off"}` outside its
hour (`beat_schedule.py:197-206`).

### 1.8 Task state machine

`task_service.TASK_TRANSITIONS` (`:87-113`) is the single matrix, with a provenance map naming the
writer of each edge (`:118-133`). States: `pending`, `in_progress`, `awaiting_approval`, `failed`,
`completed`, `cancelled`, `rejected` (`models/task.py:TaskStatus`). Terminal states have **no**
outgoing edge; the two shipped re-arms are `failed → pending` (manual retry) and
`awaiting_approval → pending` (`attach_approval_token`, `:958`). Every accepted transition writes
`task_status_changed` (`:136-139`). The pre-fix defect is in the docstring:
`PATCH /tasks/{id}/status` used to answer 200 for `completed → pending`. A worker whose row was ended
mid-run writes `task_outcome_superseded` and keeps the run's ledger row (`agent_tasks.py:2916-2945`).

---

## 2. Safety and control

| Control | Implementation | Evidence |
|---|---|---|
| **Sandbox** | `settings.sandbox_mode`, default **True** (`config.py:131`; `.env` true). `decide()` refuses every `external_*` action marked `sandbox: true`; agents record a labelled draft (`sandbox=true`, `drafted_not_sent=true`), never a fake id. Checked **before** caps so a draft is not turned into a run-killing denial | `policy_firewall.py:1282-1291`; `side_effects.py:1-16, 108-121`; inverse guard `:1507-1528` |
| **Approval tokens** | HMAC-SHA256 over `fingerprint\|exp\|jti\|issued_to`, 32-hex sig + 16-hex nonce, one dot on the wire; **single-use** via a UNIQUE insert into `consumed_approvals` in its own session (never the caller's transaction); TTL 3600 s; replay is its own control `POLSIA-E121`. Mint: `POST /api/v1/approvals` (owner key; optional `task_id` attaches, audits, requeues, dispatches; unset `APPROVAL_SECRET` ⇒ named `E_CONFIG`). Rotate: changing the secret invalidates every outstanding token by construction; the ledger stores only `sha256(body)` | `policy_firewall.py:846-889, 916-924, 947-1067`; `approvals.py:178-239, 102-175`; `models/approval.py:52`; migration `0018`; `test_r32_signature_rotation.py` |
| **What needs approval** | every EXTERNAL_IRREVERSIBLE action; **any** external action from a tainted run; `value_usd ≥ $25.00` compared in exact whole cents (HALF_UP; non-finite fails closed) | `policy_firewall.py:1109-1136, 1311-1344` |
| **Kill switch** | `POLSIA_KILL_SWITCH` env → `settings.kill_switch`; `freeze` refuses all dispatch and tool calls, `throttle` refuses agents with an external action class. Read at call time; direct-effect endpoints (`/phone/send`, deploy) call `require_external_effects_enabled` before transport | `policy_firewall.py:1412-1468`; `crew_factory.py:275-285`; `test_kill_switch_settings.py` |
| **Caps — channel** | day: email 50, twitter 12, github 6, google_ads 10, meta_ads 10, stripe 5, render 3, linkedin 4, facebook 3, instagram 5, tiktok 10, youtube 3; hour: email 15, twitter 3, github 2. A configured cap whose count is **unknown** is refused by name (`cap_unknown`, `POLSIA-E114`), never read as zero | `policy_firewall.py:720-753, 1139-1154, 1293-1309` |
| **Caps — budget** | day $25, per-run $2, $1 reserve; `allowance = min(remaining_day − reserve, remaining_agent, per_run_cap)`. In-run guard checks a provable worst case **before** each provider call and the measured spend after; a capped run is not retried | `accounting.py:368-433, 442-680`; `_common.py:516-552`; `deepseek_runtime.py:1800-1821` |
| **SSRF / egress** | resolve once via Cloudflare DoH (never the OS resolver), require every answer `is_global` and not multicast, allowlist ports 80/443, then dial the vetted address (no re-resolve); hand-walked redirects cannot change origin; `is_global` also catches RFC 6598 shared space | `tool_registry.py:1363-1432, 1462-1600`; `trust_service.py:380-414, 734+`; `test_a3_egress.py` |
| **Injection taint** | untrusted content wrapped in `<untrusted source content_id>`, forgery escaped, capped; heuristic cues set `RunTaint` on a contextvar; `close_quarantine` repairs a truncated fence so no opener stays unscanned; a tainted run escalates every external action and is **sticky** | `policy_firewall.py:394-462, 493-503`; `base_agent.py:330-385, 838-849`; `side_effects.py:38-87`; `test_taint_chain.py`, `test_w8_taint_sticky.py` |
| **Redaction / secrets** | DeepSeek key: atomic file, 0600 (icacls), masked status, scrubbed from probe output, never logged. Export redacts `approval_token`; chat redacts secrets before echo; provider creds come from the same store Settings writes; the access log scrubs `?key=`/`?api_key=` | `secret_store.py:14-77`; `data.py:276-386`; `chat_service.py:161`; `integrations.py:649-661`; `query_scrub.py:39-88` |
| **Audit ledger** | `activity_log` (`action` + `detail`) carries `approval_minted`/`approval_attached`, `task_completed`/`task_partial`/`task_failed`, `task_awaiting_approval`, `task_lease_lost`, `task_outcome_superseded`, `stale_claims_requeued`, `cycle_claimed`/`cycle_tick_skipped`, `dispatch_refused_paused`, `budget_manual_override`, `phone_send`. Plus `agent_runs`, `consumed_approvals`, `tasks.metadata.actions` | `models/report.py:14-31`; `agent_tasks.py:3112-3147`; `approvals.py:62-83` |

---

## 3. Integrations and call paths

Every adapter follows one contract: lazy import, credential check naming what is missing, bounded
timeout, `integration_call`, and an ambiguity class for mutations (`ProviderTransportError` →
`outcome_unknown`) instead of a definite "failed" (`integrations.py:105-118`). An unconfigured channel
refuses by name and never reports success — pinned by
`test_w9_capability_census_ii.py::test_an_unconfigured_channel_refuses_by_name_and_never_reports_success`.
Mutation adapters (email, post, refund, PR, deploy, SMS) are reached only when `decide()` allows the
action, so under `SANDBOX_MODE=true` the refusal happens before the adapter — "sandbox ⇒ labelled
draft" holds for every row below without restating it.

| Integration | Call path | Status | Credentials |
|---|---|---|---|
| **SendGrid send** | `send_email` → SDK (`:664-685, 867-995`); documented answer table maps 400/401/403/404/405/413/500 + 429/503/504 to `(class, retryable)`; provider id from `X-Message-Id` else recorded `absent` | **Real** | `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL` (secret store → env → Settings) |
| **SendGrid inbound** | `POST /support/inbox/inbound`: multipart parse, ECDSA over raw bytes (`X-Twilio-Email-Event-Webhook-Signature` + timestamp, 300 s) **or** constant-time OAuth bearer; id from `Message-ID` else a sha256 digest; dedupe `inbox:<id>` | **Real** (inbound read; writes one inbox row) | `SENDGRID_INBOUND_PUBLIC_KEY` **or** `_OAUTH_TOKEN` (+ `SUPPORT_INBOUND_SECRET`) |
| **Stripe money** | webhook `POST /finance/stripe/webhook` (**no API key**, `stripe-signature` — stdlib HMAC or SDK, any `v1=` for rotation, 0 ≠ off, dedupe in `stripe_events`); `refund_payment` → `refunds.create` (**`pi_` only**, `evt_…` refused pre-network, `max_network_retries=0`); `read_revenue_facts` returns measured revenue or `None` | **Real**; refund `approval-gated` | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` (both set) |
| **Render deploy** | `POST /sites/{slug}/deploy` → `deploy_site` (`deploy_service.py:517-594`): kill switch → measured `render` day cap (3) → `enforce(direct_caller="owner_render_deploy")` → **one** bounded POST (20 s) → 2xx with a deploy id is the only success; `dry_run=True` returns plan + fingerprint | **Real** | `RENDER_API_KEY`, `RENDER_SERVICE_ID` |
| **GitHub** | PR: `open_pull_request` → PyGithub `create_pull`, `retry=0`, explicit timeout (`:997-1030`). Registry: `github_service.py` over httpx, one credential path `secret_store.get_secret("GITHUB_TOKEN")`, `dry_run` default true, no force-push, no merge | **Real**; PR `approval-gated`, tools dry-run-first | `GITHUB_TOKEN` |
| **X/Twitter** | tweepy `create_tweet`, 20 s session timeout, replies via `in_reply_to_tweet_id`; **media is a named refusal** (`E_VALIDATION`, `:160-165`) | **Real, text only** | 4 twitter keys |
| **LinkedIn** | httpx `POST /rest/posts`, versioned | **Real** | `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN` |
| **Facebook** | Graph `v21.0/<page>/feed` | **Real** | `META_PAGE_ACCESS_TOKEN`, `META_FACEBOOK_PAGE_ID` |
| **Instagram** | Graph container + publish; **image required** | **Real** | `META_PAGE_ACCESS_TOKEN`, `META_INSTAGRAM_USER_ID` |
| **TikTok / YouTube** | Content Posting API `video/init/`; `videos.insert` (media URL passes the egress floor, ≤100 MB, 120 s) | **Real** | `TIKTOK_ACCESS_TOKEN` / `YOUTUBE_ACCESS_TOKEN` |
| **Ads — reads** | Google Ads 24.1.0 / facebook-business 21.0.0 reads → per-campaign rows, integer cents, `roas: None` when unmeasurable (`:1451-1505`) | **Real** | Google: `DEVELOPER_TOKEN`, `CLIENT_ID`, `REFRESH_TOKEN`, `CUSTOMER_ID`; Meta: `META_ADS_ACCESS_TOKEN`, `META_ADS_ACCOUNT_ID` |
| **Ads — budget mutation** | **always** a named refusal: "needs an account-scoped API client that this deployment does not ship; the recommendation was recorded instead" (`:1099-1133`) — even with every credential set | **Named refusal (not built)** | n/a |
| **Phone — read** | `phone_read` + `/phone/*` read the Phone Link SQLite store read-only (`PHONE_LINK_DB_DIR`, bind-mounted `/phone-link`); codes structurally absent | **Real, read-only** | none (a local store must exist) |
| **Phone — send** | `POST /phone/send` → kill switch → `sandbox_mode` → validation → driver → `create_subprocess_exec` (argv list, body in a private temp file, 45 s, child killed on timeout); only an observed `sent_at` is success. The sandbox refusal happens **before** the driver is probed | **Real, gated** | a paired `cdp-google-messages` driver, else `E_PHONE_SEND_UNPAIRED` |
| **DeepSeek** | chat completions (runs, vision), `/v1/tools/search`, DeepSeek Fetch | **Real** | stored key or `DEEPSEEK_API_KEY` |
| **Owner chat** | `POST /chat/message` → deterministic intent classification over the DB (dashboard/finance/runs/tasks/refusals), **no model call**; a confirm token gates task creation; sandbox note appended (`:675-681`) | **Real, not LLM-backed** (`chat_service.py:161-453, 836+`) | owner API key |
| **Deployment agent** | — | **Not built** (schema only; would need class + registry + tables + routes) | — |

---

## 4. Data model and APIs

### 4.1 Tables (26)

Twenty-six `__tablename__` values across `backend/app/models/`, all imported by
`models/__init__.py:1-59`. Purposes:

`company_config` = profile + settings (timezone, cycle hours, `dispatch_paused`, KPI bag); `tasks` =
queue rows (lifecycle status, metadata bag with side effect/dependency ids/approvals/action list,
dedupe key); `agent_runs` = per-execution ledger (status, duration, tokens, `cost_usd`, `usage_json`,
`tools_used`, `run_type`); `task_attachments` = image refs; `task_schedules` = owner cron prompts;
`chat_messages` = owner chat exchanges; `activity_log` = audit/feed ledger; `daily_reports` = plan +
summary per date; `social_posts`/`social_engagements` = published rows / mentions (cap counting
source); `email_*`/`prospects` = outbound lifecycle; `ad_*` = ads state/metrics; `stripe_events` /
`revenue_snapshots` / `expense_records` = money facts; `invoices` + dunning results = invoice
lifecycle + append-only ladder ledger; `memory_entries` = Chroma-linked insights with company scope;
`competitors` = **no writer** (`schemas/agent_outputs.py:163`: competitor data lands in
`agent_runs.output`); `support_inbox_items` = inbound mail; `consumed_approvals` = spent tokens;
`auto_mode_sessions` = bounded automation windows; `vision_descriptions` = cached image analyses.

### 4.2 API surface

**113 route decorators** across 36 modules in `backend/app/api/v1/` (counted by grepping
`@router.(get|post|put|patch|delete|websocket)`; `status_filters.py`/`totals.py` are helpers). Mounted
under `/api/v1` (`main.py:640-691`), plus `/ws/activity` outside the prefix (`:692`).

| Domain | Module (routes) | Key routes |
|---|---|---|
| Health | `health.py` (2) | `GET /health` (constant, **no auth**), `GET /ready` (real `SELECT 1`) |
| Tasks | `tasks.py` (7) + attachments | list/create/get, `PATCH /{id}/status`, attachments |
| Agents / chat | `agents.py` (2), `chat.py` (3) | `GET /agents/status`, `POST /agents/{type}/trigger`; message/confirm/history |
| Config / dashboard | `config.py` (7), `dashboard.py` (4) | config get/put, DeepSeek key get/put/delete; summary/KPIs/report-cycle |
| Finance | `finance.py` (6) | summary, revenue, expenses, events, `POST /finance/stripe/webhook` (**no API key**), planning |
| Social / outreach / ads | `social.py` (1), `outreach.py` (2), `prospects.py` (5), `ads.py` (2) | reads + owner actions |
| Support | `support.py` (3) | `POST /support/inbox`, `POST /support/inbox/inbound` (**secret/ECDSA/OAuth**), `GET` |
| Runs / ops | `runs.py` (3), `ops.py` (8) | ledger, `GET /runs/export`, capabilities, sandbox state, event stats |
| Approvals / channels / auto-mode | `approvals.py` (1), `channels.py` (1), `auto_mode.py` (3) | mint; counters+caps; start/status/stop |
| Phone / sites | `phone.py` (8), `sites.py` (6), `site.py` (2) | reads, `POST /phone/send`, media; site CRUD, deploy POST/GET, package |
| Planner / invoices / schedules | `planner.py` (3), `invoices.py` (5), `schedules.py` (4) | propose/confirm/status; CRUD+send/void; owner schedules |
| Data | `data.py` (2) | `GET /data/export` (ZIP + manifest), `DELETE /data` (guarded wipe) |
| Context | `economics.py`, `pricing.py`, `signals.py`, `trust.py` (2), `memory.py` (3), `artifacts.py` (4), `notifications.py`, `integrations.py` (5), `vision.py` (2) | macro, catalog, feeds, DNS/TLS/Wayback, memory, artifacts, notifications, masked integration status |
| WebSocket | `ws.py` (2) | `POST /ws/ticket` (HMAC, 30 s, 30 mints/60 s, 32 sockets/operator), `WS /ws/activity` |

**Auth.** One owner credential: `X-API-Key`, compared with `secrets.compare_digest` on **bytes** (a
non-ASCII header is simply wrong, never a 500). Empty configured key ⇒ open, and a blank header counts
as no credential; a non-blank wrong key is 401 (`core/security.py:12-49`). Exceptions: `GET /health`,
the Stripe webhook (signature), the inbound-email route (shared secret / ECDSA / OAuth), and the
WebSocket (ticket; an API key in the query string is refused and scrubbed from the access log —
`core/query_scrub.py:39-88`). Refusals are `{code, message, how_to_fix, context}` +
`X-Polsia-Error-Code` (`api/deps.py:59-70`); 29 named `E_*` codes plus a local
`E_PHONE_SEND_UNPAIRED` (`phone_send_service.py:73`).

### 4.3 Migrations, limits, censuses

* **Migration head `0020`** (`alembic/versions/0020_email_sent_requires_provider_evidence.py`); linear
  `0001 → 0020`, no branches. PostgreSQL-only semantics (`FOR UPDATE SKIP LOCKED`, CHECKs,
  append-only trigger, `Numeric` rendering) are environment-only and skip with a printed reason on
  SQLite (per `AGENTS.md`).
* **Limits:** prompt 12,000 tokens (weights 25,800; ceiling 160,000); tool result 100,000 chars; tool
  loop 4 (max 12); metered tools 10/run; web_fetch 15 s / 200 KiB / 8k–20k chars / 5 redirects;
  web_search 30 s, count ≤10; code files ≤256 KiB; attachments ≤5 MiB each, 4/task, 8 MiB total;
  phone_read ≤10 threads/messages; `planner_max_nodes` 12; `report_date` unique; approval park 72 h.
* **Censuses (measured this pass):** 9 dispatchable agents; 17 tools; 26 tables; 113 routes; 17 beat
  entries; 29+1 error codes; 9 context layers.
* **Exports:** `GET /data/export` streams a ZIP of per-table CSVs + a manifest whose row counts are
  declared as exported, `approval_token` replaced by `[redacted: credential]`
  (`data.py:276-386, 456-556, 605`); `GET /runs/export` (`runs.py:351`);
  `GET /site/package/download` (`site.py:32`).

---

## 5. Capability ledger

### PROVABLY CAN

1. **Dispatch/run 9 agent types** through one lazy registry with per-agent allowlists and action
   classes — `crew_factory.py:55-80`, `policy_firewall.py:162-344`; `test_w4_capability_census.py`.
2. **Refuse by name before spending**: wrong action class for the routed agent, frozen/throttled fleet,
   missing/unauthorized CLI, paused company, exhausted budget — `crew_factory.py:333-362`;
   `agent_tasks.py:2318-2400`.
3. **Schema-validated output** with a bounded repair ladder and a refusal instead of a half-object —
   `base_agent.py:1335-1454`; `test_structured_output_branches.py`.
4. **Call 17 real tools** with declared args, execution-time allowlisting, undeclared-argument refusal,
   per-run metering, hard result ceiling — `tool_registry.py:423-1310`.
5. **Fetch the web safely** (DoH + `is_global` + port allowlist + pinned dial + no cross-origin
   redirect) — `trust_service.py:380-414, 734+`; `test_a3_egress.py`, `test_b1_egress_adversary.py`.
6. **Enforce money limits** at dispatch and mid-run in dollars, measured-or-unknown —
   `accounting.py:403-433, 552-680`; `test_budget_gate.py`, `test_w2_deepseek_cost_guard.py`.
7. **Lease-based queue**: atomic claim, 300 s heartbeat, expired-claim takeover, bounded sweeps, retry
   ladder, supersession-safe settlement — `agent_tasks.py:2182-2316, 2590-2758, 3058-3087`;
   `test_w5_logic_state_machine.py`, `test_w8_reaper_windows.py`.
8. **Morning plan with real DAG edges** (title→id, roots-only, release on completion, blocked
   dependents failed by name, cycles detected and released) — `daily_cycle.py:947-1063`;
   `task_service.py:1879-1996`; `test_w3_dag_release.py`.
9. **Single-use, fingerprint-scoped approvals** minted over HTTP and attached to a parked row —
   `policy_firewall.py:846-1067`; `approvals.py:178-239`; `test_w2_sentinel_token_single_use.py`.
10. **Escalate tainted runs** (sticky, never de-escalatable) — `policy_firewall.py:1316-1344`;
    `side_effects.py:38-87`; `test_taint_chain.py`.
11. **Send email, receive inbound mail, verify Stripe webhooks, refund `pi_` payments, open PRs, deploy
    to Render, post to six platforms** — real adapters, one credential resolver, named refusals when
    unconfigured (`integrations.py`, `deploy_service.py`, `support_inbox_service.py`).12. **Truthful ledger** (tokens/cost/session/tools per run; 26 tables at head 0020; CSV/ZIP export with
    declared counts and redaction) — `agent_tasks.py:300-390`, `data.py:456-556`.
13. **Live activity feed** over an authenticated WS with tickets, per-operator caps, close codes —
    `ws.py:82-96, 316-369`.

### PROVABLY CANNOT (named refusals or absent code)

| Cannot | Why / evidence | Needs |
|---|---|---|
| Mutate a live ad budget | hard-coded named refusal even when configured — `integrations.py:1127-1133` | an account-scoped ad API client (Google Ads OAuth client + customer id, Meta Marketing app) and a real mutation method |
| Post media to X | named refusal — `integrations.py:160-165` | v1.1 media upload + `media_ids` |
| Thread/reply off X | named refusal — `:635-643` | per-platform reply parameters |
| Run a "deployment" agent | no class/registry/tables/routes — `crew_factory.py:67-69` | class + map entry + tables + routes + tests |
| Let the DAG planner schedule the cycle | zero production callers — `orchestration/planner.py:1-14` | an owner decision to wire or retire it |
| Answer chat with a model | deterministic routing over the DB — `chat_service.py:161-453` | a chat runtime plus its own policy and cost story |
| Any external effect while `SANDBOX_MODE=true` (the default, and this `.env`) | `policy_firewall.py:1282-1291`; `config.py:131` | `SANDBOX_MODE=false` **and** provider credentials |
| Run an agent here with neither key nor `claude` | measured: no key file, empty `DEEPSEEK_API_KEY`, `claude` absent | save a key via `PUT /config/deepseek-key`, or install/authenticate the CLI |
| Verify a cap it cannot count | `cap_unknown` (`POLSIA-E114`) instead of a fabricated zero — `policy_firewall.py:1139-1154` | a counting table for that channel |
| Send SMS without a paired driver | `E_PHONE_SEND_UNPAIRED` / `E_PHONE_UNAVAILABLE` — `phone_send_service.py:1-90` | a paired `cdp-google-messages` driver |
| Enforce a global hourly budget cap | no such policy (`RateCaps` per-channel hourly only; `BudgetPolicy` day/per-run) | a policy field + gate + tests |
| Merge a PR or force-push | not expressible (`force` refused; `github_pr` never merges) — `tool_registry.py:68-79` | a new tool and approval class |
| Receive SendGrid mail unverified | `E_CONFIG` for every caller when unconfigured — `support_inbox_service.py:1151-1310` | a public key or OAuth token |

### Credential checklist for the missing abilities

The CANNOT table's "Needs" column plus §3's credential column is the checklist; three environment facts
gate the rest: a live run needs a DeepSeek key (`PUT /config/deepseek-key`) **or** `claude` on PATH and
logged in; any external effect needs `SANDBOX_MODE=false` and `POLSIA_KILL_SWITCH` empty/`run`; periodic
work needs beat armed.

---

## 6. Verification status

| Claim class | Status |
|---|---|
| Roster, classes, prompts, schemas, allowlists, channels, tools, action classes, fail-closed rules | **Read from code** (each row cites its line; tool counts derived from `TOOL_SPECS`) |
| 113 routes / 26 tables / head 0020 / 17 beat entries / 29+1 codes | **Counted by tool over source** (grep, `__tablename__`, revision chain); no test run was needed or performed |
| `claude` absent, no key file, `SANDBOX_MODE=true`, Stripe/approval secrets set | **Measured on this host** by read-only probes (`Get-Command claude`, file existence, `.env` name enumeration, values masked) |
| Run budget ≈775 s | **Arithmetic** from `claude_cli.py:450-464`; not measured |
| Test suite green / counts | **NOT VERIFIED** — no pytest run. Named tests are the files that pin each behaviour; their passing state is the repo's claim, not this chapter's measurement |
| Live provider behaviour | **NOT VERIFIED** — no credential used, no external call made; statements describe adapter code and credential gates |
| PostgreSQL-only semantics | **NOT VERIFIED on SQLite**; documented as environment-only in `AGENTS.md` |
