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
) -> Task | None:
    """Insert a task unless `dedupe_key` already exists.

    Returns the new `Task` (flushed, so `.id` is set) or **None** when the row already
    existed — the caller must treat None as "already created", not as a failure.

    Validation matches `task_service.create_task` (unknown agent types raise ValueError)
    so a malformed orchestrator plan is caught here rather than written.  The
    insert uses a SAVEPOINT: if a concurrent writer wins the unique index, this call
    re-checks and returns None instead of poisoning the caller's transaction.
    """
    from app.services.task_service import VALID_AGENT_TYPES

    if agent_type not in VALID_AGENT_TYPES:
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
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


__all__ = [
    "create_task_deduped",
    "dedupe_key",
    "get_process_loop",
    "plan_bucket",
    "run_async",
    "sweep_bucket",
]
