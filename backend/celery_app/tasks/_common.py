"""Shared runtime helpers for the task modules: one event loop per process, idempotent
task creation, dedupe keys.

Why one loop per process: the upload built a fresh engine **and** ran
`asyncio.run()` per task (lane 06 measured 4.279 ms warm / 179 ms cold per engine and
0.698 ms per loop, with a fresh connection pool and no warm state).  `run_async` submits
the coroutine to a single long-lived loop that lives on a daemon thread, so every task in
this process reuses the same `app.core.database` engine, its pool and its loop.  A
background-thread loop (rather than a module-level loop + `run_until_complete`) is safe
from any Celery pool — solo, threads and prefork — and from `.apply()` in-process runs.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import now_utc
from app.models.task import Task

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()
_loop_guard = threading.Lock()


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    _loop_ready.set()
    loop.run_forever()


def get_process_loop() -> asyncio.AbstractEventLoop:
    """The single event loop this worker process uses for all database work."""
    global _loop, _loop_thread
    with _loop_guard:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_ready.clear()
            _loop_thread = threading.Thread(
                target=_run_loop_forever, args=(_loop,), name="polsia-task-loop", daemon=True
            )
            _loop_thread.start()
            _loop_ready.wait(timeout=10)
        return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run `coro` on the process loop and block the Celery task until it returns.

    Exceptions propagate unchanged, so `self.retry`/error classification in the task
    modules sees the real exception type (a `PolsiaError` from the service layer, a
    `ValueError` from `crew_factory`, …).
    """
    loop = get_process_loop()
    if loop.is_closed():  # pragma: no cover - only if someone disposed the loop
        raise RuntimeError("task event loop is closed; restart the worker process")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


# ── dedupe keys ──────────────────────────────────────────────────────────────
# `<agent_type>:<utc-day>:<bucket>` — the shape the queue contract names.  The unique
# index `ix_tasks_dedupe_key` (migration 0002) is what actually enforces it; the string
# is the human-readable identity of the work.
def dedupe_key(agent_type: str, bucket: str, day: datetime | None = None) -> str:
    utc_day = (day or now_utc()).date().isoformat()
    return f"{agent_type}:{utc_day}:{bucket}"


def sweep_bucket(period_hours: int, now: datetime | None = None) -> str:
    """The tick identity of a sweep whose period is `period_hours` (0-padded hour block).

    Two ticks of the same period produce the same bucket, so at-least-once delivery
    cannot create two tasks; the *next* period produces a new bucket, so the sweep still
    runs on schedule.
    """
    current = now or now_utc()
    return f"{current.hour // period_hours:02d}"


def plan_bucket(title: str) -> str:
    """Stable bucket for an orchestrator-planned task: same day + same title ⇒ same task."""
    digest = hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"plan:{digest}"


# ── idempotent task creation ─────────────────────────────────────────────────
async def create_task_deduped(
    db: AsyncSession,
    *,
    title: str,
    agent_type: str,
    dedupe_key: str,
    description: str | None = None,
    priority: int = 3,
    source: str = "scheduler",
    scheduled_date: datetime | None = None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
) -> Task | None:
    """Insert a task unless `dedupe_key` already exists.

    Returns the new `Task` (flushed, so `.id` is set) or **None** when the row already
    existed — the caller must treat None as "already created", not as a failure.

    Validation matches `task_service.create_task` (unknown agent types raise ValueError)
    so a malformed orchestrator plan is caught here rather than written.  The `status` value is
    asserted against `task_service.VALID_TASK_STATUSES` the same way (fix-5 REQUESTS §7/T17): this
    creator constructs `Task(...)` DIRECTLY, so before this assertion a caller could bypass the
    service-level check and store an out-of-set — or unset (`None`) — status on SQLite, where the
    `ck_tasks_status` CHECK is only carried by fresh `create_all` schemas.  The
    insert uses a SAVEPOINT: if a concurrent writer wins the unique index, this call
    re-checks and returns None instead of poisoning the caller's transaction.

    `metadata` is the per-task payload channel (`tasks.metadata`, the frozen JSON column the
    worker threads into the agent's prompt since RD12): `task_service.create_task` already
    accepted it, but this deduped path did not — so the CS sweep's per-item attach
    (`metadata.inbox_item`, RD12 receipt §7 REQUEST 1) could not carry its item until R17.
    Additive: the default (None) writes exactly the previous row.
    """
    from app.services.task_service import VALID_AGENT_TYPES, VALID_TASK_STATUSES

    if agent_type not in VALID_AGENT_TYPES:
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
        )
    if status not in VALID_TASK_STATUSES:
        # Refused the same way as an unknown agent type: this is a caller-side value error, not a
        # named `E_VALIDATION` refusal — the service route converts ValueError into the agent-type
        # refusal, and a status typo must not wear that code (task_service.create_task's note).
        # Covers "unset" too: `None` is not in the set.
        raise ValueError(
            f"Unknown task status: {status!r}. Valid: {', '.join(VALID_TASK_STATUSES)}"
        )

    existing = await db.scalar(select(Task.id).where(Task.dedupe_key == dedupe_key).limit(1))
    if existing is not None:
        logger.info("dedupe hit: %s already exists (task %s)", dedupe_key, existing)
        return None

    task = Task(
        title=title,
        description=description,
        agent_type=agent_type,
        priority=priority,
        status=status,
        source=source,
        scheduled_date=scheduled_date,
        dedupe_key=dedupe_key,
    )
    if metadata is not None:
        # Pass-through set AFTER construction, not `metadata_json=None`: the JSON column type
        # stores a Python None as the JSON literal 'null' (none_as_null=False), so passing
        # None would silently change the stored representation of every existing sweep row.
        # The additive promise is "the default writes exactly the previous row" (measured:
        # tests/unit/celery/test_support_inbox_sweep.py — the bare sweep task stays SQL NULL).
        task.metadata_json = metadata
    db.add(task)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        # Another writer inserted the same key between the SELECT and the INSERT.
        # Only swallow it when the key is really present; anything else is a real error.
        # (The SAVEPOINT rollback normally restores `task` to transient; the guard
        # covers a session that keeps the pending instance.)
        if sa_inspect(task).session is not None:
            db.expunge(task)
        if await db.scalar(select(Task.id).where(Task.dedupe_key == dedupe_key).limit(1)):
            logger.info("dedupe race lost: %s won by another writer", dedupe_key)
            return None
        raise
    return task


# ── broker hand-off ──────────────────────────────────────────────────────────
async def enqueue_created(task_ids: list[int], *, label: str) -> None:
    """Hand committed task rows to the broker; a failed hand-off must not strand them (D2/T19).

    The scheduler paths commit rows `pending` and then dispatch. Measured defect (audit lane 4,
    L4-F1, using the wave-4/T19 dead-broker evidence): with the broker down, `run_agent_task.delay(...)`
    raised into the caller and the rows stayed `pending` forever — nothing reconciles a row no worker
    was ever told about, and the unique dedupe key blocks the next bucket run from re-creating it.
    This mirrors the API trigger's contract (app/api/v1/agents.py): mark EVERY row `failed` with the
    measured reason in its own session, commit, then raise the named refusal so the Celery task fails
    honestly instead of reporting a fake queue.
    """
    from celery_app.tasks.agent_tasks import run_agent_task  # lazy: agent_tasks imports this module

    try:
        if len(task_ids) == 1:
            run_agent_task.delay(task_ids[0])
        else:
            from celery import group

            group([run_agent_task.s(task_id) for task_id in task_ids]).apply_async()
    except Exception as exc:  # broker unavailable: NO delivery was accepted for these rows
        from app.core.database import get_session_factory
        from app.core.errors import E_REDIS, PolsiaError
        from app.services.task_service import update_task_status

        logger.warning("failed to enqueue %s task(s) for %s", len(task_ids), label, exc_info=True)
        session_factory = get_session_factory()
        async with session_factory() as db:
            for task_id in task_ids:
                await update_task_status(
                    db,
                    task_id,
                    "failed",
                    error_message=f"{E_REDIS}: enqueue failed ({exc.__class__.__name__})",
                )
            await db.commit()
        raise PolsiaError(
            E_REDIS,
            f"{label}: {len(task_ids)} task(s) were created but could not be handed to a worker",
            context={"task_ids": task_ids},
        ) from exc


# ── dispatch budget gate ─────────────────────────────────────────────────────
async def day_spend_by_agent(db: AsyncSession) -> tuple[float, dict[str, float]]:
    """MEASURED spend for the company-local day: (total, per-agent) from `agent_runs.cost_usd`.

    Money is exact at rest (A-T17/T18); an absent day is a measured 0, never a guess. The window
    starts at the company-local midnight, bound through `as_bound_instant` for the SQLite dialect
    (the same pattern the claim path uses).
    """
    from sqlalchemy import func as sa_func

    from app.core.timeutil import as_bound_instant, day_bounds, today_in
    from app.models.task import AgentRun

    start, _end = day_bounds(today_in())
    rows = (
        await db.execute(
            select(AgentRun.agent_type, sa_func.coalesce(sa_func.sum(AgentRun.cost_usd), 0))
            .where(AgentRun.started_at >= as_bound_instant(start, db))
            .group_by(AgentRun.agent_type)
        )
    ).all()
    per_agent = {str(agent_type): float(total) for agent_type, total in rows}
    return sum(per_agent.values()), per_agent


async def assert_budget_allows(agent_type: str) -> None:
    """Dispatch-time budget gate (D3; audit A1 / lane-2 L2-F01). Raises `BudgetStop` when the day is full.

    The DAG planner's `authorize` gate had no production caller; this puts the same decision on
    the paths that actually dispatch. The policy is what the settings DECLARE
    (`agent_daily_cost_cap_usd` / `agent_run_cost_cap_usd` via `accounting.budget_policy_from_settings`;
    kill switch frozen when `POLSIA_KILL_SWITCH` says freeze). The estimate is the policy's own
    per-run cap — the flat plan carries no per-node estimate, so the gate uses the conservative
    worst case rather than an invented small number.
    """
    from app.agents.accounting import authorize, budget_policy_from_settings
    from app.agents.policy_firewall import kill_grade_from_env
    from app.core.database import get_session_factory

    policy = budget_policy_from_settings(kill_switch=kill_grade_from_env() == "freeze")
    async with get_session_factory()() as db:
        total, per_agent = await day_spend_by_agent(db)
    authorize(  # raises BudgetStop; the caller decides skip-vs-fail
        policy,
        agent_type=agent_type,
        est_cost_usd=policy.per_run_cap_usd,
        day_spent_usd=total,
        agent_spent_usd=per_agent.get(agent_type, 0.0),
    )


__all__ = [
    "assert_budget_allows",
    "create_task_deduped",
    "day_spend_by_agent",
    "dedupe_key",
    "enqueue_created",
    "get_process_loop",
    "plan_bucket",
    "run_async",
    "sweep_bucket",
]
