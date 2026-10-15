"""The outreach follow-up sweep — beat every 3 h (V9/B3, plan §9 B3 474-490).

The chain, end to end: `outreach_service.record_sent_emails` writes `email_logs.follow_up_due`
(`sent_at` + the output's earliest `due_in_days`); THIS tick reads the due rows and turns each
into a deduped `email_outreach` task; an answered prospect never sees one, because
`support_inbox_service.submit_item` stamps `replied_at` on every unstamped log of the matched
prospect and the selection below filters on `replied_at IS NULL`.

The tick, exactly:

* fires every 3 h — beat entry `email-followups` (`crontab(minute=0, hour="*/3")`, `expires`
  one full period: a tick delivered after its own slot collapses into the next one, the
  periodic-sweep rule every entry in `beat_schedule.py` follows);
* selects `email_logs WHERE follow_up_due <= now AND replied_at IS NULL AND status = 'sent'`,
  oldest due first (`follow_up_due ASC, id ASC` — deterministic on both dialects; the bound
  goes through `as_bound_instant`, the claim path's dialect rule);
* creates ONE task per PROSPECT per tick, the NEWEST-angle rule stated for the agent in the
  task description.  A prospect with several due steps gets the OLDEST step whose dedupe task
  does not exist yet — dedupe key **`followup:<email_logs.id>`**, the V9-frozen shape — so a
  step that already fired can never fire twice (the key is the log row's identity, re-checked
  every tick: a re-delivered tick and a plain re-run both answer `deduped`, never a second
  task), and a later step is never starved by an older one that cannot re-fire;
* child rows: agent `email_outreach`, source `scheduler`, priority 3, carrying
  `metadata.follow_up` = `{log_id, prospect_email, step}` — `step` is the due log's
  `sequence_step` + 1, i.e. the NEXT touch after the one that got no reply (a NULL
  `sequence_step` is the column's own 1, so the follow-up is step 2);
* commits, then hands the ids to the broker with the shared `enqueue_created` (a dead broker
  marks the rows failed and re-raises — nothing is reported queued that was not);
* nothing due -> nothing happens: no task, no activity row, no broker contact.

Registry note (settle wiring, recorded in the wave STATE.md): `celery_config.TASK_MODULES`
and `worker.py`'s eager-import list are the two mechanisms that load a task module in every
worker/beat process; both are outside this seat's write territory, so the two additive lines
for THIS module are documented there for the parent's settle patch — the same requirement
`night_task` and `cadence` carry in their own registry comments.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.timeutil import as_bound_instant, now_utc
from celery_app.tasks._common import create_task_deduped, enqueue_created, run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

#: The per-LOG dedupe key prefix (`followup:<email_logs.id>`) — frozen by the V9 plan.
DEDUPE_KEY_PREFIX = "followup:"

#: Sweep period, stated once (the beat entry's `expires` is one full period of this).
SWEEP_PERIOD_HOURS = 3


def follow_up_dedupe_key(log_id: int) -> str:
    """`followup:<log id>` — the key that makes one step fire at most once, ever."""
    return f"{DEDUPE_KEY_PREFIX}{int(log_id)}"


@app.task(name="celery_app.tasks.followups.run_follow_up_sweep")
def run_follow_up_sweep() -> dict[str, Any]:
    """Every-3h beat tick — one follow-up task per prospect with a due, unanswered step."""
    return run_async(_run_follow_up_sweep())


async def _run_follow_up_sweep() -> dict[str, Any]:
    """The async body of `run_follow_up_sweep` (runs on the process loop)."""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.outreach import EmailLog, Prospect
    from app.models.task import Task

    session_factory = get_session_factory()
    moment = now_utc()

    created_ids: list[int] = []
    deduped = 0
    due_count = 0
    async with session_factory() as db:
        due = (
            await db.execute(
                select(EmailLog, Prospect.email)
                .join(Prospect, Prospect.id == EmailLog.prospect_id)
                .where(
                    EmailLog.follow_up_due.is_not(None),
                    EmailLog.follow_up_due <= as_bound_instant(moment, db),
                    EmailLog.replied_at.is_(None),
                    EmailLog.status == "sent",
                )
                .order_by(EmailLog.follow_up_due.asc(), EmailLog.id.asc())
            )
        ).all()
        due_count = len(due)
        handled: set[int] = set()  # ONE task per prospect per tick
        for log, prospect_email in due:
            if log.prospect_id in handled:
                continue
            key = follow_up_dedupe_key(int(log.id))
            existing = await db.scalar(select(Task.id).where(Task.dedupe_key == key).limit(1))
            if existing is not None:
                # This step already has its task (an earlier tick fired it): nothing to do —
                # and the prospect is NOT marked handled, because a LATER due step that never
                # had a task may still deserve one this tick.
                deduped += 1
                continue
            step = int(log.sequence_step or 1) + 1
            task = await create_task_deduped(
                db,
                title=f"Follow up with {prospect_email} (step {step})",
                agent_type="email_outreach",
                dedupe_key=key,
                description=(
                    f"Write the step-{step} follow-up to {prospect_email}: reply in the same "
                    'thread with a NEW angle (never "just checking in"), subject 60 chars or '
                    "fewer and truthful, one binary CTA."
                ),
                source="scheduler",
                metadata={
                    "follow_up": {
                        "log_id": int(log.id),
                        "prospect_email": prospect_email,
                        "step": step,
                    }
                },
            )
            handled.add(int(log.prospect_id))
            if task is None:
                # Lost the same-key race to a concurrent tick: the row exists, count it.
                deduped += 1
            else:
                created_ids.append(int(task.id))
        await db.commit()

    # Broker hand-off AFTER the commit (the shared scheduler contract): a dead broker marks
    # the rows failed inside `enqueue_created` and re-raises — never a fake queue.
    if created_ids:
        await enqueue_created(created_ids, label="outreach follow-ups")
        # F1 (V11/W2): stamp the `dispatched_at` marker on broker acceptance — the
        # stranded-pending reconciler (`maintenance`) reads it (see cadence.py, same rule).
        from celery_app.tasks.maintenance import mark_tasks_dispatched

        await mark_tasks_dispatched(created_ids)

    result: dict[str, Any] = {
        "status": "completed" if due_count else "no_due",
        "due": due_count,
        "created": len(created_ids),
        "deduped": deduped,
        "task_ids": created_ids,
    }
    logger.info("outreach follow-up sweep: %s", result)
    return result


__all__ = [
    "DEDUPE_KEY_PREFIX",
    "SWEEP_PERIOD_HOURS",
    "follow_up_dedupe_key",
    "run_follow_up_sweep",
]
