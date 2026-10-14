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
  * Outcomes arrive as exactly `completed` | `failed` | `awaiting_approval`. Anything else
    (including `requeued`, which has a schema toggle but no hook contract yet) is a silent
    no-op: no send, no error, not even a config read.

The send rides the existing `integrations.send_email` adapter with an explicit recipient
(`app/agents/integrations.py:78`), whose refusal semantics are already honest: an
unconfigured SendGrid returns a named `E_INTEGRATION` refusal, never a fake success.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PolsiaError
from app.models.task import Task
from app.services import company_service
from app.services.activity_service import log_activity
from app.services.query_helpers import fetch_scalar

logger = logging.getLogger(__name__)

#: outcome -> (matching company_config toggle column, human label). The toggle names are the
#: FROZEN P3 column names — a typo here is a silent no-op forever, so tests pin the mapping.
_OUTCOME_ROUTES: dict[str, tuple[str, str]] = {
    "completed": ("notify_on_completed", "completed"),
    "failed": ("notify_on_failed", "failed"),
    "awaiting_approval": ("notify_on_awaiting_approval", "awaiting approval"),
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
    """The task's recorded error line — only when THIS outcome is a failure.

    `tasks.error_message` is KEPT on the row through a requeue/retry (`agent_tasks.py`
    :730-732), so a leftover line must not ride a `completed`/`awaiting_approval` mail:
    that would present stale detail as this outcome's story. Nothing is ever invented.
    """
    if outcome != "failed":
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


__all__ = ["notify_owner_for_terminal_outcome"]
