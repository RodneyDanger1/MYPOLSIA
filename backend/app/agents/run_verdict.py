"""F-07 — the ONE place a run's own verdict is enforced on its payload text.

Live defect (visuals, 2026-10-28): 8 of the trial's 10 tasks carried `status: completed` while the
run's own text said "status partial" or that no deliverable was produced — "the badge, the ledger
row and the dashboard card all say COMPLETED".  Wave 1 added the label at ONE seam
(`crew_factory.run_agent_for_task`); the planner's two agent seams call `agent.timed_run(...)`
DIRECTLY (`app/services/planner_service.py`), so a partial or refused planner run still reached its
consumer with clean text and no verdict at all.  This module is the rule, and `timed_run` — the seam
every run passes through — is where it is applied, so no call site can bypass it.

THE RULE (honest minimal version; the prose question is decided, not deferred):

  * `AgentEnvelope.status` is the ONLY authority for a run's verdict.  There is NO prose scan:
    the model's free text is never read to guess a status.  Measured reason: the caveat channel
    (`notes`) is used on healthy runs too (the trial's `status: ok` runs each carried 3-7 honest
    caveats — "KPIs are pre-revenue placeholders", "no channel-level CAC breakdown exists"), so a
    keyword rule would relabel genuine completions `[partial]` and destroy the signal with a
    fabricated verdict.
  * `refused` -> a NAMED refusal (`E_VALIDATION`, terminal: the task is recorded FAILED, never
    completed).  The refusal carries the run's own reason, notes, measured usage and verdict, so
    the failure text a human reads is the run's own excuse rather than a generic error.
  * `partial` -> the verdict rides the payload as structured data (`payload_status` + `verdict`)
    AND in the text the owner reads: `summary` is prefixed `[partial] ` (once), which the worker
    copies verbatim into `tasks.result_summary` and the activity feed.
  * `ok` -> untouched (no label is invented for work the run declares complete).

The BADGE/status-vocabulary half (`TaskStatus` has no `partial`; the worker logs a partial run as a
`task_completed`/`success` activity row) belongs to `task_service.py` / `celery_app/tasks/**` — see
the lane's REQUESTS, never edited here.
"""
from __future__ import annotations

from typing import Any

#: The result key carrying the run's own `AgentEnvelope.status` at the TOP level of the stored
#: output, so a consumer never has to parse prose to find it (F-07).
PAYLOAD_STATUS_KEY = "payload_status"

#: The visible label a PARTIAL run's summary carries into `tasks.result_summary` (F-07).
PARTIAL_SUMMARY_PREFIX = "[partial] "

#: The structured verdict record: `{status, label, reason, source}`.  It is the machine-readable
#: twin of the visible label, so a consumer that renders the payload (not just the summary) cannot
#: present a partial run as a clean success.
VERDICT_KEY = "verdict"

#: The statuses that are NOT a clean completion.  `ok` (and an absent status) needs no label.
NON_OK_STATUSES = ("partial", "refused")


def verdict_label(status: str) -> str:
    """The visible label for a non-ok status (`[partial] ` / `[refused] `), or ""."""
    return f"[{status}] " if status in NON_OK_STATUSES else ""


def verdict_reason(result: dict[str, Any]) -> str:
    """The honest reason a VERDICT gives: its `notes`, else its `summary` (bounded)."""
    notes = [str(note).strip() for note in (result.get("notes") or []) if str(note).strip()]
    if notes:
        return "; ".join(notes)[:1000]
    return str(result.get("summary") or "").strip()[:1000] or "no reason given"


def verdict_record(status: str, result: dict[str, Any]) -> dict[str, Any]:
    """The structured verdict a payload carries for a non-ok status."""
    return {
        "status": status,
        "label": verdict_label(status).strip(),
        "reason": verdict_reason(result),
        # Where the verdict came from: the run's own output contract, never a text heuristic.
        "source": "agent_envelope.status",
    }


def _label_summary_once(result: dict[str, Any], status: str) -> None:
    """Prefix `summary` with the verdict label exactly once (idempotent by construction).

    Deliberately NOT applied under the sanctioned `CLAUDE_CLI_MOCK` seam: that path's summary is
    the mock fixture's own text ("Mock Claude response for testing"), pinned verbatim by the
    queue-safety tests, and the placeholder payload already carries its "nothing was performed"
    note.  No product run is ever mock-shaped — the structured `payload_status`/`verdict` record
    is still written, so a mocked run is never unmarked in the ledger.
    """
    label = verdict_label(status)
    if not label:
        return
    from app.agents.base_agent import is_mock_mode

    if is_mock_mode():
        return
    summary = str(result.get("summary") or "").strip()
    if summary.startswith(label.strip()):
        return
    result["summary"] = f"{label}{summary}" if summary else label.strip()


def apply_run_verdict(agent_type: str, result: dict[str, Any]) -> dict[str, Any]:
    """Enforce the run's own verdict on its payload; returns the (mutated) payload.

    Called by `BaseMeridianAgent.timed_run` — the ONE seam every agent run passes through, including
    the planner's direct `timed_run` calls — and idempotently again by
    `crew_factory.run_agent_for_task`, so a future dispatch site cannot skip the rule.

    `refused` raises the named `E_VALIDATION` refusal (see `_raise_refused_task`); `partial` labels
    the payload and returns it; anything else is untouched.
    """
    if not isinstance(result, dict):
        return result
    status = result.get("status")
    if not isinstance(status, str) or status not in NON_OK_STATUSES:
        return result
    result[PAYLOAD_STATUS_KEY] = status
    result[VERDICT_KEY] = verdict_record(status, result)
    if status == "refused":
        _raise_refused_task(agent_type, result)
    _label_summary_once(result, status)
    return result


def _raise_refused_task(agent_type: str, result: dict[str, Any]) -> None:
    """Raise the NAMED refusal when the run's own verdict is `status="refused"` (F2-FEED-1).

    `AgentEnvelope` documents `refused` as "a policy outcome, never as success", but nothing on
    the completion path read it.  The measured usage/cost this run really incurred rides the
    refusal's `context` (`usage`), the same carrier `base_agent.usage_from_outcome` reads for a
    failed run, so a refused run does not vanish from the ledger's accounting.
    """
    from app.agents.base_agent import RUN_META_KEYS
    from app.core.errors import E_VALIDATION, MeridianError

    reason = verdict_reason(result)
    measured = {key: result[key] for key in RUN_META_KEYS if key in result}
    raise MeridianError(
        E_VALIDATION,
        f"agent {agent_type} refused its task: {reason}",
        how_to_fix=(
            "the run could not do this task honestly (its notes name why). Rewrite the task "
            "instruction or supply the missing input; do not re-run the identical prompt. The "
            "run's RUNTIME STATUS block names the deployment facts this worker knows."
        ),
        context={
            "agent_type": agent_type,
            "refused": True,
            PAYLOAD_STATUS_KEY: "refused",
            VERDICT_KEY: result.get(VERDICT_KEY) or verdict_record("refused", result),
            "summary": str(result.get("summary") or "")[:1000],
            "notes": [str(note) for note in (result.get("notes") or [])][:20],
            "usage": measured,
        },
    )


__all__ = [
    "NON_OK_STATUSES",
    "PARTIAL_SUMMARY_PREFIX",
    "PAYLOAD_STATUS_KEY",
    "VERDICT_KEY",
    "apply_run_verdict",
    "verdict_label",
    "verdict_reason",
    "verdict_record",
]
