"""Morning and evening cycle Celery tasks — triggered by Beat.

Morning (D-01/D-02/D-03/D-06): run the orchestrator, **materialise the tasks it returns**
(they were discarded, so the documented 5-8 tasks/day never happened), record the plan in
`daily_reports.morning_plan` (dead code in the upload), and fan the independent agent
tasks out with a Celery group.  The upload ran the finance agent with
`run_agent_task.apply(...)` — eagerly, inside the beat process, blocking it for the whole
CLI run and bypassing routing and retries.  There is no `.apply()` in this file.

Evening (D-04/D-05): ONE grouped aggregate over the day window, computed with
`app.core.timeutil.day_window` (company timezone, dialect-correct).  The upload compared a
naive local midnight against a `timestamptz` column — measured: 0 rows selected for a day
that had 303 (lane 06, matrix Table 3).

Trade-off recorded (lane 04 D5): the finance snapshot is now queued with the rest instead of
running before the orchestrator.  The cycle therefore no longer blocks on it (the defect
being fixed), and today's plan sees the *previous* snapshot; the fresh snapshot lands
asynchronously and feeds the evening report and tomorrow's plan.  Running it inline again
would restore the serial wall-clock this design exists to remove.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery import group

from app.core.timeutil import today_in, today_utc
from celery_app.tasks._common import create_task_deduped, dedupe_key, plan_bucket, run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

# The documented band is "06:00 Beat job enqueues 5-8 tasks for the day"
# (docs/ACCEPTANCE_TESTING.md:94).  Of those, three are fixed (finance snapshot +
# customer_support + social_media) and the orchestrator's plan supplies the rest, so the
# plan contributes at most 5.  Nothing here fabricates tasks to reach the band: a plan
# that does not fill it is logged as a warning.
MAX_MORNING_TASKS = 8
FIXED_MORNING_TASKS = 3
MAX_PLANNED_TASKS = MAX_MORNING_TASKS - FIXED_MORNING_TASKS

# (agent_type, title, dedupe bucket, priority) — always created, once per UTC day.
_FIXED_MORNING_WORK: tuple[tuple[str, str, str, int], ...] = (
    ("finance", "Daily revenue snapshot", "morning-snapshot", 1),
    ("customer_support", "Morning customer support sweep", "morning-sweep", 2),
    ("social_media", "Morning social media check", "morning-check", 2),
)


@app.task(name="celery_app.tasks.daily_cycle.run_morning_cycle")
def run_morning_cycle() -> dict[str, Any]:
    """06:00 (settings.morning_cycle_hour, UTC) — plan the day, create its tasks, fan out."""
    return run_async(_morning_cycle())


@app.task(name="celery_app.tasks.daily_cycle.run_evening_cycle")
def run_evening_cycle() -> dict[str, Any]:
    """20:00 (settings.evening_cycle_hour, UTC) — day summary for the company's day."""
    return run_async(_evening_cycle())


async def _morning_cycle() -> dict[str, Any]:
    from app.agents.crew_factory import run_agent_for_task
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity
    from app.services.company_service import get_full_context
    from app.services.report_service import save_morning_plan
    # imported here, not at module scope: agent_tasks imports this module's package
    # through celery_app.worker, and a module-level import would be circular when
    # agent_tasks is the entry point.
    from celery_app.tasks.agent_tasks import run_agent_task

    session_factory = get_session_factory()
    day = today_in()  # the company's local day: what "today's plan" means (D-16)

    async with session_factory() as db:
        context = await get_full_context(db)

    # The orchestrator's CLI call is blocking: run it off the loop, and hold no DB
    # session across it (the upload pinned a connection for the whole run).
    plan = await asyncio.to_thread(
        run_agent_for_task,
        "orchestrator",
        {
            "title": "Morning planning cycle",
            "description": f"Generate today's task plan for {day.isoformat()}",
        },
        context,
    )
    if not isinstance(plan, dict):
        plan = {}
    raw_planned = plan.get("tasks")
    planned = [entry for entry in raw_planned if isinstance(entry, dict)] if isinstance(raw_planned, list) else []

    created: list[int] = []
    queued: list[tuple[str, int]] = []
    duplicates: list[str] = []
    skipped: list[str] = []

    # One transaction for the whole morning plan: nothing is queued before it commits, so
    # a worker can never pick up a row that does not exist yet (no producer/consumer race).
    async with session_factory() as db:
        for agent_type, title, bucket, priority in _FIXED_MORNING_WORK:
            key = dedupe_key(agent_type, bucket)
            task = await create_task_deduped(
                db,
                title=title,
                agent_type=agent_type,
                dedupe_key=key,
                priority=priority,
                source="scheduler",
            )
            if task is None:
                duplicates.append(key)
            else:
                created.append(task.id)
                queued.append((agent_type, task.id))

        dropped_by_cap = max(0, len(planned) - MAX_PLANNED_TASKS)
        if dropped_by_cap:
            logger.warning(
                "morning plan offered %s entries; %s are beyond MAX_PLANNED_TASKS=%s and were "
                "not created (the 5-8 band is a per-day ceiling, not a target)",
                len(planned),
                dropped_by_cap,
                MAX_PLANNED_TASKS,
            )
        for entry in planned[:MAX_PLANNED_TASKS]:
            title = str(entry.get("title") or "").strip()
            agent_type = str(entry.get("agent_type") or "").strip()
            if not title or not agent_type:
                skipped.append(f"malformed plan entry: {entry!r}")
                logger.warning("morning plan: skipping malformed entry %r", entry)
                continue
            try:
                priority = int(entry.get("priority") or 3)
            except (TypeError, ValueError):
                priority = 3
            key = dedupe_key(agent_type, plan_bucket(title))
            try:
                task = await create_task_deduped(
                    db,
                    title=title,
                    agent_type=agent_type,
                    dedupe_key=key,
                    description=entry.get("description"),
                    priority=priority,
                    source="orchestrator",
                )
            except ValueError as exc:
                # Unknown agent_type in the plan: skip that entry, keep the cycle alive
                # (a model output must not be able to break the scheduler).
                skipped.append(f"{agent_type}: {exc}")
                logger.warning("morning plan: %s", exc)
                continue
            if task is None:
                duplicates.append(key)
            else:
                created.append(task.id)
                queued.append((agent_type, task.id))

        if len(created) < 5:
            logger.warning(
                "morning cycle created %s task(s) for %s — below the documented 5-8 band "
                "(the orchestrator planned %s usable entries); not padding with fake work",
                len(created),
                day,
                len(planned),
            )

        await db.commit()
    # Fan out AFTER the commit: every task row exists before a worker can see its message.
    # A group dispatches them independently (the upload ran the finance agent inline with
    # .apply(), serialising the whole cycle behind it).
    if created:
        group([run_agent_task.s(task_id) for task_id in created]).apply_async()

    plan_text = str(plan.get("summary") or "Morning plan generated")
    async with session_factory() as db:
        await save_morning_plan(db, report_date=day, plan=plan_text, tasks_planned=len(created))
        await log_activity(
            db,
            agent_type="orchestrator",
            action="morning_plan_complete",
            summary=plan_text,
            level="success",
            detail={
                "tasks_planned": len(created),
                "tasks_skipped": len(skipped),
                "duplicates": len(duplicates),
                "plan_entries": len(planned),
                "plan_entries_dropped_by_cap": dropped_by_cap,
                "fanout": "celery_group",
                "day": day.isoformat(),
                "utc_day": today_utc().isoformat(),
            },
        )
        await db.commit()

    logger.info(
        "morning cycle: created=%s duplicates=%s skipped=%s dropped_by_cap=%s day=%s",
        len(created),
        len(duplicates),
        len(skipped),
        dropped_by_cap,
        day,
    )
    return {
        "status": "completed",
        "day": day.isoformat(),
        "created": len(created),
        "task_ids": created,
        "queued": [{"agent_type": a, "task_id": t} for a, t in queued],
        "duplicates": duplicates,
        "skipped": skipped,
        "plan_entries": len(planned),
        "plan_entries_dropped_by_cap": dropped_by_cap,
        "plan_summary": plan_text,
    }


async def _evening_cycle() -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.agents.crew_factory import run_agent_for_task
    from app.core.database import get_session_factory
    from app.core.timeutil import day_window
    from app.models.task import Task
    from app.services.activity_service import log_activity
    from app.services.company_service import get_full_context
    from app.services.report_service import save_evening_summary

    session_factory = get_session_factory()
    day = today_in()

    async with session_factory() as db:
        context = await get_full_context(db)
        # ONE grouped aggregate for the company-local day, half-open [start, end) bounds
        # bound for the active dialect.  Replaces the upload's two `count(*)` round-trips
        # against `datetime.combine(date.today(), time.min)` (naive local midnight).
        start, end = day_window(db, day)
        rows = await db.execute(
            select(Task.status, func.count())
            .where(Task.created_at >= start, Task.created_at < end)
            .group_by(Task.status)
        )
        counts = {status: int(n) for status, n in rows.all()}

    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)

    result = await asyncio.to_thread(
        run_agent_for_task,
        "orchestrator",
        {
            "title": "Evening reporting cycle",
            "description": f"Generate evening summary for {day.isoformat()}",
        },
        context,
    )
    if not isinstance(result, dict):
        result = {}
    summary_text = str(result.get("summary") or "Evening summary generated")
    raw_insights = result.get("insights")
    insights = [str(item) for item in raw_insights] if isinstance(raw_insights, list) else []

    async with session_factory() as db:
        await save_evening_summary(
            db,
            report_date=day,
            summary=summary_text,
            insights=insights,
            tasks_completed=completed,
            tasks_failed=failed,
            metrics_snapshot=context.get("kpis", {}),
        )
        await log_activity(
            db,
            agent_type="orchestrator",
            action="evening_summary_complete",
            summary=summary_text,
            level="success",
            detail={
                "tasks_completed": completed,
                "tasks_failed": failed,
                "tasks_total": sum(counts.values()),
                "day": day.isoformat(),
            },
        )
        await db.commit()

    return {
        "status": "completed",
        "day": day.isoformat(),
        "tasks_completed": completed,
        "tasks_failed": failed,
        "counts": counts,
        "summary": summary_text,
    }
