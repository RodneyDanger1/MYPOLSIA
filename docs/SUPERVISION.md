# Agent supervision policy — the four tiers and where each is enforced

S1's four supervision tiers (S1 implication #7, `u6-research/smallbiz-playbook/PLAYBOOK.md:166`):
**auto** (drafts/internal) · **review** (outbound comms, listings) · **approve** (money out,
contracts, pricing, public statements) · **human-only** (irreversible, regulated).  This is the
shipped map — one line per tier naming the enforcing code path, as of 2026-10-14 (base commit
`5cf3feb`).  Docs-only: this file changes no behaviour.

- **auto — drafts and internal work inside a bounded autonomous window.** The window is the Auto
  Mode session (`app/services/auto_mode_service.py:253` — `ends_at = now + duration`, validated
  1 h … 5 d, one live window; counters at `:313`) or the night-task lane
  (`celery_app/tasks/night_task.py:62` — a self-gating tick that drains at most
  `night_task_limit` rows, OFF unless `night_task_enabled`); inside a window the firewall's allow
  verdict is `app/agents/policy_firewall.py:682`, and while `SANDBOX_MODE=true` every external
  action is recorded as a labelled draft instead of sent (`policy_firewall.py:623`).
- **review — outbound comms and listings, bounded by the per-channel send caps.**
  `app/agents/policy_firewall.py:629` (day cap) and `:638` (hour cap) DENY with
  `E_RATE_LIMITED` (`POLSIA-E117`) once a cap is reached, and a configured cap whose counter has
  no local source refuses by name (`cap_unknown`, `POLSIA-E114` — `policy_firewall.py:520`);
  counters come from the shipped send tables via `app/services/channel_service.py` and are
  readable at `GET /api/v1/channels/caps` (`app/api/v1/channels.py:84`).
- **approve — money out, contracts, pricing, public statements.** The firewall escalates to
  `REQUIRE_APPROVAL` for irreversible actions (`app/agents/policy_firewall.py:649`), tainted run
  contexts (`:652`) and value ≥ $25 (`:665`); the worker parks the task as an `awaiting_approval`
  row (`celery_app/tasks/agent_tasks.py:719`, owner notification at `:844`), and the owner mints
  a single-use HMAC approval token via `POST /api/v1/approvals` (`app/api/v1/approvals.py:103`
  → `issue_approval`, `policy_firewall.py:425`), which `decide()` verifies at
  `policy_firewall.py:676` before the gated action may proceed.
- **human-only — irreversible/regulated actions no automated path may perform unattended.**
  With no approval channel configured, the same approval branch fails CLOSED to a hard DENY
  (`app/agents/policy_firewall.py:668`); `enforce()` raises the named `E_POLICY_DENIED`
  (`policy_firewall.py:800`); and a frozen fleet refuses dispatch outright
  (`app/agents/crew_factory.py:213`, `dispatch_preflight`).

Honest gaps (recorded, not hidden): the research-fetch budget is recorded post-hoc only (tier
(a), code-gap #11 — no tool-call proxy exists, so an over-budget run writes an activity row and
is never blocked); output-quality "review" is owner-driven today (the sampled-review loop is
S1 #8, shipped as the V9 B2 agent-quality surface); and the tool-allowlist leg of `decide()` has
no production caller yet (`policy_firewall.py:548` docstring note) — the CLI subprocess argv
gates and the DeepSeek runtime's no-tools wire enforce inside the run.
