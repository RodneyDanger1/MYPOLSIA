"""Maintenance tasks — retention cleanup, the task-lease sweep, and the publish-health tick.

The uploaded `cleanup_old_activity` could never run: no beat entry, no caller (D-15), and
its cutoff used `datetime.utcnow()` (naive, deprecated in 3.12) against a `timestamptz`
column while deleting every matching row in one unbounded transaction.

Here: a timezone-aware cutoff adapted to the active dialect, deletes in chunks of
`settings.activity_bulk_flush` so a first run against a large table cannot hold one long
transaction (long transactions block vacuum — lane 06 §4.4), and the number of deleted rows
is reported in the task result and in the log.

`requeue_stale_task_claims` is the second job. `requeue_stale_claims` (task_service) existed,
was tested, and had NO caller anywhere in the tree — a worker that dies mid-run leaves its
claim live until the lease expires, and nothing ever expired it, so the recovery was inert
(T1). This module drives that service through the process loop on a 5-minute beat entry
(`celery_app/beat_schedule.py`), strictly inside the default 900 s claim lease.

`check_activity_publish_health` is the third job (R22). The activity breaker counts failures and
skips and exposes them via `events.publish_stats()`, but that function had ZERO production
readers — the "a skip is never silent" contract was visible to tests only. The tick puts the
stats into the worker's log every 15 min. PROCESS BOUNDARY, stated not hidden: the counters are
module globals in `app.core.events`, so this tick reports the WORKER process's state (where
agent-task activity publishes happen); the API process's counters are served by
GET /api/v1/ops/events/stats (owner auth).

`reconcile_stranded_pending_tasks` is the fourth job (V11/W2, audit F1): `cadence` and
`followups` commit `pending` rows and THEN hand them to the broker; a crash between the two (or
a broker that accepts then loses the message) leaves the row `pending` forever — the lease
sweep covers `in_progress` only, and the unique dedupe key blocks the next bucket run from
re-creating the child (the T19/L4-F1 contract, missed on the scheduler paths). This tick
re-dispatches `pending` `source='scheduler'` rows older than
`PENDING_RECONCILE_AFTER_SECONDS` through the SAME broker hand-off the workers use
(`run_agent_task.delay`; an atomic claim makes a duplicate delivery a safe skip), stamps the
`dispatched_at` marker on acceptance, and names every refusal in an activity row — nothing is
silently re-sent and nothing is silently left behind. The BEAT ENTRY is the PARENT's to append
at settle (this module does not own `beat_schedule.py`), every 5 minutes next to the lease
sweep:

    "reconcile-stranded-pending": {
        "task": "celery_app.tasks.maintenance.reconcile_stranded_pending_tasks",
        "schedule": crontab(minute="*/5"),
    }
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.core.events import publish_stats
from app.core.timeutil import UTC, as_bound_instant, now_utc
from app.services.task_service import requeue_stale_claims
from celery_app.tasks._common import run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

#: How long a `pending` scheduler row may sit UNCLAIMED before this reconciler treats it as
#: stranded: two full cadence periods (15 min each), comfortably past every live hand-off draw.
PENDING_RECONCILE_AFTER_SECONDS = 30 * 60
#: One sweep's work bound (larger than any healthy tick's fan-out; the next sweep continues).
PENDING_RECONCILE_BATCH = 200
#: Activity actions — a re-dispatch and a refused re-dispatch are both visible, never silent.
REDISPATCHED_ACTION = "task_pending_redispatched"
REDISPATCH_FAILED_ACTION = "task_pending_redispatch_failed"


@app.task(name="celery_app.tasks.maintenance.cleanup_old_activity")
def cleanup_old_activity() -> dict[str, Any]:
    """Delete `activity_log` rows older than `settings.activity_retention_days`."""
    return run_async(_cleanup_old_activity())


async def _cleanup_old_activity() -> dict[str, Any]:
    from sqlalchemy import delete, select

    from app.core.database import get_session_factory
    from app.models.report import ActivityLog

    session_factory = get_session_factory()
    cutoff = now_utc() - timedelta(days=settings.activity_retention_days)
    chunk = max(1, int(settings.activity_bulk_flush))

    deleted = 0
    while True:
        async with session_factory() as db:
            # Dialect-correct binding: SQLite stores the naive UTC wall clock, PostgreSQL
            # keeps a real timestamptz (timeutil.as_bound_instant is the one place that
            # knows the difference).
            bound = as_bound_instant(cutoff, db)
            ids = (
                await db.execute(
                    select(ActivityLog.id)
                    .where(ActivityLog.created_at < bound)
                    .order_by(ActivityLog.created_at)
                    .limit(chunk)
                )
            ).scalars().all()
            if not ids:
                break

            result = await db.execute(delete(ActivityLog).where(ActivityLog.id.in_(list(ids))))
            await db.commit()
            batch = int(result.rowcount or 0)
            deleted += batch
            logger.info(
                "activity retention: deleted %s row(s) older than %s (running total %s)",
                batch,
                cutoff.isoformat(),
                deleted,
            )
            if batch == 0:
                # A driver that does not report rowcount: stop rather than loop forever.
                # The next scheduled run picks the residue up.
                logger.warning(
                    "activity retention: delete reported 0 affected rows for %s selected id(s); "
                    "stopping this pass",
                    len(ids),
                )
                break

    return {
        "status": "completed",
        "deleted": deleted,
        "cutoff": cutoff.isoformat(),
        "retention_days": settings.activity_retention_days,
        "batch_size": chunk,
    }


@app.task(name="celery_app.tasks.maintenance.requeue_stale_task_claims")
def requeue_stale_task_claims() -> dict[str, Any]:
    """Every 5 min — recover tasks whose claiming worker died (claim lease expiry).

    The interval is deliberately SHORTER than `task_claim_lease_seconds` (default 900 s):
    a crashed worker's claim is unreachable until something sweeps, so the worst-case
    recovery bound is interval + lease (~20 min at the defaults). A row that has exhausted
    `task_max_attempts` goes terminal inside `requeue_stale_claims` itself — the sweep never
    requeues it forever.
    """
    return run_async(_requeue_stale_claims_in_session())


async def _requeue_stale_claims_in_session() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.core.database import get_session_factory
    from app.models.task import Task

    session_factory = get_session_factory()
    async with session_factory() as db:
        # `scanned` is measured, not assumed: the in-flight claims the sweep's WHERE examines.
        scanned = int(
            await db.scalar(
                select(func.count()).select_from(Task).where(Task.status == "in_progress")
            )
        )
        # The service's own count of recovered claims: back to `pending`, or terminal when
        # the attempts are spent (reason recorded) — either way, no longer both stuck and
        # invisible.
        requeued = await requeue_stale_claims(db)
        await db.commit()

    logger.info("task-lease sweep: recovered %s of %s in-flight claim(s)", requeued, scanned)
    return {"status": "completed", "requeued": requeued, "scanned": scanned}


@app.task(name="celery_app.tasks.maintenance.check_activity_publish_health")
def check_activity_publish_health() -> dict[str, Any]:
    """Every 15 min — a publish outage stays on the worker's log until it is cleared.

    WARNING (with the full stats dict) while anything is non-clean: any failure, any skipped
    publish, or an open breaker.  `logger.debug` when clean, so a healthy fleet does not pay one
    line per tick.  The counters are cumulative since worker start (or the last
    `POST /api/v1/ops/events/reset`), so the warning repeats until an operator resets after
    fixing the incident — that repetition is the point: a skip is never silent.
    """
    stats = publish_stats()
    dirty = bool(stats["failures"]) or bool(stats["skipped_while_open"]) or bool(stats["breaker_open"])
    if dirty:
        logger.warning("activity publish breaker is not clean: %s", stats)
    else:
        logger.debug("activity publish breaker clean: %s", stats)
    return {"status": "completed", "clean": not dirty, "stats": stats}


# ── the stranded-pending reconciler (V11/W2, audit F1) ───────────────────────────────────────

async def mark_tasks_dispatched(task_ids: list[int]) -> int:
    """Stamp `metadata.dispatched_at` on rows the broker just ACCEPTED — the F1 marker.

    Called by the scheduler paths (`cadence`, `followups`) after their `enqueue_created`
    hand-off returns (an accepted hand-off is the moment these rows are no longer the
    process's to lose), and by the reconciler itself when it re-dispatches.  An ADDITIVE
    metadata key: every existing consumer of `metadata_json` keeps its keys untouched.
    Returns the number of rows stamped.
    """
    if not task_ids:
        return 0
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.task import Task

    stamp = now_utc().isoformat()
    ids = [int(item) for item in task_ids]
    async with get_session_factory()() as db:
        rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        for row in rows:
            existing = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            # Re-assign a NEW dict: the JSON column tracks attribute assignment, not in-place
            # mutation (the `_common.create_task_deduped` metadata rule, same reason).
            row.metadata_json = {**existing, "dispatched_at": stamp}
        await db.commit()
    return len(rows)


@app.task(name="celery_app.tasks.maintenance.reconcile_stranded_pending_tasks")
def reconcile_stranded_pending_tasks() -> dict[str, Any]:
    """Every 5 min (parent appends the beat entry at settle) — re-dispatch stranded `pending` rows."""
    return run_async(_reconcile_stranded_pending())


async def _reconcile_stranded_pending() -> dict[str, Any]:
    """`pending` + `source='scheduler'` + never claimed + older than the window -> hand off again.

    One row at a time through the SAME broker call the workers' entrypoint uses
    (`run_agent_task.delay`); a duplicate delivery is safe because the claim
    (`task_service.claim_task`) is atomic and a non-`pending` row is a named skip in
    `_execute_agent_task`.  A row whose `dispatched_at` marker is still INSIDE the window is
    a live hand-off (it pushed its own next attempt out by another window), not a stranded row.
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.task import Task
    from app.services.activity_service import log_activity

    session_factory = get_session_factory()
    cutoff = now_utc() - timedelta(seconds=PENDING_RECONCILE_AFTER_SECONDS)

    async with session_factory() as db:
        bound = as_bound_instant(cutoff, db)
        rows = (
            await db.execute(
                select(Task.id, Task.metadata_json)
                .where(
                    Task.status == "pending",
                    Task.source == "scheduler",
                    Task.claimed_at.is_(None),
                    Task.created_at < bound,
                )
                .order_by(Task.id)
                .limit(PENDING_RECONCILE_BATCH)
            )
        ).all()

    from celery_app.tasks.agent_tasks import run_agent_task  # lazy: the broker edge

    redispatched_ids: list[int] = []
    failed_ids: list[int] = []
    skipped_recent: list[int] = []
    for task_id, metadata in rows:
        task_id = int(task_id)
        meta = metadata if isinstance(metadata, dict) else {}
        dispatched_at = meta.get("dispatched_at")
        if isinstance(dispatched_at, str):
            try:
                last = datetime.fromisoformat(dispatched_at)
            except ValueError:
                last = None
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if last > cutoff:
                    skipped_recent.append(task_id)  # live hand-off inside the window
                    continue
        try:
            run_agent_task.delay(task_id)
        except Exception as exc:  # noqa: BLE001 - broker refusal is NAMED below, row stays pending
            logger.warning("pending reconciler: task %s redispatch refused: %s", task_id, exc)
            failed_ids.append(task_id)
            continue
        redispatched_ids.append(task_id)

    if redispatched_ids:
        await mark_tasks_dispatched(redispatched_ids)
    if redispatched_ids or failed_ids:
        async with session_factory() as db:
            if redispatched_ids:
                await log_activity(
                    db,
                    "system",
                    REDISPATCHED_ACTION,
                    summary=(
                        f"re-dispatched {len(redispatched_ids)} stranded pending task(s) "
                        f"to the broker (unclaimed for over "
                        f"{PENDING_RECONCILE_AFTER_SECONDS // 60} min)"
                    ),
                    detail={
                        "task_ids": redispatched_ids,
                        "window_seconds": PENDING_RECONCILE_AFTER_SECONDS,
                    },
                )
            if failed_ids:
                await log_activity(
                    db,
                    "system",
                    REDISPATCH_FAILED_ACTION,
                    summary=(
                        f"could not re-dispatch {len(failed_ids)} stranded pending task(s); "
                        f"they stay pending for the next sweep"
                    ),
                    level="warning",
                    detail={"task_ids": failed_ids},
                )
            await db.commit()

    result = {
        "status": "completed",
        "scanned": len(rows),
        "redispatched": len(redispatched_ids),
        "failed": failed_ids,
        "skipped_recent": skipped_recent,
        "task_ids": redispatched_ids,
        "window_seconds": PENDING_RECONCILE_AFTER_SECONDS,
    }
    logger.info("pending reconciler: %s", result)
    return result
