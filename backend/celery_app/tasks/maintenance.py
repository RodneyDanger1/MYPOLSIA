"""Maintenance tasks — retention cleanup and the task-lease sweep.

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
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app.config import settings
from app.core.timeutil import as_bound_instant, now_utc
from app.services.task_service import requeue_stale_claims
from celery_app.tasks._common import run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)


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
