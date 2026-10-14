"""Auto Mode sessions — the bounded run window (wave U5, plan P2 / backend leg B22).

WHY this module exists: the reference product hands the company to "Auto Mode" for a bounded
window ("run for the next 8 hours") and the clone had no session surface at all — only the
per-run dispatch gate.  A session is a WINDOW, not a parent: it owns no task FK; the worker
hooks in `celery_app/tasks/agent_tasks.py` fold the counters in, and this module is the only
writer of `auto_mode_sessions` rows besides those hooks.

THE SEMANTICS (frozen by the wave-U5 contract; tests/unit/test_u5_auto_mode.py pins each):

  * ACTIVE = ``stopped_at IS NULL AND started_at <= now < ends_at``.  At most ONE active
    window: `start_session` lazily closes stale (expired, still-open) rows first, then
    refuses the named `E_VALIDATION` if a live one exists — never two windows.
  * start: ``ends_at = now + duration``, duration validated 1 hour .. 5 days.
  * stop early: ``stop_reason="user_stop"``; refusing `E_VALIDATION` when nothing is active
    (named, never a silent success).
  * expiry: an expired window is closed LAZILY on the next read (`status_snapshot`), on stop
    and on start, with ``stop_reason="duration_end"`` — no beat entry needed.
  * the day-budget denial in the worker closes a live window with ``stop_reason="day_cap"``.
    A gate INTERRUPTS a session, it never bypasses it; the recorded `manual_override` (T04)
    and approval parking are separate owner decisions this module does not touch.
  * counters: `tasks_run` is INCREMENTED on the session row by the worker's terminal write
    (one UPDATE, folded into the same transaction as the run's activity row);
    ``spend_used`` = SUM(agent_runs.cost_usd) over [started_at, ends_at] with the finance
    money rule (exact `Decimal` arithmetic; **None when nothing was measured** — never a
    fabricated 0.00).
  * every close stamps the final measured spend into `spend_usd`; unmeasured stays NULL.

RACE NOTE (measured, fixed in V7): the frozen migration carries only an index on `started_at`, so
no DB constraint can encode "one active row" (`ends_at > now` is not a column). Historically the
check and the insert ran in one transaction; on SQLite writes serialize, but the V7 close drill
PROVED that on PostgreSQL two TRUE-concurrent POSTs under READ COMMITTED BOTH pass the check
(sessions 2 and 3, 2026-10-14 — raw: polsia-work/build/ui-reverse/wave-v7/drills.txt). The fix
is `_acquire_start_window_lock`: a transaction-scoped `pg_advisory_xact_lock` taken BEFORE the
check-then-insert, so the second starter blocks until the first commits and then refuses the
named `E_VALIDATION`. No schema change; SQLite (unit harness) is an explicit no-op. The live
re-drill after the fix is the whole-flow proof.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import E_VALIDATION, PolsiaError
from app.core.timeutil import as_bound_instant
from app.models.auto_mode import AutoModeSession
from app.models.task import AgentRun
from app.services.finance_service import money

#: The frozen `stop_reason` value set (schema comment: duration_end|day_cap|user_stop|error).
STOP_REASON_DURATION_END = "duration_end"
STOP_REASON_DAY_CAP = "day_cap"
STOP_REASON_USER_STOP = "user_stop"
STOP_REASON_ERROR = "error"
STOP_REASONS = frozenset(
    {STOP_REASON_DURATION_END, STOP_REASON_DAY_CAP, STOP_REASON_USER_STOP, STOP_REASON_ERROR}
)

#: Duration bounds: 1 hour .. 5 days (frozen).
MIN_DURATION_HOURS = 1.0
MAX_DURATION_HOURS = 120.0

#: PostgreSQL advisory-lock key serializing `start_session`'s check-then-insert. The frozen
#: schema cannot encode "one active row" (see the module RACE NOTE), so cross-process
#: serialization rides a transaction-scoped lock instead of a constraint. The value is a fixed
#: 8-byte constant ("POLSIAV7" as hex) — collisions with other subsystems are impossible in
#: this tree because no other module takes advisory locks.
_POSTGRES_START_WINDOW_LOCK_KEY = 0x504F4C5349415637


async def _acquire_start_window_lock(db: AsyncSession) -> None:
    """Serialize the start path across PostgreSQL processes (V7 drill-proven fix).

    Two TRUE-concurrent POSTs on live PG were measured BOTH passing the active-window check
    under READ COMMITTED (sessions 2 and 3, 2026-10-14). This helper, called before the
    check-then-insert, makes the second starter BLOCK until the first transaction commits; its
    follow-up SELECT then observes the committed window and refuses `E_VALIDATION` by name.
    Transaction-scoped (`pg_advisory_xact_lock`): released on commit/rollback of the request's
    own transaction, no unlock bookkeeping. A no-op on non-PostgreSQL binds — SQLite serializes
    writers, and the unit harness must keep running it.
    """
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
        return
    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _POSTGRES_START_WINDOW_LOCK_KEY},
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a stored instant to aware UTC (SQLite hands back naive UTC wall clocks)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_duration_hours(duration_hours: float) -> timedelta:
    """1 hour .. 5 days, or the named `E_VALIDATION` refusal.  Returns the window length.

    `not (MIN <= h <= MAX)` (not two separate comparisons) so NaN — which compares False to
    everything — is refused too instead of sneaking through as "not > max and not < min".
    """
    try:
        hours = float(duration_hours)
    except (TypeError, ValueError):
        hours = float("nan")
    if not (MIN_DURATION_HOURS <= hours <= MAX_DURATION_HOURS):
        raise PolsiaError(
            E_VALIDATION,
            f"duration_hours must be between 1 (1 hour) and 120 (5 days); got {duration_hours!r}",
            how_to_fix="send duration_hours as a number between 1 and 120 (1 hour to 5 days)",
            context={
                "duration_hours": repr(duration_hours),
                "min": MIN_DURATION_HOURS,
                "max": MAX_DURATION_HOURS,
            },
        )
    return timedelta(hours=hours)


async def get_live_session(
    db: AsyncSession, *, now: datetime | None = None
) -> AutoModeSession | None:
    """The one window covering `now`, if any.  Read-only — it never closes anything.

    Deterministic order (newest first) so that even the documented duplicate-window race
    answers the same row on every read.
    """
    moment = now or utcnow()
    bound = as_bound_instant(moment, db)
    return (
        (
            await db.execute(
                select(AutoModeSession)
                .where(
                    AutoModeSession.stopped_at.is_(None),
                    AutoModeSession.started_at <= bound,
                    AutoModeSession.ends_at > bound,
                )
                .order_by(AutoModeSession.started_at.desc(), AutoModeSession.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def spend_over_window(
    db: AsyncSession, started_at: datetime | None, ends_at: datetime | None
) -> Decimal | None:
    """SUM(agent_runs.cost_usd) for runs STARTED inside [started_at, ends_at].

    The finance money rule: exact `Decimal` through `finance_service.money`, and ``None``
    when nothing was measured (no ledger rows, or rows whose `cost_usd` is NULL) — never a
    fabricated ``Decimal("0")`` (H-03 made this counter truthful from day one: cycle runs
    reach `agent_runs`).
    """
    start = _as_utc(started_at)
    end = _as_utc(ends_at)
    if start is None or end is None:
        return None
    total = await db.scalar(
        select(sa_func.sum(AgentRun.cost_usd)).where(
            AgentRun.started_at.is_not(None),
            AgentRun.started_at >= as_bound_instant(start, db),
            AgentRun.started_at <= as_bound_instant(end, db),
        )
    )
    return money(total)


async def _close_rows(
    db: AsyncSession,
    rows: list[AutoModeSession],
    *,
    reason: str,
    now: datetime,
) -> list[AutoModeSession]:
    """Stamp `stopped_at`/`stop_reason` on each row and snapshot its final measured spend."""
    for row in rows:
        spend = await spend_over_window(db, row.started_at, row.ends_at)
        row.stopped_at = now
        row.stop_reason = reason
        if spend is not None:
            row.spend_usd = spend
    if rows:
        await db.flush()
    return rows


async def close_stale_sessions(
    db: AsyncSession, *, now: datetime | None = None
) -> list[AutoModeSession]:
    """Lazy expiry: close every still-open window whose `ends_at` has passed — `duration_end`."""
    moment = now or utcnow()
    bound = as_bound_instant(moment, db)
    rows = (
        (
            await db.execute(
                select(AutoModeSession).where(
                    AutoModeSession.stopped_at.is_(None),
                    AutoModeSession.ends_at <= bound,
                )
            )
        )
        .scalars()
        .all()
    )
    return await _close_rows(db, list(rows), reason=STOP_REASON_DURATION_END, now=moment)


async def close_live_sessions(
    db: AsyncSession, *, reason: str, now: datetime | None = None
) -> list[AutoModeSession]:
    """Close every window that COVERS `now` (the worker's `day_cap` hook uses this).

    Only covering rows are closed: an expired-but-unclosed row is somebody else's lazy
    `duration_end` (labelling it `day_cap` would blame a reason that is not true), and rows
    closed earlier are immune because the selection keys on ``stopped_at IS NULL``.
    """
    if reason not in STOP_REASONS:
        raise ValueError(f"unknown auto-mode stop reason: {reason!r}")
    moment = now or utcnow()
    bound = as_bound_instant(moment, db)
    rows = (
        (
            await db.execute(
                select(AutoModeSession).where(
                    AutoModeSession.stopped_at.is_(None),
                    AutoModeSession.started_at <= bound,
                    AutoModeSession.ends_at > bound,
                )
            )
        )
        .scalars()
        .all()
    )
    return await _close_rows(db, list(rows), reason=reason, now=moment)


async def start_session(
    db: AsyncSession, *, duration_hours: float, now: datetime | None = None
) -> AutoModeSession:
    """Open the one Auto Mode window: insert with `ends_at = now + duration`.

    Refuses the named `E_VALIDATION` when a session is already active — never two windows.
    A stale (expired) row does not block a new window; it is closed first with
    `duration_end`, exactly as a status read would have done.
    """
    duration = validate_duration_hours(duration_hours)
    moment = now or utcnow()
    await _acquire_start_window_lock(db)
    await close_stale_sessions(db, now=moment)
    live = await get_live_session(db, now=moment)
    if live is not None:
        ends = _as_utc(live.ends_at)
        raise PolsiaError(
            E_VALIDATION,
            "an Auto Mode session is already active",
            how_to_fix=(
                "stop the active session with DELETE /api/v1/auto-mode before starting another "
                "(GET /api/v1/auto-mode shows its id and ends_at)"
            ),
            # JSON-serializable context only (the refusal envelope is rendered by the app's
            # exception handler): the instant travels as ISO-8601 text, not a datetime object.
            context={
                "active_session_id": int(live.id),
                "ends_at": ends.isoformat() if ends is not None else None,
            },
        )
    row = AutoModeSession(started_at=moment, ends_at=moment + duration, tasks_run=0)
    db.add(row)
    await db.flush()
    return row


async def stop_active_session(
    db: AsyncSession, *, now: datetime | None = None
) -> AutoModeSession:
    """Close the live window early with `user_stop`; refuse `E_VALIDATION` when none is live.

    Stale (expired) rows are closed first so "nothing is active" is a statement about the
    window, not about bookkeeping lag.
    """
    moment = now or utcnow()
    await close_stale_sessions(db, now=moment)
    live = await get_live_session(db, now=moment)
    if live is None:
        raise PolsiaError(
            E_VALIDATION,
            "no Auto Mode session is active",
            how_to_fix=(
                "start one with POST /api/v1/auto-mode; GET /api/v1/auto-mode reports whether "
                "a window is live"
            ),
        )
    closed = await _close_rows(db, [live], reason=STOP_REASON_USER_STOP, now=moment)
    return closed[0]


async def increment_tasks_run(db: AsyncSession, *, now: datetime | None = None) -> int:
    """One UPDATE: bump `tasks_run` on every window covering `now`.  Returns rows affected.

    Folded into the worker's terminal transaction (the same session/commit as the run's
    activity row) — a single statement, no read-modify-write.  A task whose terminal write
    falls outside every window changes nothing; SQLite may answer -1 for an untyped driver
    rowcount and `or 0` normalises that, while the UPDATE itself is unconditional in its
    `WHERE` (never a conditional that hides an error).
    """
    moment = now or utcnow()
    bound = as_bound_instant(moment, db)
    result = await db.execute(
        sa_update(AutoModeSession)
        .where(
            AutoModeSession.stopped_at.is_(None),
            AutoModeSession.started_at <= bound,
            AutoModeSession.ends_at > bound,
        )
        .values(tasks_run=sa_func.coalesce(AutoModeSession.tasks_run, 0) + 1)
    )
    return int(result.rowcount or 0)


async def session_view(
    db: AsyncSession, row: AutoModeSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """The wire view of one session row + its live counters (router models mirror these keys).

    `time_left_seconds` is only meaningful for a COVERING window (None once closed), and
    `spend_used` is recomputed from the ledger on every read — the stored `spend_usd` is the
    close-time snapshot, the live number always reflects the rows on disk.
    """
    moment = now or utcnow()
    started = _as_utc(row.started_at)
    ends = _as_utc(row.ends_at)
    stopped = _as_utc(row.stopped_at)
    live = stopped is None and ends is not None and ends > moment
    left = int((ends - moment).total_seconds()) if live and ends is not None else None
    return {
        "id": int(row.id),
        "started_at": started,
        "ends_at": ends,
        "stopped_at": stopped,
        "stop_reason": row.stop_reason,
        "tasks_run": int(row.tasks_run or 0),
        "spend_used": await spend_over_window(db, started, ends),
        "time_left_seconds": max(0, left) if left is not None else None,
    }


async def status_snapshot(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """`GET`'s payload: the live window + live counters, after lazy-expiring stale rows.

    The caller (the route) commits — the lazy close is a real write, and it must persist
    independently of `get_db`'s teardown commit so the route behaves identically under a
    plain test-client session override (the approvals.py precedent).
    """
    moment = now or utcnow()
    await close_stale_sessions(db, now=moment)
    live = await get_live_session(db, now=moment)
    if live is None:
        return {"active": False, "session": None}
    return {"active": True, "session": await session_view(db, live, now=moment)}


__all__ = [
    "MAX_DURATION_HOURS",
    "MIN_DURATION_HOURS",
    "STOP_REASONS",
    "STOP_REASON_DAY_CAP",
    "STOP_REASON_DURATION_END",
    "STOP_REASON_ERROR",
    "STOP_REASON_USER_STOP",
    "close_live_sessions",
    "close_stale_sessions",
    "get_live_session",
    "increment_tasks_run",
    "session_view",
    "spend_over_window",
    "start_session",
    "status_snapshot",
    "stop_active_session",
    "validate_duration_hours",
]
