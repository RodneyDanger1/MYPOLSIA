"""The task cadence — JUST-IN-TIME children for due `task_schedules` rows (wave V8, C2).

What this is, stated honestly:

* Beat fires **every 15 min** and the tick is a no-op unless something is due.  For every ENABLED
  `task_schedules` row whose `next_run_at` is due (`<= now`), it creates exactly ONE child task
  (title = the schedule's name, description = its prompt, agent `orchestrator`, source
  `scheduler`) through `_common.create_task_deduped` with the frozen dedupe key
  `schedule:<id>:<iso-date-hour>` — the ISO date-hour of the DUE instant in the COMPANY timezone —
  and hands the ids to the broker with the same `enqueue_created` hand-off the other schedulers
  use (a dead broker marks the rows failed and re-raises; nothing is reported as queued that was
  not).
* **Children are JUST-IN-TIME**: no future child row is ever materialized (the plan's M2 trap); a
  tick with nothing due writes NOTHING — no activity row, no task touch, no broker contact.
* **The due instant is consumed once**: two ticks that both see the same due instant collide on
  the task's unique dedupe key (the loser re-selects and creates nothing), and `next_run_at` is
  recomputed from the COMPANY-tz wall clock — never by adding 24h, so a spring-forward day (23 h)
  and a fall-back day (25 h) keep their local hour (see `schedule_service.compute_next_run`).
  A missed window (worker down at the due moment) fires ONE child on the next tick that sees the
  due check pass — keyed to the due instant, so a backlog of missed ticks cannot replay.
* A DISABLED row is skipped before any other logic (pause = `enabled=false` on the wire).  A
  malformed STORED value (hour outside 0-23, a weekly row without a weekday) is a named `E_CONFIG`
  refusal from the service — the tick then writes nothing at all.

Timing note: `now` is read once per tick (`core/timeutil.now_utc`) and the company timezone comes
from `core/timeutil.company_tz()` — the same seams every other cycle uses.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.timeutil import company_tz, now_utc
from celery_app.tasks._common import enqueue_created, run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)


@app.task(name="celery_app.tasks.cadence.run_due_schedules")
def run_due_schedules() -> dict[str, Any]:
    """Every-15-min Beat tick — one child per due schedule, next_run recomputed from the wall clock."""
    return run_async(_run_due_schedules())


async def _run_due_schedules() -> dict[str, Any]:
    """The async body of `run_due_schedules` (runs on the process loop)."""
    from app.core.database import get_session_factory
    from app.services.schedule_service import claim_due_schedules
    from celery_app.tasks._common import create_task_deduped

    session_factory = get_session_factory()
    moment = now_utc()
    zone = company_tz()

    created_ids: list[int] = []
    deduped = 0
    # ONE transaction for the whole tick: the claim advances each due row and the child insert
    # runs in the SAME session, so an advance and its child commit together — a crash mid-tick
    # cannot leave an occurrence consumed without its child or vice versa (a re-delivery of the
    # same due instant then hits the dedupe key and re-reports `deduped`).
    async with session_factory() as db:
        claims = await claim_due_schedules(db, now=moment, tz=zone)
        for claim in claims:
            task = await create_task_deduped(
                db,
                title=claim.name,
                agent_type="orchestrator",
                dedupe_key=claim.dedupe_key,
                description=claim.prompt,
                source="scheduler",
                metadata={"schedule_id": claim.schedule_id, "due": claim.due_at.isoformat()},
            )
            if task is None:  # the dedupe key already exists — ONE child per due instant
                deduped += 1
            else:
                created_ids.append(int(task.id))
        await db.commit()

    # Broker hand-off AFTER the commit: `enqueue_created` marks the rows failed and raises when
    # the broker is down (the shared scheduler contract), so a failed hand-off is visible as
    # failed rows, never as a fake queue.
    if created_ids:
        await enqueue_created(created_ids, label="task cadence")

    result: dict[str, Any] = {
        "status": "completed" if claims else "no_due",
        "due": len(claims),
        "created": len(created_ids),
        "deduped": deduped,
        "task_ids": created_ids,
    }
    logger.info("task cadence: %s", result)
    return result
