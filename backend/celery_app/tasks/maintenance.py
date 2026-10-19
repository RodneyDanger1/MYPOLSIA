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
GET /api/v1/ops/events/stats (owner auth). V23/L4 (COMPLETENESS #8) closes the visibility gap
between those two processes: the tick also publishes the four-value snapshot
`{failures, skipped_while_open, breaker_open, ts}` to the shared Redis key
`redis_client.ACTIVITY_PUBLISH_STATS_KEY` (TTL one beat period), and
`GET /api/v1/ops/activity-publish` serves it to the Usage page's worker-process card.

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

`close_stale_agent_runs` is the fifth job (wave v15 / E3 item 3-01, BUG-DIGEST MAJOR §B5):
`agent_runs` rows stuck `running` forever.  The ledger's ONLY closer is
`task_service.finish_agent_run`, which a dead worker never reaches — so every run a crashed
worker started sat `running` for the age of the database, invisible to every maintenance tick.
This sweep closes rows whose `started_at` is older than `AGENT_RUN_STALE_AFTER_SECONDS` (24x
the shipped CLI cap) through that same service call, with the named reason on the row
(`raw_log`) and one batched activity row per sweep; `duration_secs`/`cost_usd` are left exactly
as recorded — a run that never finished has no measured duration and its cost stays NULL when
unmeasured (A-T17), while a MEASURED cost keeps counting against the day cap unchanged.
Idempotent by construction: closed rows are no longer `running`, so the next tick is a no-op.
`rearm_paused_released_tasks` is the sixth job (wave v15 / E3 item 3-03, §B7 + decision #2):
the resume half of the paused-dispatch contract — while the company is NOT paused it
re-dispatches `pending` rows the pause gate released (marker `tasks.metadata.paused_release`)
through `run_agent_task.delay`, clearing the marker on acceptance so the resume re-arms each
row EXACTLY ONCE; a broker refusal keeps the marker for the next tick, and while paused the
tick hands nothing to the broker.  ACCEPTANCE IS NOT DELIVERY (E7 F-03): `.delay()` returning
means the broker took the publish, so a message accepted then lost leaves the row `pending` and
marker-less — `reconcile_stranded_pending_tasks` covers that population too, through the
`rearmed_at` stamp the clear leaves behind (any `source`, unclaimed, older than one window).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.core import redis_client
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
#: A re-armed row is fed again after one full window with no claim observed (E7 F-03): the re-arm
#: clears its marker on broker ACCEPTANCE, and `.delay()` returning only means the broker took the
#: publish — an accept-then-lost message left a NON-scheduler row `pending`, unclaimed and with no
#: second net.  The same window as the scheduler net: two cadence periods.
REARM_REDISPATCH_AFTER_SECONDS = PENDING_RECONCILE_AFTER_SECONDS
#: Activity actions — a re-dispatch and a refused re-dispatch are both visible, never silent.
REDISPATCHED_ACTION = "task_pending_redispatched"
REDISPATCH_FAILED_ACTION = "task_pending_redispatch_failed"


def _metadata_instant(value: Any) -> datetime | None:
    """An ISO timestamp from `tasks.metadata` as an aware UTC instant, or None.

    Absent, non-string and unparseable values are None (a skip, never an exception): the metadata
    channel is caller-owned, so a malformed stamp must not take a maintenance tick down.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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

    V23/L4 (COMPLETENESS #8): the same snapshot is also PUBLISHED under
    `redis_client.ACTIVITY_PUBLISH_STATS_KEY` (TTL one beat period) so the API process can serve
    the worker's numbers — `GET /api/v1/ops/activity-publish`.  The write is observability, not
    work: any Redis failure is logged and the tick still returns its stats, never failing the
    maintenance job over a stats mirror.
    """
    return run_async(_check_activity_publish_health())


async def _check_activity_publish_health() -> dict[str, Any]:
    stats = publish_stats()
    dirty = bool(stats["failures"]) or bool(stats["skipped_while_open"]) or bool(stats["breaker_open"])
    if dirty:
        logger.warning("activity publish breaker is not clean: %s", stats)
    else:
        logger.debug("activity publish breaker clean: %s", stats)

    snapshot = {
        "failures": int(stats["failures"]),
        "skipped_while_open": int(stats["skipped_while_open"]),
        "breaker_open": bool(stats["breaker_open"]),
        "ts": now_utc().isoformat(),
    }
    try:
        await redis_client.write_activity_publish_stats(snapshot)
    except Exception as exc:  # noqa: BLE001 - a stats mirror must NEVER fail the tick
        # INFO, not WARNING: the clean-tick contract leaves WARNING for a dirty breaker, and this
        # tick's own result/log still carry the real counters.  The worker card's honest
        # "not published yet" state is where an operator sees the consequence.
        logger.info(
            "activity publish stats were not written to Redis (%s: %s); the tick continues",
            type(exc).__name__,
            exc,
        )

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
    """`pending` + never claimed + older than the window -> hand off again.

    Two shapes are stranded and both go back to the broker:

    * `source='scheduler'` with `created_at` older than the window (the V11 commit->dispatch gap);
    * ANY source carrying the pause re-arm's own evidence (`metadata.rearmed_at` older than one
      window, E7 F-03).  The re-arm clears `paused_release` on broker ACCEPTANCE, so a message the
      broker accepted and then lost left a non-scheduler row looking exactly like a fresh
      `pending` row — with nothing to feed it again.  The re-arm stamp is the evidence, the claim
      is the proof of life, and a row that IS claimed or no longer `pending` is never re-sent.

    One row at a time through the SAME broker call the workers' entrypoint uses
    (`run_agent_task.delay`); a duplicate delivery is safe because the claim
    (`task_service.claim_task`) is atomic and a non-`pending` row is a named skip in
    `_execute_agent_task`.  A row whose `dispatched_at` marker is still INSIDE the window is
    a live hand-off (it pushed its own next attempt out by another window), not a stranded row —
    so each row is re-dispatched at most ONCE per window, whichever shape matched.
    """
    from sqlalchemy import String, and_, cast, or_, select

    from app.core.database import get_session_factory
    from app.models.task import Task
    from app.services.activity_service import log_activity

    session_factory = get_session_factory()
    cutoff = now_utc() - timedelta(seconds=PENDING_RECONCILE_AFTER_SECONDS)

    async with session_factory() as db:
        bound = as_bound_instant(cutoff, db)
        rows = (
            await db.execute(
                select(Task.id, Task.source, Task.created_at, Task.metadata_json)
                .where(
                    Task.status == "pending",
                    Task.claimed_at.is_(None),
                    or_(
                        and_(Task.source == "scheduler", Task.created_at < bound),
                        # prefilter only (dialect-portable LIKE on the rendered JSON); the exact
                        # timestamp comparison below decides.
                        cast(Task.metadata_json, String).like("%rearmed_at%"),
                    ),
                )
                .order_by(Task.id)
                .limit(PENDING_RECONCILE_BATCH)
            )
        ).all()

    from celery_app.tasks.agent_tasks import run_agent_task  # lazy: the broker edge

    redispatched_ids: list[int] = []
    rearmed_ids: list[int] = []
    failed_ids: list[int] = []
    skipped_recent: list[int] = []
    for task_id, source, created_at, metadata in rows:
        task_id = int(task_id)
        meta = metadata if isinstance(metadata, dict) else {}
        rearmed_at = _metadata_instant(meta.get("rearmed_at"))
        scheduler_stranded = (
            str(source) == "scheduler"
            and created_at is not None
            and (created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)) < cutoff
        )
        rearm_stranded = rearmed_at is not None and rearmed_at < cutoff
        if not (scheduler_stranded or rearm_stranded):
            continue  # selected by the prefilter but inside the window for its own shape
        last = _metadata_instant(meta.get("dispatched_at"))
        if last is not None and last > cutoff:
            skipped_recent.append(task_id)  # live hand-off inside the window
            continue
        try:
            run_agent_task.delay(task_id)
        except Exception as exc:  # noqa: BLE001 - broker refusal is NAMED below, row stays pending
            logger.warning("pending reconciler: task %s redispatch refused: %s", task_id, exc)
            failed_ids.append(task_id)
            continue
        redispatched_ids.append(task_id)
        if rearm_stranded:
            rearmed_ids.append(task_id)

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
                        "rearmed_ids": rearmed_ids,
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
        "rearmed_ids": rearmed_ids,
        "window_seconds": PENDING_RECONCILE_AFTER_SECONDS,
    }
    logger.info("pending reconciler: %s", result)
    return result


# ── the stale-run closer (wave v15 / E3 item 3-01, B5) ───────────────────────────────────────

#: How long a `running` agent run may stay open before this sweep treats it as abandoned.
#: 24x the shipped CLI cap (`settings.claude_timeout_seconds` = 300 s): a run still open an
#: order of magnitude past its own timeout cannot be a healthy run.  A module constant, not a
#: settings key — `app/config.py` carries the frozen build keys and is not this lane's file.
AGENT_RUN_STALE_AFTER_SECONDS = 2 * 60 * 60
#: The named reason code every closed run carries (on `agent_runs.raw_log`) and the sweep's feed action.
STALE_RUN_REASON_CODE = "E_RUN_STALE"
STALE_RUNS_CLOSED_ACTION = "stale_agent_runs_closed"
#: How many recovered ids the sweep's SUMMARY line names (detail always carries every id).
_STALE_RUN_IDS_IN_SUMMARY = 20


@app.task(name="celery_app.tasks.maintenance.close_stale_agent_runs")
def close_stale_agent_runs() -> dict[str, Any]:
    """Every 5 min — close `agent_runs` rows stuck `running` past the staleness bound (B5)."""
    return run_async(_close_stale_runs_in_session())


async def _close_stale_runs_in_session() -> dict[str, Any]:
    """Close every `running` run whose `started_at` cannot be a live run anymore.

    The close routes through `task_service.finish_agent_run` — the ledger's one closer — with
    status `cancelled` (the `ck_agent_runs_status` vocabulary's terminal-abandoned state; the
    run neither completed nor produced a failure an agent authored) and the named reason on
    `raw_log`.  `duration_secs` stays NULL: the run never reported one and the sweep does not
    invent it (the reason string carries the measured AGE, which is a different fact).
    `cost_usd` is untouched: an unmeasured cost stays NULL (A-T17) and a measured one keeps
    counting against the day cap exactly as recorded.

    A `running` row with NO `started_at` is closed too, with the missing timestamp NAMED: it
    can never become measurable later, and an immortal `running` row is the defect itself.
    Ordered by id, one batched activity row per sweep, and idempotent by construction — a
    closed row is no longer `running`, so the next tick scans past it.
    """
    from sqlalchemy import or_, select

    from app.core.database import get_session_factory
    from app.models.task import AgentRun
    from app.services.activity_service import log_activities
    from app.services.task_service import finish_agent_run

    session_factory = get_session_factory()
    cutoff = now_utc() - timedelta(seconds=AGENT_RUN_STALE_AFTER_SECONDS)

    closed: list[int] = []
    undated: list[int] = []
    scanned = 0
    async with session_factory() as db:
        bound = as_bound_instant(cutoff, db)
        rows = (
            await db.execute(
                select(AgentRun.id, AgentRun.started_at)
                .where(
                    AgentRun.status == "running",
                    or_(AgentRun.started_at.is_(None), AgentRun.started_at < bound),
                )
                .order_by(AgentRun.id)
            )
        ).all()
        scanned = len(rows)
        for raw_id, started_at in rows:
            run_id = int(raw_id)
            if started_at is None:
                reason = (
                    f"{STALE_RUN_REASON_CODE}: run was still 'running' with no `started_at` "
                    "recorded — its age can never be measured; closed by close_stale_agent_runs; "
                    "duration unknown"
                )
                undated.append(run_id)
            else:
                opened = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=UTC)
                age_seconds = int((now_utc() - opened).total_seconds())
                reason = (
                    f"{STALE_RUN_REASON_CODE}: still 'running' after {age_seconds} s "
                    f"(bound {AGENT_RUN_STALE_AFTER_SECONDS} s; started_at {opened.isoformat()}) "
                    "— the worker died mid-run; closed by close_stale_agent_runs "
                    "(duration/cost left exactly as recorded)"
                )
            run_row = await finish_agent_run(db, run_id, "cancelled", error_message=reason)
            if run_row is not None:
                closed.append(run_id)

        if closed:
            shown = closed[:_STALE_RUN_IDS_IN_SUMMARY]
            listed = ", ".join(f"#{run_id}" for run_id in shown)
            if len(closed) > _STALE_RUN_IDS_IN_SUMMARY:
                listed += f", … (+{len(closed) - _STALE_RUN_IDS_IN_SUMMARY} more)"
            parts = [f"{len(closed)} stale agent run(s) closed"]
            if undated:
                parts.append(f"{len(undated)} had no started_at recorded")
            await log_activities(
                db,
                [
                    {
                        "agent_type": "orchestrator",
                        "action": STALE_RUNS_CLOSED_ACTION,
                        "summary": f"{'; '.join(parts)}: {listed}",
                        "level": "warning",
                        "detail": {
                            "closed_ids": closed,
                            "undated_ids": undated,
                            "bound_seconds": AGENT_RUN_STALE_AFTER_SECONDS,
                        },
                    }
                ],
            )
        await db.commit()

    result = {
        "status": "completed",
        "scanned": scanned,
        "closed": closed,
        "closed_count": len(closed),
        "undated": undated,
        "bound_seconds": AGENT_RUN_STALE_AFTER_SECONDS,
    }
    logger.info("stale-run sweep: %s", result)
    return result


# ── the pause resume re-arm (wave v15 / E3 item 3-03, B7 + decision #2) ──────────────────────

#: One tick's work bound (larger than any healthy pause release; the next tick continues).
REARM_BATCH = 200


@app.task(name="celery_app.tasks.maintenance.rearm_paused_released_tasks")
def rearm_paused_released_tasks() -> dict[str, Any]:
    """Every 5 min — hand pause-released `pending` rows back to the broker (the resume nudge)."""
    return run_async(_rearm_paused_released_tasks())


async def _rearm_paused_released_tasks() -> dict[str, Any]:
    """The resume half of the paused-dispatch contract (3-03): re-arm EXACTLY ONCE.

    The pause gate (`agent_tasks._execute_agent_task`) releases a refused row back to
    `pending` and stamps `tasks.metadata["paused_release"]` — the marker that says "this queued
    work lost its message to a pause".  While the company is still paused, this tick hands
    NOTHING to the broker (re-feeding a paused row would only churn claim/release); once the
    owner resumes, every marked row is re-dispatched through the SAME `run_agent_task.delay`
    hand-off the workers use, and the marker is cleared on ACCEPTANCE — so a second tick
    re-arms nothing, and the row runs exactly once (the claim stays atomic).

    A broker refusal keeps the marker and records the id in `failed`, so the next tick
    retries — nothing is silently dropped.  The scan is prefiltered in SQL (the JSON column
    rendered as text, LIKE on the marker key) and re-checked exactly in Python.
    """
    from sqlalchemy import String, cast, select

    from app.agents.crew_factory import resolve_dispatch_paused
    from app.core.database import get_session_factory
    from app.models.task import Task
    from app.services.activity_service import log_activity
    from celery_app.tasks.agent_tasks import PAUSED_RELEASE_KEY

    session_factory = get_session_factory()
    async with session_factory() as db:
        paused = await resolve_dispatch_paused(db)
        raw_rows = (
            await db.execute(
                select(Task.id, Task.metadata_json)
                .where(
                    Task.status == "pending",
                    Task.claimed_by.is_(None),
                    cast(Task.metadata_json, String).like(f"%{PAUSED_RELEASE_KEY}%"),
                )
                .order_by(Task.id)
                .limit(REARM_BATCH)
            )
        ).all()

    marked: list[int] = [
        int(task_id)
        for task_id, metadata in raw_rows
        if isinstance(metadata, dict) and PAUSED_RELEASE_KEY in metadata
    ]
    if not marked:
        return {
            "status": "completed",
            "paused": paused,
            "scanned": len(raw_rows),
            "rearmed": [],
            "failed": [],
            "skipped_paused": [],
        }
    if paused:
        logger.info("pause re-arm: %s row(s) wait for resume", len(marked))
        return {
            "status": "completed",
            "paused": True,
            "scanned": len(raw_rows),
            "rearmed": [],
            "failed": [],
            "skipped_paused": marked,
        }

    from celery_app.tasks.agent_tasks import run_agent_task  # lazy: the broker edge

    rearmed: list[int] = []
    failed: list[int] = []
    for task_id in marked:
        try:
            run_agent_task.delay(task_id)
        except Exception as exc:  # noqa: BLE001 — broker refusal is NAMED; the marker stays
            logger.warning("pause re-arm: task %s hand-off refused: %s", task_id, exc)
            failed.append(task_id)
            continue
        rearmed.append(task_id)

    if rearmed:
        await _clear_paused_markers(rearmed)

    if rearmed or failed:
        async with session_factory() as db:
            if rearmed:
                await log_activity(
                    db,
                    "system",
                    "paused_tasks_rearmed",
                    summary=(
                        f"re-armed {len(rearmed)} task(s) released while the company was paused "
                        "(resume)"
                    ),
                    detail={"task_ids": rearmed},
                )
            if failed:
                await log_activity(
                    db,
                    "system",
                    "paused_tasks_rearm_failed",
                    summary=(
                        f"could not re-arm {len(failed)} pause-released task(s); they stay "
                        "pending for the next sweep"
                    ),
                    level="warning",
                    detail={"task_ids": failed},
                )
            await db.commit()

    result = {
        "status": "completed",
        "paused": False,
        "scanned": len(raw_rows),
        "rearmed": rearmed,
        "failed": failed,
        "skipped_paused": [],
    }
    logger.info("pause re-arm: %s", result)
    return result


async def _clear_paused_markers(task_ids: list[int]) -> None:
    """Clear `metadata.paused_release` on rows the broker just ACCEPTED (the exactly-once edge).

    Re-assigns a NEW dict per row (the JSON column tracks attribute assignment, not in-place
    mutation — the `_common.create_task_deduped` metadata rule), keeping every other key and
    stamping `rearmed_at` so the resume is auditable from the row itself.

    E7 F-04 — the read-modify-write is guarded by a ROW LOCK taken BEFORE the payload is read.
    The merge writes the whole JSON blob, so a read without the lock let any concurrent metadata
    writer that committed between this SELECT and this COMMIT have its keys silently erased
    (JSON is last-write-wins; measured: a second writer's disjoint key vanished).  The no-op
    UPDATE is the dialect-portable lock — PostgreSQL takes the row lock for it and SQLite its
    single writer lock — and `with_for_update()` states the same intent for the re-select that
    follows (it compiles to `FOR UPDATE` on PostgreSQL; SQLite has no such clause and is already
    serialised by the write lock the UPDATE took).  The stamp is captured once, outside, so the
    audit value is the acceptance instant, not the flush instant.
    """
    if not task_ids:
        return
    from sqlalchemy import select, update

    from app.core.database import get_session_factory
    from app.models.task import Task
    from celery_app.tasks.agent_tasks import PAUSED_RELEASE_KEY

    stamp = now_utc().isoformat()
    ids = [int(item) for item in task_ids]
    async with get_session_factory()() as db:
        await db.execute(update(Task).where(Task.id.in_(ids)).values(updated_at=Task.updated_at))
        rows = (
            await db.execute(select(Task).where(Task.id.in_(ids)).with_for_update())
        ).scalars().all()
        for row in rows:
            existing = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            merged = {**existing, "rearmed_at": stamp}
            merged.pop(PAUSED_RELEASE_KEY, None)
            row.metadata_json = merged
        await db.commit()
