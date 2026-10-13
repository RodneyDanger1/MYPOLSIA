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

Timing (T2).  The morning entry in `beat_schedule` fires **hourly** and this task self-gates
on the window: the configured hour is read as `company_config.daily_cycle_hour` FIRST, with
`settings.morning_cycle_hour` filling an ABSENT value only — precedence is explicit, and a
stored value always wins.  The hour is **company-local** (interpreted via
`core/timeutil.company_tz()`, never as a naive UTC hour), so the task runs only inside the
configured hour's `[H:00, H+1:00)` local window; an invalid stored value is a named
`E_CONFIG` refusal, never a silent fallback.

Timing (L4-11).  The evening entry fires **hourly** on the same pattern and self-gates on the
company-local `settings.evening_cycle_hour` (the frozen company schema carries no evening hour —
see `_resolve_evening_hour`).  It used to fire on a UTC crontab hour while writing the report for
the company-LOCAL day (`today_in()`), so for any non-UTC company the "evening" summary was filed
against the wrong (or a partial) local day; the hour now means the same timezone the day window
does, and a change in Settings takes effect without a beat restart.

Trade-off recorded (lane 04 D5): the finance snapshot is now queued with the rest instead of
running before the orchestrator.  The cycle therefore no longer blocks on it (the defect
being fixed), and today's plan sees the *previous* snapshot; the fresh snapshot lands
asynchronously and feeds the evening report and tomorrow's plan.  Running it inline again
would restore the serial wall-clock this design exists to remove.

T13 (D20).  The morning cycle's summary write is the production consumer of
`activity_service.log_activities`: every plan entry the run SKIPPED becomes one row in a single
`executemany` (budget denial / malformed entry / unknown agent type, reason named), where those
reasons previously died in `logger.warning` + the return dict.  The cycle keeps exactly ONE
`log_activity` for its headline announcement, because the batch writer publishes no Redis event
and `/ws/activity` must still see the cycle.

T14 (D21).  The producer half of the refund path: when the morning cycle creates the fixed
`finance` task, it attaches the failed-payment records the Stripe webhook persisted
(`finance_service.recent_failed_payments` -> `metadata.failed_payments`), so the finance agent's
run actually RECEIVES the `pi_…` id a refund needs.  Nothing is attached when there are no such
records — the task row is then exactly what it was before.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.timeutil import company_tz, now_utc
from celery_app.tasks._common import (
    assert_budget_allows,
    create_task_deduped,
    dedupe_key,
    enqueue_created,
    plan_bucket,
    run_async,
)
from celery_app.worker import app

logger = logging.getLogger(__name__)

# The documented band is "enqueues 5-8 tasks for the day" on the morning-cycle row of
# docs/ACCEPTANCE_TESTING.md (since D7: "Hourly Beat job, self-gated to the configured
# company-local hour (default 06:00)").  Of those, three are fixed (finance snapshot +
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
    """Hourly Beat tick — self-gates on the company-local Morning Cycle Hour, then plans the day."""
    return run_async(_morning_cycle())


@app.task(name="celery_app.tasks.daily_cycle.run_evening_cycle")
def run_evening_cycle() -> dict[str, Any]:
    """Hourly Beat tick — self-gates on the company-local Evening Cycle Hour, then summarises."""
    return run_async(_evening_cycle())


async def _resolve_morning_hour(db) -> tuple[int, str]:
    """The hour the morning cycle runs at, and which source supplied it.

    Company value first, the settings fallback for an absent value only; an invalid value is
    a named `E_CONFIG` refusal rather than a silent default (see the module docstring).
    """
    # precedence: company_config.daily_cycle_hour wins; settings.morning_cycle_hour fills an
    # absent value only.
    from app.core.errors import E_CONFIG, ERROR_HINTS, PolsiaError
    from app.services.company_service import get_daily_cycle_hour

    stored = await get_daily_cycle_hour(db)
    if stored is None:
        hour, source = settings.morning_cycle_hour, "settings"
    else:
        hour, source = stored, "company"
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise PolsiaError(
            E_CONFIG,
            f"morning cycle hour {hour!r} (from {source}) is not a valid hour: "
            "the accepted range is an integer 0-23",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"value": hour, "source": source},
        )
    return hour, source


def _morning_window(day: date, hour: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The `[H:00, H+1:00)` company-local window as timezone-aware UTC instants.

    Shared by BOTH cycles (the name is historical): the morning cycle gates on it, and since
    L4-11 the evening cycle gates on the same shape at its own company-local hour.
    """
    start_local = datetime.combine(day, time(hour=hour), tzinfo=tz)
    end_local = start_local + timedelta(hours=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _resolve_evening_hour() -> tuple[int, str]:
    """The hour the evening cycle runs at (company-local), and which source supplied it.

    Same validation shape as `_resolve_morning_hour` — an invalid value is a named `E_CONFIG`
    refusal, never a silent default. The frozen company schema carries no evening hour
    (`company_config.daily_cycle_hour` is the morning's), so `settings.evening_cycle_hour` is
    the single source today; a company-level evening hour is an owner decision (REQUESTS).
    """
    from app.core.errors import E_CONFIG, ERROR_HINTS, PolsiaError

    hour, source = settings.evening_cycle_hour, "settings"
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise PolsiaError(
            E_CONFIG,
            f"evening cycle hour {hour!r} (from {source}) is not a valid hour: "
            "the accepted range is an integer 0-23",
            how_to_fix=ERROR_HINTS[E_CONFIG],
            context={"value": hour, "source": source},
        )
    return hour, source


async def _morning_cycle() -> dict[str, Any]:
    from app.agents.accounting import BudgetStop
    from app.agents.crew_factory import run_agent_for_task
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activities, log_activity
    from app.services.company_service import get_full_context
    from app.services.finance_service import recent_failed_payments
    from app.services.report_service import save_morning_plan

    session_factory = get_session_factory()

    # The self-gate (T2): the beat entry fires hourly, so decide HERE, before any heavy work.
    # The configured hour is company-local, read company-first, and an invalid value has already
    # raised E_CONFIG inside _resolve_morning_hour.
    now = now_utc()
    tz = company_tz()
    async with session_factory() as db:
        hour, source = await _resolve_morning_hour(db)
    day = now.astimezone(tz).date()  # the company's local day: what "today's plan" means (D-16)
    start, end = _morning_window(day, hour, tz)
    window = {
        "hour": hour,
        "source": source,
        "tz": tz.key,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if not (start <= now < end):
        logger.info(
            "morning cycle skipped (outside_morning_window): now=%s window=[%s, %s) "
            "hour=%s source=%s tz=%s",
            now.isoformat(),
            start.isoformat(),
            end.isoformat(),
            hour,
            source,
            tz.key,
        )
        return {"status": "skipped", "reason": "outside_morning_window", "window": window}

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
            # L3-06: the caller declares the cycle — the agent reads this FIRST and only falls
            # back to sniffing title/description for ad-hoc tasks. A model-authored plan entry
            # whose wording contains "close"/"report"/"summary" can no longer flip this run.
            "metadata": {"cycle": "morning"},
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
            # D3: measured-spend gate per dispatch (audit A1/L2-F01); a denied entry is skipped
            # WITH its reason — never silently dropped, never partially created.
            try:
                await assert_budget_allows(agent_type)
            except BudgetStop as exc:
                skipped.append(f"{agent_type}: budget — {exc.reason}")
                logger.warning("morning cycle: budget denied %s (%s)", agent_type, exc.reason)
                continue
            key = dedupe_key(agent_type, bucket)
            # T14/D21: the finance run is the one that must SEE the failed payments (its refund
            # path needs a real `pi_…` id).  Agents hold no DB session, so the worker can only
            # hand a task its own `metadata` — this is the producer that fills it, from the
            # records the Stripe webhook persisted (finance_service.recent_failed_payments).
            # Absent reads attach NOTHING (the task row is exactly what it was before, and the
            # agent's consumer degrades to no extra context) — an empty list is not attached
            # because "no failed payments" needs no row.
            metadata: dict[str, Any] | None = None
            if agent_type == "finance":
                failed_payments = await recent_failed_payments(db)
                if failed_payments:
                    metadata = {"failed_payments": failed_payments}
            task = await create_task_deduped(
                db,
                title=title,
                agent_type=agent_type,
                dedupe_key=key,
                priority=priority,
                source="scheduler",
                metadata=metadata,
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
            # D3: measured-spend gate per planned dispatch (audit A1/L2-F01).
            try:
                await assert_budget_allows(agent_type)
            except BudgetStop as exc:
                skipped.append(f"{agent_type}: budget — {exc.reason}")
                logger.warning("morning plan: budget denied %s (%s)", agent_type, exc.reason)
                continue
            # The priority coercion below used to be fully silent.  An ABSENT priority is the
            # output schema's own default (`TaskSpec.priority` — default=3,
            # app/schemas/agent_outputs.py), so absence is not malformed and is not recorded;
            # a PRESENT value the coercion rewrites — non-numeric, or falsy like 0 swallowed
            # by the `or` — is recorded under a stable slug so "defaulted" and "coerced" stay
            # distinguishable.
            raw_priority = entry.get("priority")
            try:
                priority = int(raw_priority or 3)
                coerced = raw_priority is not None and not raw_priority
            except (TypeError, ValueError):
                priority = 3
                coerced = True
            if coerced:
                logger.warning(
                    "code=priority_coerced: morning plan entry %r priority %r -> 3",
                    title,
                    raw_priority,
                )
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
    # .apply(), serialising the whole cycle behind it). D2/T19 parity: a dead broker marks
    # the created rows failed instead of stranding them (see enqueue_created).
    if created:
        await enqueue_created(created, label="morning cycle")

    plan_text = str(plan.get("summary") or "Morning plan generated")
    async with session_factory() as db:
        await save_morning_plan(db, report_date=day, plan=plan_text, tasks_planned=len(created))
        # T13/D20: the cycle's per-item records go through the BATCH writer — one `executemany`
        # for every plan entry this run had to skip (budget denial / malformed entry / unknown
        # agent type), which is exactly the "hundreds of rows, one round trip" case
        # `log_activities` was built and measured for (×63.8 vs one insert per row).  These rows
        # used to exist nowhere durable: the reasons lived in `logger.warning` + the return dict,
        # so the dashboard feed could show a plan's SIZE but never why it was short.
        skip_rows: list[dict[str, Any]] = [
            {
                "agent_type": "orchestrator",
                "action": "plan_entry_skipped",
                "summary": reason,
                "level": "warning",
                "detail": {"day": day.isoformat(), "reason": reason, "cycle": "morning"},
            }
            for reason in skipped
        ]
        if skip_rows:
            await log_activities(db, skip_rows)
        # The cycle's ANNOUNCEMENT stays a single `log_activity`: `log_activities` publishes no
        # Redis event by design, and `/ws/activity` must still see the cycle complete.
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
                "utc_day": now.astimezone(UTC).date().isoformat(),
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
        "window": window,
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

    # The self-gate (L4-11): the beat entry fires hourly, so decide HERE whether this tick is the
    # company's evening hour — BEFORE any model call. The hour is company-local (the same
    # timezone the day window below uses), so the report is filed against the local day that is
    # ending, not against whatever the UTC clock says. An invalid configured hour has already
    # raised E_CONFIG inside _resolve_evening_hour.
    now = now_utc()
    tz = company_tz()
    hour, source = _resolve_evening_hour()
    day = now.astimezone(tz).date()  # the company's local day: what "the day's summary" means
    start, end = _morning_window(day, hour, tz)
    window = {
        "hour": hour,
        "source": source,
        "tz": tz.key,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if not (start <= now < end):
        logger.info(
            "evening cycle skipped (outside_evening_window): now=%s window=[%s, %s) "
            "hour=%s source=%s tz=%s",
            now.isoformat(),
            start.isoformat(),
            end.isoformat(),
            hour,
            source,
            tz.key,
        )
        return {"status": "skipped", "reason": "outside_evening_window", "window": window}

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
            # L3-06: the caller declares the cycle; the agent reads this FIRST.
            "metadata": {"cycle": "evening"},
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
        # T13/D20: the evening cycle produces exactly ONE event and no per-item records (its
        # aggregate is the event; the per-status detail rides the row's `detail`), so there is
        # nothing to batch — a one-row `log_activities` call would buy nothing and would LOSE the
        # live publish (the batch writer publishes no Redis event by design).  The single
        # `log_activity` here is the documented announcement shape, unchanged.
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
        "window": window,  # L4-11: which company-local hour this tick matched (audit parity with the morning cycle)
    }
