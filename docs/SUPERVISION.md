# Agent supervision policy — the four tiers and where each is enforced

S1's four supervision tiers (S1 implication #7, `u6-research/smallbiz-playbook/PLAYBOOK.md:166`):
**auto** (drafts/internal) · **review** (outbound comms, listings) · **approve** (money out,
contracts, pricing, public statements) · **human-only** (irreversible, regulated).  This is the
shipped map — one line per tier naming the enforcing code path.

**Citations are symbols, not line numbers.**  Each anchor below names a module and a
`def`/`class` inside it (the form `app/agents/policy_firewall.py::decide`);
`backend/tests/unit/test_b2_truth.py` resolves every one against the tree, so an anchor that stops
existing fails the gate instead of silently pointing at unrelated code.  The previous revision
carried line numbers dated `2026-10-14 (base commit 5cf3feb)`; twelve of sixteen had rotted onto
blank lines or unrelated functions.

- **auto — drafts and internal work inside a bounded autonomous window.** The window is the Auto
  Mode session (`app/services/auto_mode_service.py::start_session` — `ends_at = now + duration`,
  duration validated 1 h … 5 d by `::validate_duration_hours`, one live window; per-window counters
  at `::increment_tasks_run`) or the night-task lane
  (`celery_app/tasks/night_task.py::run_night_task` — a self-gating tick that drains at most
  `night_task_limit` rows, OFF unless `night_task_enabled`); inside a window the firewall's allow
  verdict is the final `PolicyDecision(Decision.ALLOW, …)` in `app/agents/policy_firewall.py::decide`,
  and while `SANDBOX_MODE=true` every external action is recorded as a labelled draft instead of
  sent (the `external and sandbox_mode` leg of `::decide`).
- **review — outbound comms and listings, bounded by the per-channel send caps.**  The day and hour
  cap DENYs live in `app/agents/policy_firewall.py::decide` and surface as `E_RATE_LIMITED`
  (`POLSIA-E117`) through `::enforce`; a configured cap whose counter has no local source refuses by
  name (`cap_unknown`, `POLSIA-E114`) via `app/agents/policy_firewall.py::_cap_count_unknown`;
  counters come from the shipped send tables via `app/services/channel_service.py`
  (`::channel_caps`, `::remaining_under_cap`) and are readable at `GET /api/v1/channels/caps`
  (`app/api/v1/channels.py::channel_caps`).
- **approve — money out, contracts, pricing, public statements.** The firewall escalates to
  `REQUIRE_APPROVAL` for irreversible actions, tainted run contexts and value ≥ $25 — the three
  `needs_approval` legs of `app/agents/policy_firewall.py::decide`; the worker parks the task as an
  `awaiting_approval` row (`celery_app/tasks/agent_tasks.py::run_agent_task`, which also writes the
  owner notification), and the owner mints a single-use HMAC approval token via
  `POST /api/v1/approvals` (`app/api/v1/approvals.py::issue_approval_endpoint` → the firewall's
  `app/agents/policy_firewall.py::issue_approval`), which `decide()` re-verifies through
  `app/agents/policy_firewall.py::verify_approval` before the gated action may proceed.
- **human-only — irreversible/regulated actions no automated path may perform unattended.**  With no
  approval channel configured, the same approval branch fails CLOSED to a hard DENY
  (`app/agents/policy_firewall.py::decide`); `enforce()` raises the named `E_POLICY_DENIED`
  (`::enforce`); and a frozen fleet refuses dispatch outright
  (`app/agents/crew_factory.py::dispatch_preflight`).

Honest gaps (recorded, not hidden): the research-fetch budget is recorded post-hoc only (tier
(a), code-gap #11 — no tool-call proxy exists, so an over-budget run writes an activity row and
is never blocked); output-quality "review" is owner-driven today (the sampled-review loop is
S1 #8, shipped as the V9 B2 agent-quality surface); and the tool-allowlist leg of `decide()`
(the `req.tool` branch of `app/agents/policy_firewall.py::decide`) DOES have a production caller:
`app/agents/tool_registry.py::execute_tool` reaches `decide()` through
`app/agents/tool_registry.py::_execute` for every registry tool call, so the leg is enforced at
execution time — the earlier claim here that nothing in production called it was stale.
The CLI subprocess argv gates and the registry leg above are the two enforcement points inside a
run.
