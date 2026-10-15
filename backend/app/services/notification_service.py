"""Owner email notifications for terminal task outcomes (U5/P5 — audit aid1 gap #6).

The bell (B1) covers in-app surfacing; this is the EMAIL leg: when a task reaches a
terminal outcome, tell the owner — if, and only if, the owner configured it.

Contract (frozen for wave U5 — `build/ui-reverse/wave-u5/WAVE-PLAN.md`):

  * `notify_owner_for_terminal_outcome(db, task_id, outcome)` NEVER raises: a notification
    failure must not be able to fail a task run. Every failure path is wrapped, recorded as
    ONE `owner_notification_failed` activity row naming the failure (the
    `run_artifacts_not_persisted` pattern, `celery_app/tasks/agent_tasks.py:643-661`), and
    then the call returns normally.
  * It is a silent NO-OP unless `company_config.notification_email` is set AND the toggle
    matching the outcome is on. Unconfigured is a NAMED state (Settings owns it; no
    settings UI ships this wave), never a half-send.
  * Outcomes arrive as exactly `completed` | `failed` | `awaiting_approval` | `requeued`.
    `requeued` was WIRED in V8/C5 (owner decision D4): the worker's requeue branch
    (`agent_tasks._execute_agent_task`) routes THIS outcome — instead of `failed` — for an
    attempt that was put back in the queue, so `notify_on_run_requeued` governs it and
    `notify_on_failed` must NOT also fire for that attempt (one leg, one outcome per
    attempt — the double-email edge). Anything else is a silent no-op: no send, no error,
    not even a config read.
  * `notify_owner_cycle_report(db, kind=..., report_date=..., summary=...)` (V8/C5) rides the
    same never-raising discipline for the daily cycle's report email: the report's own
    transaction has already committed and an email problem must never fail the cycle. Its
    unconfigured state is deliberately NOT silent — it records ONE named
    `cycle_report_email_skipped` row, because a scheduled report that never arrives must be
    explainable.

Both owner-mail egresses route through the SHARED side-effect gate first
(`_firewall_clearance` -> `request_side_effect`, email channel + the REAL `channel_service`
counts — the same gate the invoice + outreach paths run): under `SANDBOX_MODE=true` (shipped
default) the mail is refused BY NAME (`E_SANDBOX_BLOCKED`, recorded as a labelled draft — never
a silent skip) and the provider is never reached; with the sandbox off the send rides the
existing `integrations.send_email` adapter with an explicit recipient
(`app/agents/integrations.py:78`), whose refusal semantics are already honest: an
unconfigured SendGrid returns a named `E_INTEGRATION` refusal, never a fake success.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ERROR_HINTS, PolsiaError
from app.models.task import Task
from app.services import company_service
from app.services.activity_service import log_activity
from app.services.query_helpers import fetch_scalar

logger = logging.getLogger(__name__)

#: The firewall slot owner mail is gated under (D2/V12-F2): the same email-channel capability
#: the invoice firewalled path sends on (`invoice_service._SEND_AGENT_SLOT`).  The activity
#: rows keep naming "orchestrator" as the real producer — the ledger-vs-slot split that path
#: documents.
_SEND_AGENT_SLOT = "email_outreach"


def _draft_error(outcome: Any) -> dict[str, Any]:
    """The named refusal for a sandboxed (or approval-required) send, carrying the house draft
    markers (`sandbox` / `drafted_not_sent`) so a recorded failure can never read as a send."""
    code = outcome.e_code or "E_POLICY_DENIED"
    return {
        "code": code,
        "message": outcome.reason or "external action recorded as a labelled draft; nothing sent",
        "how_to_fix": ERROR_HINTS.get(code, ""),
        **outcome.draft_flags(),
    }


async def _firewall_clearance(db: AsyncSession, *, action: str, payload: dict[str, Any]) -> None:
    """Ask the SHARED side-effect gate before any owner mail leaves this module (D2/V12-F2).

    This module used to call `integrations.send_email` directly, so under `SANDBOX_MODE=true`
    (the shipped default) a configured install put REAL mail on the wire and skipped the email
    caps.  The gate is the same one the invoice + outreach egresses run: `request_side_effect`
    with the email-channel capability and the REAL `channel_service` counts (the `RateCaps`
    defaults: email 50/day, 15/h).

      * sandboxed -> raise `_NotificationFailure` carrying the named refusal
        (E_SANDBOX_BLOCKED) plus the house draft markers, so the funnel records a NAMED row —
        a labelled draft, never a silent skip;
      * a policy/cap denial -> `enforce` raises its own named `PolsiaError` (E_RATE_LIMITED, or
        the cap-unknown posture) — the funnel records it by code, the provider stays untouched;
      * allowed -> returns None; the caller may send.
    """
    # Lazy imports on purpose (the module rule further down): importing this module must never
    # drag the provider libraries — or the firewall graph — into an import chain that does not
    # send.
    from app.agents.policy_firewall import ActionClass
    from app.agents.side_effects import request_side_effect
    from app.services import channel_service

    outcome = request_side_effect(
        _SEND_AGENT_SLOT,
        ActionClass.EXTERNAL_REVERSIBLE,
        "email",
        action=action,
        payload=payload,
        sends_today=await channel_service.channel_sends_today(db, "email"),
        sends_this_hour=await channel_service.channel_sends_this_hour(db, "email"),
    )
    if not outcome.allowed:
        raise _NotificationFailure(_draft_error(outcome))

#: outcome -> (matching company_config toggle column, human label). The toggle names are the
#: FROZEN P3 column names — a typo here is a silent no-op forever, so tests pin the mapping.
_OUTCOME_ROUTES: dict[str, tuple[str, str]] = {
    "completed": ("notify_on_completed", "completed"),
    "failed": ("notify_on_failed", "failed"),
    "awaiting_approval": ("notify_on_awaiting_approval", "awaiting approval"),
    "requeued": ("notify_on_run_requeued", "requeued"),
}

#: Subject prefix when the company row has no usable name.
_DEFAULT_SUBJECT_PREFIX = "Polsia"

#: Titles longer than this are shortened in the SUBJECT only; the body carries the full title.
_SUBJECT_TITLE_MAX = 120


class _NotificationFailure(Exception):
    """One named refusal from the send adapter. Internal — never escapes this module."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        super().__init__(str(error.get("message") or error.get("code") or "notification failed"))


async def notify_owner_for_terminal_outcome(db: AsyncSession, task_id: int, outcome: str) -> None:
    """Email the owner about `task_id`'s `outcome`; NEVER raises, NO-OP when unconfigured.

    Called by the worker's terminal-outcome hook AFTER the run's activity commit. Any failure
    while notifying is recorded as one activity row and swallowed — the run's outcome is
    already final and this leg must not be able to disturb it.
    """
    try:
        await _notify_owner(db, task_id, outcome)
    except _NotificationFailure as failure:
        await _record_failure(db, task_id, outcome, failure.error)
    except PolsiaError as exc:
        # A named failure from the read path (config `E_DB`, the task load) keeps its E_* code.
        await _record_failure(
            db,
            task_id,
            outcome,
            {"code": exc.code, "message": exc.message, "how_to_fix": exc.how_to_fix},
        )
    except Exception as exc:
        # Everything else (an adapter that raised instead of refusing, a DBAPI surprise) is
        # still only ever a recorded failure: the run is never failed by this leg.
        await _record_failure(
            db, task_id, outcome, {"code": type(exc).__name__, "message": str(exc)}
        )


async def _notify_owner(db: AsyncSession, task_id: int, outcome: str) -> None:
    """The send leg — raises for the funnel above; returns None for every no-op."""
    if outcome not in _OUTCOME_ROUTES:
        # Unknown outcome: no config read, no send, no error (hostile-case contract).
        return
    config = await company_service.get_company(db)
    recipient = _configured_recipient(config, outcome)
    if recipient is None:
        # Named no-op: no recipient stored, or this outcome's toggle is off.
        return
    task = await _load_task(db, task_id)
    subject, body = _compose(task, task_id, outcome, prefix=_subject_prefix(config))

    # D2/V12-F2: the shared gate FIRST — sandbox ON refuses by name (recorded as a labelled
    # draft), a capped channel refuses by name; only an allowed outcome reaches the provider.
    await _firewall_clearance(
        db, action="send_owner_notification", payload={"outcome": outcome, "task_id": task_id}
    )

    # Lazy import on purpose (CLAUDE.md:116): importing this module must never drag the
    # provider libraries into an import chain that does not send.
    from app.agents import integrations

    result = integrations.send_email(to=recipient, subject=subject, body=body)
    if not isinstance(result, dict) or not result.get("ok"):
        raise _NotificationFailure(_refusal_error(result))


def _configured_recipient(config: Any, outcome: str) -> str | None:
    """The owner recipient when configured for THIS outcome, else `None`.

    Reads the frozen column names defensively (`getattr` with defaults): migration 0007 that
    adds them ships in the same wave (lane P3), so before it lands every read is
    "not configured" — the no-op — never an AttributeError and never a half-send.
    """
    toggle, _label = _OUTCOME_ROUTES[outcome]
    if not bool(getattr(config, toggle, False)):
        return None
    return str(getattr(config, "notification_email", None) or "").strip() or None


async def _load_task(db: AsyncSession, task_id: int) -> Task | None:
    """The task row, or `None`; a failed read is a named `E_DB` (funnelled by the caller)."""
    return await fetch_scalar(db, select(Task).where(Task.id == task_id), what=f"task {task_id}")


def _subject_prefix(config: Any) -> str:
    name = str(getattr(config, "name", None) or "").strip()
    return name or _DEFAULT_SUBJECT_PREFIX


def _task_title(task: Task | None, task_id: int) -> str:
    """The stored title, or the honest placeholder — the id is real, the title is unknown."""
    title = str(getattr(task, "title", None) or "").strip()
    return title or f"untitled task #{task_id}"


def _recorded_error(task: Task | None, outcome: str) -> str:
    """The task's recorded error line — for `failed` and `requeued` outcomes only.

    For `requeued` the line is THIS attempt's own failure (the run just wrote it; the retry
    has not run yet), so it is the story of why the retry exists — never stale detail.
    For a `completed`/`awaiting_approval` mail the column can hold a leftover line (it is
    KEPT through a requeue/retry, `agent_tasks.py`), which must not ride: that would present
    stale detail as this outcome's story. Nothing is ever invented.
    """
    if outcome not in ("failed", "requeued"):
        return ""
    return str(getattr(task, "error_message", None) or "").strip()


def _shorten(text: str, limit: int = _SUBJECT_TITLE_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _compose(task: Task | None, task_id: int, outcome: str, *, prefix: str) -> tuple[str, str]:
    """The short honest subject + body: id, outcome, title, and the error line when there is one."""
    label = _OUTCOME_ROUTES[outcome][1]
    title = _task_title(task, task_id)
    subject = f"[{prefix}] Task #{task_id} {label}: {_shorten(title)}"
    lines = [f"Task #{task_id} - {label}.", f"Title: {title}"]
    error_line = _recorded_error(task, outcome)
    if error_line:
        lines.append(f"Error: {error_line}")
    return subject, "\n".join(lines)


def _refusal_error(result: Any) -> dict[str, Any]:
    """Normalise whatever the adapter returned into the shape the failure row carries."""
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict) and error:
        return dict(error)
    return {"code": "unexpected_result", "message": f"send_email returned {result!r}"}


async def _record_failure(
    db: AsyncSession, task_id: int, outcome: str, error: dict[str, Any]
) -> None:
    """Record ONE `owner_notification_failed` row, then return — never raises.

    The commit is owned HERE on purpose: the hook runs after the run's terminal commit
    (WAVE-PLAN) and the worker session closes without another commit, so a flush-only row
    would be rolled back at close — a ghost record. `agent_type="orchestrator"` is the
    existing non-agent producer value: the AGENT did not fail, the notification did; the
    task id / outcome / error travel in `detail`. If even this recording fails, it is
    logged and swallowed — the never-raise contract outranks the record.
    """
    code = str(error.get("code") or "unknown")
    try:
        await log_activity(
            db,
            agent_type="orchestrator",
            action="owner_notification_failed",
            summary=f"owner email for task #{task_id} ({outcome}) failed: {code}",
            level="warning",
            detail={"task_id": task_id, "outcome": outcome, "error": error},
        )
        await db.commit()
    except Exception:
        logger.exception(
            "owner notification: could not record the failure for task %s (%s)", task_id, outcome
        )


# ── cycle-report email (V8/C5) ───────────────────────────────────────────────────────────────

#: cycle kind -> the human label used in the subject. `morning`/`evening` are the two cycle
#: reports the daily cycle writes (`daily_reports.morning_plan` / `.evening_summary`).
_CYCLE_REPORT_KINDS: dict[str, str] = {"morning": "Morning", "evening": "Evening"}


async def notify_owner_cycle_report(
    db: AsyncSession, *, kind: str, report_date: date, summary: str
) -> None:
    """Email the owner the day's cycle report; NEVER raises — the cycle outranks this leg.

    Called by `daily_cycle` AFTER the report's own transaction has committed (the email must
    describe a committed report).  Contract, mirroring `notify_owner_for_terminal_outcome`:

      * configured (`company_config.cycle_report_email` on AND `notification_email` set) ->
        one send via the same `integrations.send_email` adapter;
      * unconfigured -> ONE honest `cycle_report_email_skipped` activity row naming exactly
        which piece is missing (a scheduled report that never arrives must be explainable);
      * any failure (adapter refusal, adapter raise, config read failure) -> ONE
        `cycle_report_email_failed` row, then return normally.
    """
    try:
        await _notify_cycle_report(db, kind=kind, report_date=report_date, summary=summary)
    except _NotificationFailure as failure:
        await _record_cycle_row(
            db,
            "cycle_report_email_failed",
            kind,
            report_date,
            level="warning",
            note=str(failure.error.get("code") or "refused"),
            detail={"error": failure.error},
        )
    except PolsiaError as exc:
        await _record_cycle_row(
            db,
            "cycle_report_email_failed",
            kind,
            report_date,
            level="warning",
            note=exc.code,
            detail={
                "error": {"code": exc.code, "message": exc.message, "how_to_fix": exc.how_to_fix}
            },
        )
    except Exception as exc:  # noqa: BLE001 — a cycle-report email must never fail the cycle
        await _record_cycle_row(
            db,
            "cycle_report_email_failed",
            kind,
            report_date,
            level="warning",
            note=type(exc).__name__,
            detail={"error": {"code": type(exc).__name__, "message": str(exc)}},
        )


def _cycle_skip_reason(config: Any) -> str | None:
    """Why this cycle report will not be emailed, or `None` when it will be."""
    if config is None:
        return "company config is not seeded; cycle_report_email is off"
    if not bool(getattr(config, "cycle_report_email", False)):
        return "cycle_report_email is off"
    recipient = str(getattr(config, "notification_email", None) or "").strip()
    if not recipient:
        return "notification_email is not set"
    return None


async def _notify_cycle_report(
    db: AsyncSession, *, kind: str, report_date: date, summary: str
) -> None:
    """The cycle-report send leg — raises for the funnel above; returns None for every skip."""
    if kind not in _CYCLE_REPORT_KINDS:
        await _record_cycle_row(
            db,
            "cycle_report_email_skipped",
            kind,
            report_date,
            level="info",
            note=f"unknown cycle report kind {kind!r}",
            detail={
                "reason": (
                    f"unknown cycle report kind {kind!r}; expected one of "
                    + ", ".join(sorted(_CYCLE_REPORT_KINDS))
                )
            },
        )
        return
    config = await company_service.get_company(db)
    reason = _cycle_skip_reason(config)
    if reason is not None:
        await _record_cycle_row(
            db,
            "cycle_report_email_skipped",
            kind,
            report_date,
            level="info",
            note=reason,
            detail={"reason": reason},
        )
        return

    # `_cycle_skip_reason` above answers a reason for a None config, so reaching this line proves
    # the row exists; the assert documents that invariant for the type checker.
    assert config is not None
    recipient = str(config.notification_email or "").strip()
    label = _CYCLE_REPORT_KINDS[kind]
    subject = f"[{_subject_prefix(config)}] {label} report {report_date.isoformat()}"
    body_text = str(summary or "").strip() or "(no summary recorded)"
    body = f"{label} report for {report_date.isoformat()}.\n\n{body_text}"

    # D2/V12-F2: the same gate the terminal-outcome leg runs — sandbox ON refuses by name and
    # the refusal is recorded by the funnel above; only an allowed outcome reaches the provider.
    await _firewall_clearance(
        db,
        action="send_cycle_report",
        payload={"kind": kind, "report_date": report_date.isoformat()},
    )

    # Lazy import on purpose (the same rule the terminal-outcome leg follows above).
    from app.agents import integrations

    result = integrations.send_email(to=recipient, subject=subject, body=body)
    if not isinstance(result, dict) or not result.get("ok"):
        raise _NotificationFailure(_refusal_error(result))


async def _record_cycle_row(
    db: AsyncSession,
    action: str,
    kind: str,
    report_date: date,
    *,
    level: str,
    note: str,
    detail: dict[str, Any],
) -> None:
    """Record ONE cycle-report row, then return — never raises (the `_record_failure` rule).

    The commit is owned HERE for the same reason the failure recorder owns it: this leg runs
    after the cycle's own commit and nothing else commits after it. If even the recording
    fails it is logged and swallowed — the never-raise contract outranks the record.
    """
    outcome_word = "skipped" if action.endswith("_skipped") else "failed"
    try:
        await log_activity(
            db,
            agent_type="orchestrator",
            action=action,
            summary=(
                f"cycle report email ({kind} {report_date.isoformat()}) {outcome_word}: {note}"
            ),
            level=level,
            detail={"kind": kind, "report_date": report_date.isoformat(), **detail},
        )
        await db.commit()
    except Exception:
        logger.exception(
            "cycle report email: could not record %s for %s %s", action, kind, report_date
        )


__all__ = ["notify_owner_cycle_report", "notify_owner_for_terminal_outcome"]
