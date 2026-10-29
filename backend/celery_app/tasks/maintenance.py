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

`recover_orphaned_deliveries` is the eighth job (F-22, 2026-10-28) and the ONLY one with a
non-Beat production caller: a `worker_ready` hook.  Live incident: the worker prefetched two
`run_agent_task` messages, lost Redis mid-prefetch (TimeoutError) and was SIGKILLed alive-but-
wedged with `unacked=2`; a restart did NOT restore them, because kombu's Redis transport restores
an unacked delivery only from a LIVE worker's event loop and only after `visibility_timeout`
(1800 s here) — and its crash-path restore never ran.  Task 315 was left `in_progress` with its
delivery still in `unacked`, and two `unacked_index` tags had no payload at all (a manual HDEL
left them behind — the ghost shape).  This tick runs the claim-lease sweep, then restores exactly
the deliveries the DB still owes (pending/due, or a claim whose lease expired), DROPS deliveries
whose row is terminal or gone (a redelivery there can only be refused at claim and re-lost), and
LEAVES a live claim's delivery to the transport's visibility timeout.  It dispatches nothing and
publishes nothing: every restored message still passes the claim, pause and budget gates.

`cleanup_run_workspaces` is the seventh job (V27/L2): V26 shipped
`tool_registry.cleanup_expired_run_workspaces` (policy: keep the newest 20 run directories under
`<workspace>/runs/`, max age 72 h) and NOTHING called it, so a run workspace outlived its policy
forever.  This daily tick is the caller: it invokes the registry function with the NAMED defaults
(imported from the registry, never re-declared here), records ONE activity row carrying the counts,
the run names and the policy — never a file's contents and never a path inside a run — and returns
the same shape the owner route serves.  `plan_expired_run_workspaces` is the read-only twin the
route's dry run needs; it imports the registry's constants, its run-id charset and its link guard,
so the preview and the delete cannot disagree about what "expired" means (pinned by
`tests/unit/test_v27_retention.py::test_the_preview_and_the_delete_agree_on_identical_trees`).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from celery.signals import worker_ready

from app.agents.tool_registry import (
    _RUN_ID_RE,
    CODE_WORKSPACE_DEFAULT,
    RUN_RETENTION_KEEP_LATEST,
    RUN_RETENTION_MAX_AGE_HOURS,
    RUN_WORKSPACE_SUBDIR,
    _tree_contains_link,
    cleanup_expired_run_workspaces,
)
from app.config import settings
from app.core import redis_client
from app.core.errors import E_CONFIG, E_VALIDATION, PolsiaError
from app.core.events import publish_stats
from app.core.timeutil import UTC, as_bound_instant, now_utc
from app.services.task_service import requeue_stale_claims
from celery_app.tasks._common import (
    dispatch_paused_reason,
    paused_skip,
    run_async,
)
from celery_app.worker import app

if TYPE_CHECKING:  # annotation-only: the task module keeps its runtime imports lazy
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.report import ActivityLog

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

    RESULT SHAPE (F-W8-TM-3, wave 9): a sweep can do TWO different things and the result names
    each one separately — `recovered` (rows moved out of `in_progress`), `requeued` (back to
    `pending`, i.e. genuinely recoverable work) and `ended` (terminally `failed` at the attempt
    bound).  The wave-8 shape reported the ONE total under the name `requeued`, so a sweep that
    ENDED two crash-looping rows read as `{"requeued": 2}` — a recovery that never happened.
    """
    return run_async(_requeue_stale_claims_in_session())


#: The stale-claim sweep's two outcomes are DIFFERENT facts (F-W8-TM-3).  `measured` means the
#: split below is the DB's own post-sweep state for exactly the ids the sweep considered;
#: `unavailable` means the snapshot and the sweep disagreed, so NO split is reported (never guessed).
SWEEP_OUTCOME_MEASURED = "measured"
SWEEP_OUTCOME_UNAVAILABLE = "unavailable"


def _status_text(value: Any) -> str:
    """A `tasks.status` value as the plain text the write produced (enum or text)."""
    return str(getattr(value, "value", value))


async def _sweep_stale_claims_with_outcomes(db: AsyncSession) -> dict[str, Any]:
    """Run the shipped stale-claim sweep and MEASURE its two outcomes, in the caller's transaction.

    F-W8-TM-3 (found in wave 8, live L4): `requeue_stale_claims` returns ONE number for a pass that
    can (a) put a dead worker's claim back on the queue or (b) END a crash-looping row at the
    attempt bound (`failed`, "lease expired after N attempts").  Reporting that number as
    `requeued` claimed a recovery that never happened: the live sweep said `{"requeued": 2}` while
    tasks 335 and 336 were both written `failed` and none was requeued.

    The service's own RETURNING split is not exposed (its return value is pinned to the count, and
    every shipped caller depends on that), so this wrapper MEASURES the outcome instead of
    inferring it:

      1. snapshot the ids the sweep's own rule selects — the same predicate (status, last sign of
         life vs the same lease) and the same `STALE_CLAIM_BATCH`/id order the service uses;
      2. run the shipped sweep;
      3. read the status the DB now holds for exactly those ids, in the SAME session and
         transaction (the caller commits both together), so no other writer can move them in
         between and a rolled-back sweep cannot leave a split claiming a recovery that never
         committed.

    Fail-closed: if the snapshot and the sweep disagree (the service's rule changed, a heartbeat
    was refreshed inside the window, a row crossed the lease boundary), the split is NOT reported
    as a number — `outcomes` is "unavailable", `requeued`/`ended` are None, and `reason` names the
    disagreement.  A counter that cannot be measured is never guessed.
    """
    from sqlalchemy import func, select

    from app.models.task import Task
    from app.services.task_service import STALE_CLAIM_BATCH

    lease = int(settings.task_claim_lease_seconds)
    cutoff = as_bound_instant(now_utc() - timedelta(seconds=lease), db)
    candidate_ids = [
        int(task_id)
        for task_id in (
            await db.execute(
                select(Task.id)
                .where(
                    Task.status == "in_progress",
                    func.coalesce(Task.heartbeat_at, Task.claimed_at, Task.created_at) < cutoff,
                )
                .order_by(Task.id)
                .limit(STALE_CLAIM_BATCH)
            )
        ).scalars().all()
    ]

    recovered = int(await requeue_stale_claims(db))

    after: dict[int, str] = {}
    if candidate_ids:
        rows = (
            await db.execute(select(Task.id, Task.status).where(Task.id.in_(candidate_ids)))
        ).all()
        after = {int(task_id): _status_text(status) for task_id, status in rows}

    requeued_ids = sorted(task_id for task_id in candidate_ids if after.get(task_id) == "pending")
    ended_ids = sorted(task_id for task_id in candidate_ids if after.get(task_id) == "failed")
    changed_ids = set(requeued_ids) | set(ended_ids)
    # A candidate that is STILL `in_progress` was refreshed (a live heartbeat) between the snapshot
    # and the sweep's own UPDATE, so the sweep did not touch it.  It is reported as `left_ids`, not
    # as an outcome — and it does not break the agreement: the split only has to account for the
    # rows the service says it CHANGED.
    left_ids = [task_id for task_id in candidate_ids if task_id not in changed_ids]
    measured = len(changed_ids) == recovered
    reason = None
    if not measured:
        reason = (
            f"the sweep changed {recovered} row(s) but the measured outcomes account for "
            f"{len(requeued_ids) + len(ended_ids)} (snapshot of {len(candidate_ids)}); the split is "
            "not reported as a number"
        )
    return {
        "recovered": recovered,
        "requeued": len(requeued_ids) if measured else None,
        "ended": len(ended_ids) if measured else None,
        "outcomes": SWEEP_OUTCOME_MEASURED if measured else SWEEP_OUTCOME_UNAVAILABLE,
        "reason": reason,
        "requeued_ids": requeued_ids,
        "ended_ids": ended_ids,
        "left_ids": left_ids,
    }


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
        # The split is measured in this transaction (F-W8-TM-3): `recovered` is every claim moved
        # out of `in_progress`, `requeued` the ones back on the queue, `ended` the ones the attempt
        # ladder terminated — three facts, never one number wearing three names.
        swept = await _sweep_stale_claims_with_outcomes(db)
        await db.commit()

    logger.info(
        "task-lease sweep: recovered %s of %s in-flight claim(s) — %s back to pending, %s ended "
        "at the attempt bound (outcomes=%s)",
        swept["recovered"],
        scanned,
        "?" if swept["requeued"] is None else swept["requeued"],
        "?" if swept["ended"] is None else swept["ended"],
        swept["outcomes"],
    )
    if swept["outcomes"] != SWEEP_OUTCOME_MEASURED:
        logger.warning("task-lease sweep: outcome split unavailable — %s", swept["reason"])
    return {"status": "completed", "scanned": scanned, **swept}


# ── F-22: the broker's orphaned deliveries (no Beat required) ─────────────────────────────────
#: One recovery pass inspects at most this many unacked deliveries (the F-51 bounded-sweep rule:
#: a bound is announced, never silently truncated — the next pass continues).
DELIVERY_RECOVERY_BATCH = 200
#: The activity action ONE pass writes when it changed anything.  A pass that changed nothing
#: writes nothing (the F-51/H-02 rule: an activity row must describe a real change).
DELIVERY_RECOVERY_ACTION = "broker_delivery_recovery"
#: The named reason a pass reports when the broker could not be reached.  It is a SKIP, not a
#: failure: a worker started before Redis must still become ready (`broker_connection_retry_on_startup`).
DELIVERY_RECOVERY_UNAVAILABLE = "E_REDIS"


@app.task(name="celery_app.tasks.maintenance.recover_orphaned_deliveries")
def recover_orphaned_deliveries() -> dict[str, Any]:
    """F-22 — get the broker's unacked deliveries and the DB's claims back in agreement.

    Callers: the WORKER STARTUP hook below (always) and any operator/Beat tick that wants a pass.
    It is bookkeeping, never dispatch: no row is created, no message is published, no agent runs.
    The one spend-adjacent act is RESTORING a delivery the DB still owes — and that message then
    goes through the same claim, pause gate and budget gate as every other delivery, so a paused
    company's restored message is released by name instead of spending.
    """
    return run_async(_recover_orphaned_deliveries())


def _aware_utc(value: datetime | None) -> datetime | None:
    """A row timestamp as an aware UTC instant (SQLite stores naive UTC wall clocks)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _recover_orphaned_deliveries(*, broker: Any | None = None) -> dict[str, Any]:
    """The async body of `recover_orphaned_deliveries`; `broker` is injectable for tests."""
    from sqlalchemy import func, select

    from app.core.database import get_session_factory
    from app.models.task import Task
    from celery_app.tasks.broker_recovery import (
        KombuRedisDeliveryBroker,
        RecoveryResult,
    )

    result = RecoveryResult(bound=DELIVERY_RECOVERY_BATCH)
    session_factory = get_session_factory()

    # 1. The claim-lease sweep FIRST: a stale claim becomes `pending` (and carries the reconciler's
    #    `rearmed_at` stamp) before its delivery is put back, so the redelivery claims a row it is
    #    allowed to take instead of being refused and burning the message.  This is the same
    #    bookkeeping sweep Beat runs — it spends nothing and is safe while the company is paused
    #    (pinned by `test_f2_feed2_paused_beat`), which is exactly why it is the right first step
    #    on a stack whose Beat is off by design.
    try:
        async with session_factory() as db:
            result.reaped_scanned = int(
                await db.scalar(
                    select(func.count()).select_from(Task).where(Task.status == "in_progress")
                )
            )
            # F-W8-TM-3: the pass reports the sweep's TWO outcomes separately, so a restored
            # delivery is never confused with a row the attempt ladder just ended.
            swept = await _sweep_stale_claims_with_outcomes(db)
            await db.commit()
        result.reaped_recovered = int(swept["recovered"])
        result.reaped_requeued = swept["requeued"]
        result.reaped_ended = swept["ended"]
        result.reaped_outcomes = str(swept["outcomes"])
        result.reaped_reason = swept["reason"]
    except Exception as exc:  # noqa: BLE001 — a DB outage must not stop the delivery decision
        result.errored.append({"step": "lease_sweep", "error": exc.__class__.__name__})
        logger.warning("delivery recovery: lease sweep failed (%s)", exc.__class__.__name__,
                       exc_info=True)

    own_broker = broker is None
    if own_broker:
        try:
            broker = KombuRedisDeliveryBroker()
        except Exception as exc:  # noqa: BLE001 — a worker started before Redis is not a crash
            result.status = "skipped"
            result.reason = DELIVERY_RECOVERY_UNAVAILABLE
            result.errored.append({"step": "broker_connect", "error": exc.__class__.__name__})
            logger.warning(
                "delivery recovery: broker unavailable (%s); no delivery was inspected and "
                "nothing was mutated — a redelivery stays owned by the transport's visibility "
                "timeout until a pass can reach Redis",
                exc.__class__.__name__,
            )
            return result.as_dict()

    # The unavailable-broker branch above returns; reaching here means a broker exists.
    # Stated explicitly so the rest of the pass (and the finally close) is provably non-None;
    # mypy flagged these eight union-attr reads in the wave-2 recovery path.
    if broker is None:
        result.status = "skipped"
        result.reason = DELIVERY_RECOVERY_UNAVAILABLE
        return result.as_dict()

    try:
        deliveries = broker.snapshot()
        result.scanned = len(deliveries)
        if len(deliveries) > DELIVERY_RECOVERY_BATCH:
            result.bound_reached = True
            logger.warning(
                "delivery recovery: %s unacked deliveries, inspecting the first %s (oldest "
                "delivery first); the next pass continues",
                len(deliveries),
                DELIVERY_RECOVERY_BATCH,
            )
            deliveries = deliveries[:DELIVERY_RECOVERY_BATCH]

        managed = [delivery for delivery in deliveries if delivery.managed]
        rows: dict[int, Any] = {}
        if managed:
            task_ids = sorted({int(delivery.task_id) for delivery in managed
                               if delivery.task_id is not None})
            async with session_factory() as db:
                rows = {
                    int(task.id): task
                    for task in (
                        await db.execute(select(Task).where(Task.id.in_(task_ids)))
                    ).scalars().all()
                }

        now = now_utc()
        lease_seconds = int(settings.task_claim_lease_seconds)
        terminal = {"completed", "failed", "cancelled", "rejected"}
        for delivery in deliveries:
            if not delivery.has_payload:
                # An `unacked_index` entry with no `unacked` field: the message is GONE (a manual
                # HDEL leaves exactly this — the live capture had two).  The index entry is a
                # phantom delivery: drop it so a later visibility-timeout restore cannot resurrect
                # nothing and so the index cannot grow without bound.
                broker.drop(delivery.tag)
                result.dropped.append(f"{delivery.tag}:index-only")
                continue
            if not delivery.managed:
                result.left_unmanaged.append(f"{delivery.tag}:{delivery.task_name or 'undecoded'}")
                continue
            task = rows.get(int(delivery.task_id))
            if task is None:
                # The row is gone: nobody owes a run.  Dropping the delivery is the only action
                # that cannot resurrect work a data decision deleted.
                broker.drop(delivery.tag)
                result.dropped.append(f"{delivery.tag}:task-{delivery.task_id}:missing-row")
                continue
            status = str(task.status or "")
            if status in terminal:
                # A ghost delivery: the work already ended.  Dropping it is safe AND is what stops
                # a redelivery from being refused at claim and re-lost (the "ghost double-run"
                # half of F-22: the claim would refuse it, so it must not be requeued at all).
                broker.drop(delivery.tag)
                result.dropped.append(f"{delivery.tag}:task-{delivery.task_id}:{status}")
                continue
            if status == "pending":
                due = _aware_utc(task.scheduled_date)
                if due is not None and due > now:
                    # `claim_task` refuses a future `scheduled_date` (E_TASK_NOT_RUNNABLE), so
                    # restoring now would burn the only delivery on a refusal.  The cadence owns
                    # this row when it is due; the row itself stays pending.
                    broker.drop(delivery.tag)
                    result.dropped.append(f"{delivery.tag}:task-{delivery.task_id}:not-due")
                    continue
                broker.restore(delivery.tag)
                result.restored.append(int(delivery.task_id))
                continue
            if status == "in_progress":
                last_seen = _aware_utc(task.heartbeat_at or task.claimed_at or task.created_at)
                if last_seen is not None and (now - last_seen).total_seconds() < lease_seconds:
                    # A worker that may still be alive owns this row.  The transport's visibility
                    # timeout is the correct owner of that case — restoring now would race a live
                    # run for nothing.
                    result.left_live_claim.append(int(delivery.task_id))
                    continue
                broker.restore(delivery.tag)
                result.restored.append(int(delivery.task_id))
                continue
            # Any other row state (a NULL status is schema-legal): leave the delivery untouched and
            # name it — an unknown state is a finding, not a licence to guess.
            result.left_unmanaged.append(
                f"{delivery.tag}:task-{delivery.task_id}:status-{status or 'null'}"
            )

        if result.changed:
            from app.services.activity_service import log_activity

            async with session_factory() as db:
                await log_activity(
                    db,
                    agent_type="orchestrator",
                    action=DELIVERY_RECOVERY_ACTION,
                    summary=(
                        f"broker delivery recovery: restored {len(result.restored)} orphaned "
                        f"delivery(ies), dropped {len(result.dropped)} ghost delivery(ies), left "
                        f"{len(result.left_live_claim)} live claim(s) to the visibility timeout"
                    ),
                    level="warning" if result.dropped else "info",
                    detail={
                        "scanned": result.scanned,
                        "restored": result.restored,
                        "dropped": result.dropped,
                        "left_live_claim": result.left_live_claim,
                        "reaped_requeued": result.reaped_requeued,
                        "reaped_recovered": result.reaped_recovered,
                        "reaped_ended": result.reaped_ended,
                        "reaped_outcomes": result.reaped_outcomes,
                        "bound": result.bound,
                    },
                )
                await db.commit()
        logger.info(
            "delivery recovery: scanned=%s restored=%s dropped=%s live_left=%s "
            "claim_sweep=%s/%s requeued=%s ended=%s outcomes=%s",
            result.scanned,
            result.restored,
            result.dropped,
            result.left_live_claim,
            result.reaped_recovered,
            result.reaped_scanned,
            result.reaped_requeued,
            result.reaped_ended,
            result.reaped_outcomes,
        )
        return result.as_dict()
    except Exception as exc:  # noqa: BLE001 — report what was done; the next pass is idempotent
        result.status = "partial"
        result.reason = exc.__class__.__name__
        result.errored.append({"step": "delivery_pass", "error": exc.__class__.__name__})
        logger.warning("delivery recovery: pass failed (%s)", exc.__class__.__name__, exc_info=True)
        return result.as_dict()
    finally:
        if own_broker and broker is not None:
            try:
                broker.close()
            except Exception:  # noqa: BLE001 — closing a probe connection cannot fail the pass
                logger.debug("delivery recovery: closing the broker connection failed",
                             exc_info=True)


#: One startup pass per worker process.  The signal can fire once per consumer start; the recovery
#: is idempotent, but one pass per process start is the honest unit (the log line is the evidence).
_STARTUP_RECOVERY_DONE = False


@worker_ready.connect
def _recover_deliveries_on_worker_startup(sender: Any = None, **kwargs: Any) -> None:
    """F-22's production caller: a restarted worker recovers its orphaned deliveries IMMEDIATELY.

    Why a worker-startup hook rather than a Beat entry: this wave's posture is Beat OFF, and the
    live incident proved the worker's own restart is when a stranded delivery must be dealt with.
    The transport would restore it only after `visibility_timeout` (1800 s) and only from a live
    worker's event loop; a `SIGKILL`-ed worker restores nothing at close.  This hook runs after the
    consumer is ready, before any redelivery is owed, and never raises into the worker.
    """
    global _STARTUP_RECOVERY_DONE
    if _STARTUP_RECOVERY_DONE:
        return
    _STARTUP_RECOVERY_DONE = True
    try:
        result = run_async(_recover_orphaned_deliveries())
        logger.info("worker-startup delivery recovery: %s", result)
    except Exception:  # noqa: BLE001 — a recovery failure must never stop a worker from starting
        logger.warning(
            "worker-startup delivery recovery failed; the next worker start (and the transport's "
            "visibility timeout) still cover the orphaned deliveries",
            exc_info=True,
        )
    # F-26 (wave 3): the SAME reason this hook exists for deliveries applies to the ledger rows the
    # dead process was writing — `close_stale_agent_runs` is a Beat task and Beat is OFF, so before
    # this line an interrupted run stayed `running` for the life of the deployment (live:
    # `agent_runs` 311 and 314, since 21:07/21:11Z).  A restarted worker IS the evidence that the
    # previous process is gone, so it closes the rows that process abandoned, with the named
    # reason and the interruption marker the sweep writes.  No spend: the sweep only writes the
    # ledger; the stale bound keeps a genuinely live run (another worker) untouched.
    try:
        closed = run_async(_close_stale_runs_in_session())
        logger.info("worker-startup stale-run sweep: %s", closed)
    except Exception:  # noqa: BLE001 — ledger hygiene must never stop a worker from starting
        logger.warning(
            "worker-startup stale-run sweep failed; the rows stay `running` until the next worker "
            "start or a Beat tick (the sweep is idempotent)",
            exc_info=True,
        )


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

    # F2-FEED-2 (2026-10-28): while the owner has pressed STOP this reconciler hands NOTHING to the
    # broker.  This is the function that made the live unattended spend measurable: at
    # 2026-10-28 19:30:06 UTC a queue tick already in the broker re-dispatched four stranded rows
    # (`redispatched: 4`) and the restarted worker ran them — real DeepSeek calls, agent_runs
    # 295-298, $0.0029, with no owner action.  A paused company must not re-feed the dispatcher;
    # rows stay `pending` and are re-dispatched by the next tick after resume (nothing is lost).
    async with session_factory() as db:
        paused_reason = await dispatch_paused_reason(db)
    if paused_reason is not None:
        result = paused_skip(
            "stranded-pending-reconciler",
            paused_reason,
            scanned=0,
            redispatched=0,
            failed=[],
            skipped_recent=[],
            task_ids=[],
            rearmed_ids=[],
            window_seconds=PENDING_RECONCILE_AFTER_SECONDS,
        )
        logger.warning("pending reconciler: %s", result)
        return result

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

    F-26 (wave 3): the row also gets an explicit INTERRUPTION record in `usage_json`
    (`interrupted.cost_scope`).  Since the provider loop now persists cumulative usage while the
    run is live (`record_run_usage_progress`), an interrupted row usually HAS a real measured cost
    — but only up to the kill, so it is a LOWER BOUND, not the run's total.  The marker is what
    lets a reader tell that apart from a run whose cost is complete, and from one where nothing
    was ever measured (`cost_scope="unmeasured"`, cost stays NULL — an unknown, never a 0).

    A `running` row with NO `started_at` is closed too, with the missing timestamp NAMED: it
    can never become measurable later, and an immortal `running` row is the defect itself.
    Ordered by id, one batched activity row per sweep, and idempotent by construction — a
    closed row is no longer `running`, so the next tick scans past it.
    """
    from sqlalchemy import or_, select

    from app.core.database import get_session_factory
    from app.models.task import AgentRun
    from app.services.activity_service import log_activities
    from app.services.task_service import (
        IN_FLIGHT_USAGE_KEY,
        RUN_COST_SCOPE_KEY,
        RUN_INTERRUPTED_KEY,
        finish_agent_run,
    )

    session_factory = get_session_factory()
    cutoff = now_utc() - timedelta(seconds=AGENT_RUN_STALE_AFTER_SECONDS)

    closed: list[int] = []
    undated: list[int] = []
    cost_partial: list[int] = []
    cost_unmeasured: list[int] = []
    scanned = 0
    async with session_factory() as db:
        bound = as_bound_instant(cutoff, db)
        rows = (
            await db.execute(
                select(
                    AgentRun.id,
                    AgentRun.started_at,
                    AgentRun.usage_json,
                    AgentRun.cost_usd,
                )
                .where(
                    AgentRun.status == "running",
                    or_(AgentRun.started_at.is_(None), AgentRun.started_at < bound),
                )
                .order_by(AgentRun.id)
            )
        ).all()
        scanned = len(rows)
        for raw_id, started_at, usage_json, cost_usd in rows:
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
                    "(duration left exactly as recorded; cost is what the live run measured)"
                )
            existing = usage_json if isinstance(usage_json, dict) else {}
            in_flight = existing.get(IN_FLIGHT_USAGE_KEY)
            in_flight_block = in_flight if isinstance(in_flight, dict) else {}
            measured_cost = cost_usd is not None
            cost_scope = "in_flight_partial" if measured_cost else "unmeasured"
            merged = dict(existing)
            merged[RUN_COST_SCOPE_KEY] = cost_scope
            merged[RUN_INTERRUPTED_KEY] = {
                "at": now_utc().isoformat(),
                "bound_seconds": AGENT_RUN_STALE_AFTER_SECONDS,
                "reason_code": STALE_RUN_REASON_CODE,
                "cost_scope": cost_scope,
                "usage_source": "in_flight" if in_flight_block else "none",
                "provider_calls_persisted": in_flight_block.get("provider_calls"),
            }
            if measured_cost:
                cost_partial.append(run_id)
            else:
                cost_unmeasured.append(run_id)
            run_row = await finish_agent_run(
                db, run_id, "cancelled", error_message=reason, usage_json=merged
            )
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
            if cost_partial:
                parts.append(
                    f"{len(cost_partial)} carry a PARTIAL measured cost (spent up to the "
                    "interruption)"
                )
            if cost_unmeasured:
                parts.append(f"{len(cost_unmeasured)} have UNMEASURED cost (null, not zero)")
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
                            "cost_partial_ids": cost_partial,
                            "cost_unmeasured_ids": cost_unmeasured,
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
        "cost_partial_ids": cost_partial,
        "cost_unmeasured_ids": cost_unmeasured,
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


# ---------------------------------------------------------------------------
# run-workspace retention (V27/L2) — the daily caller for the V26 cleanup
#
# The POLICY lives in `app/agents/tool_registry.cleanup_expired_run_workspaces` (keep the newest
# `RUN_RETENTION_KEEP_LATEST` = 20 run directories, max age `RUN_RETENTION_MAX_AGE_HOURS` = 72 h).
# This module never re-declares it: the delete path CALLS that function, and the preview path
# imports its constants, its run-id charset and its link guard.  The two paths can therefore only
# disagree if the registry's own rule changes under both of them — and
# `test_the_preview_and_the_delete_agree_on_identical_trees` measures the agreement on identical
# trees, so a drift is a failing test rather than a wrong delete.
# ---------------------------------------------------------------------------

#: The activity row's action — the /ops panel's "last result" reads exactly this.
RUN_WORKSPACE_CLEANUP_ACTION = "run_workspace_cleanup"
#: The agent_type on the row: this is system maintenance, not agent work.
RUN_WORKSPACE_CLEANUP_AGENT = "system"
#: How many NAMES an activity row carries.  Counts are always exact; a mass expiry keeps the row
#: bounded (`removed_truncated` / `skipped_truncated` say so) instead of writing an unbounded blob.
RUN_WORKSPACE_CLEANUP_NAME_CAP = 50


def _runs_dir_for(workspace_root: str | Path | None) -> Path:
    """`<workspace>/runs`, resolved — the SAME base the registry function resolves at call time.

    Read from `settings.code_workspace_dir` when `workspace_root` is None (the V26 function's own
    at-call-time rule; tests and the route's drill seam pass a temp root instead).  A root that
    cannot be resolved is the registry's `E_CONFIG` class, named here with the same remedy.
    """
    if workspace_root is None:
        raw = getattr(settings, "code_workspace_dir", CODE_WORKSPACE_DEFAULT)
        text = raw.strip() if isinstance(raw, str) else ""
        candidate = Path(text or CODE_WORKSPACE_DEFAULT).expanduser()
    else:
        if not isinstance(workspace_root, str | Path):
            raise PolsiaError(
                E_VALIDATION,
                f"run workspace cleanup: workspace_root must be a path string "
                f"(got {type(workspace_root).__name__})",
                how_to_fix="pass a directory path, or omit workspace_root for the configured one",
                context={"workspace_root": repr(workspace_root)[:200]},
            )
        candidate = Path(workspace_root).expanduser()
    try:
        base = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise PolsiaError(
            E_CONFIG,
            f"run workspace cleanup: the workspace root {str(candidate)!r} cannot be resolved "
            f"({type(exc).__name__}: {exc})",
            how_to_fix="set CODE_WORKSPACE_DIR to a directory this process can read",
            context={"workspace_root": str(candidate)},
        ) from exc
    return base / RUN_WORKSPACE_SUBDIR


def plan_expired_run_workspaces(
    *, workspace_root: str | Path | None = None, now: float | None = None
) -> dict[str, Any]:
    """The V26 policy PREVIEWED — the same candidates, the same skips, zero deletions.

    `cleanup_expired_run_workspaces` has one behaviour: delete.  A dry run therefore cannot call
    it, and the alternative — a second set of keep/age numbers — is exactly what this lane forbids.
    This function walks the same immediate children with the SAME imported constants, the SAME
    `_RUN_ID_RE` charset and the SAME `_tree_contains_link` guard, and answers the same
    `{runs_dir, removed, count, kept, skipped, policy}` shape with `removed` meaning "would be
    removed".  `run_workspace_cleanup(apply=False)` is its only caller.

    Never raises for an absent `runs/` (an empty answer, not an error) and refuses a linked
    `runs/` outright — the registry's own two shapes, so a preview can never claim a delete that
    the applied pass would refuse to perform.
    """
    clock = time.time() if now is None else float(now)
    policy: dict[str, object] = {
        "keep_latest": RUN_RETENTION_KEEP_LATEST,
        "max_age_hours": float(RUN_RETENTION_MAX_AGE_HOURS),
    }
    runs_dir = _runs_dir_for(workspace_root)
    empty: dict[str, object] = {
        "runs_dir": str(runs_dir), "removed": [], "count": 0, "kept": 0, "skipped": [],
        "policy": policy,
    }
    if not runs_dir.exists():
        return empty

    is_junction = getattr(os.path, "isjunction", None)
    if runs_dir.is_symlink() or (callable(is_junction) and is_junction(runs_dir)):
        raise PolsiaError(
            E_VALIDATION,
            f"run workspace cleanup: {str(runs_dir)!r} is a link or junction — refusing to "
            "preview or delete anything through it",
            how_to_fix="remove the link under the workspace root by hand, then retry",
            context={"runs_dir": str(runs_dir)},
        )
    if not runs_dir.is_dir():
        raise PolsiaError(
            E_CONFIG,
            f"run workspace cleanup: {str(runs_dir)!r} exists but is not a directory",
            how_to_fix="point CODE_WORKSPACE_DIR at a directory (or remove the file in the way)",
            context={"runs_dir": str(runs_dir)},
        )

    candidates: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    with os.scandir(runs_dir) as entries:
        for entry in entries:
            try:
                if entry.is_symlink() or (callable(is_junction) and is_junction(entry.path)):
                    skipped.append({"name": entry.name, "reason": "link"})
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    skipped.append({"name": entry.name, "reason": "not_a_directory"})
                    continue
                if not _RUN_ID_RE.match(entry.name):
                    skipped.append({"name": entry.name, "reason": "invalid_name"})
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:  # an unreadable entry is never previewed as deletable
                skipped.append(
                    {"name": entry.name, "reason": f"stat_failed:{type(exc).__name__}"}
                )
                continue
            candidates.append(
                {"name": entry.name, "path": Path(entry.path), "mtime": float(stat.st_mtime)}
            )

    newest_first = sorted(
        candidates, key=lambda item: (item["mtime"], item["name"]), reverse=True
    )
    protected = {str(item["name"]) for item in newest_first[:RUN_RETENTION_KEEP_LATEST]}
    cutoff = clock - float(RUN_RETENTION_MAX_AGE_HOURS) * 3600.0
    resolved_runs = runs_dir.resolve()
    removed: list[str] = []
    for item in newest_first:
        name = str(item["name"])
        path = item["path"]
        assert isinstance(path, Path)  # populated above; mypy cannot read the dict shape
        if name in protected:
            continue
        mtime = item["mtime"]
        assert isinstance(mtime, int | float)  # populated above; mypy cannot read the dict shape
        if float(mtime) >= cutoff:
            continue
        try:
            resolved = path.resolve()
        except (OSError, ValueError) as exc:
            skipped.append({"name": name, "reason": f"resolve_failed:{type(exc).__name__}"})
            continue
        if resolved == resolved_runs or resolved_runs not in resolved.parents:
            skipped.append({"name": name, "reason": "outside_runs"})
            continue
        if _tree_contains_link(resolved):
            skipped.append({"name": name, "reason": "contains_link"})
            continue
        removed.append(name)

    return {
        "runs_dir": str(runs_dir),
        "removed": removed,
        "count": len(removed),
        "kept": len(candidates) - len(removed),
        "skipped": skipped,
        "policy": policy,
    }


def run_workspace_cleanup(
    *, apply: bool, workspace_root: str | Path | None = None, now: float | None = None
) -> dict[str, Any]:
    """Preview (`apply=False`) or delete (`apply=True`) under `<workspace>/runs/`.

    The applied path is the V26 registry function itself, called with the NAMED defaults imported
    from it; the preview path is `plan_expired_run_workspaces`.  `applied` states which happened —
    the caller never has to infer it from the counts.  Blocks on the filesystem; the route calls
    it off the event loop (`run_in_threadpool`).
    """
    if not isinstance(apply, bool):
        raise PolsiaError(
            E_VALIDATION,
            f"run workspace cleanup: apply must be a boolean (got {type(apply).__name__})",
            how_to_fix="pass apply=true to delete, apply=false (or omit) to preview",
            context={"apply": repr(apply)[:40]},
        )
    if apply:
        outcome = cleanup_expired_run_workspaces(
            workspace_root=workspace_root,
            keep_latest=RUN_RETENTION_KEEP_LATEST,
            max_age_hours=RUN_RETENTION_MAX_AGE_HOURS,
        )
    else:
        outcome = plan_expired_run_workspaces(workspace_root=workspace_root, now=now)

    # Both producers answer the registry's own keys; the casts name that contract for mypy
    # (`cleanup_expired_run_workspaces` returns `dict[str, object]`).  A missing key here is a
    # broken producer and must fail loudly, not be defaulted into a fabricated zero.
    removed_raw = cast(list[object], outcome["removed"])
    skipped_raw = cast(list[dict[str, str]], outcome["skipped"])
    policy_raw = cast(dict[str, object], outcome["policy"])
    removed = [str(name) for name in removed_raw]
    skipped = [{"name": str(item["name"]), "reason": str(item["reason"])} for item in skipped_raw]
    cap = RUN_WORKSPACE_CLEANUP_NAME_CAP
    result: dict[str, Any] = {
        "runs_dir": str(outcome["runs_dir"]),
        "removed": removed[:cap],
        "count": int(cast(int, outcome["count"])),
        "kept": int(cast(int, outcome["kept"])),
        "skipped": skipped[:cap],
        "policy": {
            "keep_latest": int(cast(int, policy_raw["keep_latest"])),
            "max_age_hours": float(cast(float, policy_raw["max_age_hours"])),
        },
        "applied": apply,
    }
    # The SAME 50-name cap the activity row applies, plus the flags that SAY SO, so the POST body
    # and `GET .../run-workspace-cleanup` cannot disagree about whether a list is complete.  A
    # flag is present only when its list was actually cut: absent means complete, never `false`
    # (V29/L1 T1.4).
    if len(removed) > cap:
        result["removed_truncated"] = True
    if len(skipped) > cap:
        result["skipped_truncated"] = True
    return result


async def record_run_workspace_cleanup(
    db: AsyncSession, outcome: dict[str, Any], *, trigger: str
) -> ActivityLog:
    """Write the ONE activity row for a cleanup: counts, run names and the policy.

    Names only — the row is on the agent-visible feed, so it carries no file's contents and no
    path INSIDE a run directory.  `runs_dir` is the operation's scope (a directory), the same value
    the route returns.  The list is capped at `RUN_WORKSPACE_CLEANUP_NAME_CAP` with an explicit
    `*_truncated` flag, so a mass expiry stays bounded without a silent truncation.
    """
    from app.services.activity_service import log_activity

    removed = [str(name) for name in outcome["removed"]]
    skipped = [dict(item) for item in outcome["skipped"]]
    cap = RUN_WORKSPACE_CLEANUP_NAME_CAP
    policy = outcome["policy"]
    applied = bool(outcome["applied"])
    detail: dict[str, Any] = {
        "runs_dir": str(outcome["runs_dir"]),
        "count": int(outcome["count"]),
        "kept": int(outcome["kept"]),
        "removed": removed[:cap],
        "skipped": skipped[:cap],
        "policy": {
            "keep_latest": int(policy["keep_latest"]),
            "max_age_hours": float(policy["max_age_hours"]),
        },
        "applied": applied,
        "trigger": trigger,
    }
    if len(removed) > cap:
        detail["removed_truncated"] = True
    if len(skipped) > cap:
        detail["skipped_truncated"] = True
    # `run_workspace_cleanup` already applied the cap, so its flags (or the count itself) carry
    # the truth: a capped list of 50 with a count of 73 is truncated even though `len <= cap`
    # (V29/L1 T1.4 — the flags must survive a producer that capped before handing them over).
    if bool(outcome.get("removed_truncated")):
        detail["removed_truncated"] = True
    if bool(outcome.get("skipped_truncated")):
        detail["skipped_truncated"] = True

    total = detail["count"] + detail["kept"]
    summary = (
        f"run workspace cleanup ({'applied' if applied else 'dry run'}): "
        f"{detail['count']} of {total} run workspace(s) "
        f"{'removed' if applied else 'would be removed'}; "
        f"keep newest {detail['policy']['keep_latest']}, "
        f"max age {detail['policy']['max_age_hours']:g} h"
    )
    return await log_activity(
        db,
        RUN_WORKSPACE_CLEANUP_AGENT,
        RUN_WORKSPACE_CLEANUP_ACTION,
        summary=summary,
        detail=detail,
    )


@app.task(name="celery_app.tasks.maintenance.cleanup_run_workspaces")
def cleanup_run_workspaces() -> dict[str, Any]:
    """Daily (V27/L2) — apply the run-workspace retention policy and record it.

    The beat entry is `run-workspace-cleanup` in `celery_app/beat_schedule.py`.  The registry
    function does the deletion (never re-implemented here); the activity row is written in the
    SAME call, so "it ran" and "it is on the feed" cannot diverge.
    """
    return run_async(_cleanup_run_workspaces())


async def _cleanup_run_workspaces() -> dict[str, Any]:
    from app.core.database import get_session_factory

    outcome = run_workspace_cleanup(apply=True)
    async with get_session_factory()() as db:
        entry = await record_run_workspace_cleanup(db, outcome, trigger="beat")
        activity_id = int(entry.id)
        await db.commit()

    result = {**outcome, "status": "completed", "activity_id": activity_id}
    logger.info(
        "run workspace cleanup: applied=%s removed=%s kept=%s runs_dir=%s",
        result["applied"],
        result["count"],
        result["kept"],
        result["runs_dir"],
    )
    return result
