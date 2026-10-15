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

H-03 (wave U3, backend auditor).  Both cycles used to call `run_agent_for_task` DIRECTLY — the
run never reached `agent_runs` (whose only other production writer is the queue worker), so
`GET /finance/summary`'s `ai_spend`, the usage ledger and the dispatch budget gate all under-counted
every scheduled day, and the cycle's OWN call was never gated at all.  Both call sites now go
through `_run_recorded_cycle_agent`: the D3 gate first, then a `run_type="cycle"` ledger row created
before the model call and closed afterwards (with its accounting columns), closed `failed` when the
run raises.  A day whose budget is full refuses the tick with a named skip + one activity row —
nothing is charged and no row is written.

Claim (wave v15 / E3 item 3-02, B6).  Each hourly tick now takes a DURABLE once-per-(cycle,
company-local day) claim (`_claim_cycle_tick` — a committed `cycle_claimed` activity row, written
after the window self-gate and BEFORE any spend) and a tick that finds the day claimed exits as a
named skip (`cycle_tick_skipped`, `reason="already_claimed"`) with zero provider calls.  This is
what makes a broker redelivery of a pre-ack-killed tick harmless: the first tick's claim is already
committed, so the replay skips instead of re-running the orchestrator (the §B6 double spend).  The
beat entries' `expires` were shortened below the hour (3600 -> 1800) as the second layer; the claim
is the primary guard.  The claim is per cycle AND per local day: the evening cycle's claim is its
own, and the next day's window claims anew.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
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


# ── the once-per-(cycle, company-local-day) claim (wave v15 / E3 item 3-02) ──────────────────
# BUG-DIGEST MAJOR §B6: the hourly entries fire with `expires: 3600` — the exact width of the
# local-hour self-gate — so a pre-ack kill redelivers INSIDE the same window and the whole
# cycle re-runs: a second orchestrator model call (double spend).  The per-day task dedupe keys
# never covered the model call itself.
#
# Claim store — decided with evidence (lane receipt §3-02): `activity_log`, the table this same
# item designates for the named-skip record.  No existing table carries a DB-enforced uniqueness
# key for (cycle, company-local day) that is not a fabricated task row (`tasks.dedupe_key` is
# the only free-form unique index — a claim row there would be a fake task on the board — and
# `daily_reports.report_date` is ONE row per day shared by both cycles); a new claim table would
# serialize this lane on another seat's migration, so the claim rides the honest existing store.
# Atomicity: the check+insert is one transaction under a company-row lock on PostgreSQL (SQLite
# serializes writers behind its single-writer lock), and the claim COMMITS before any spend — so
# the concurrency that matters (a redelivery after the first delivery's process died) always
# finds the committed claim and exits as a named skip.
CYCLE_CLAIM_ACTION = "cycle_claimed"
CYCLE_SKIP_ACTION = "cycle_tick_skipped"


async def _claim_cycle_tick(
    *, cycle: str, day: date, window: dict[str, Any]
) -> tuple[bool, int | None]:
    """Insert the once-per-(cycle, company-local day) claim; report who holds it.

    Returns `(True, new_claim_id)` when THIS tick won the claim (the row is committed before
    the caller can spend anything) and `(False, holder_claim_id)` when the day is already
    claimed — the caller then records the named skip and returns without a provider call.

    The claim is an `activity_log` row (`action='cycle_claimed'`, `detail={cycle, day, window}`)
    — the same ledger the skip record uses, so `GET /activity` tells the whole once-per-day
    story.  The scan is bounded to the claimed day (minus an hour of slack) so the JSON-path
    filter never walks the whole retention window.
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.core.timeutil import as_bound_instant, day_bounds, dialect_name
    from app.models.company import CompanyConfig
    from app.models.report import ActivityLog
    from app.services.activity_service import log_activity

    session_factory = get_session_factory()
    async with session_factory() as db:
        if dialect_name(db) == "postgresql":
            # Serialize concurrent claimers on the single company row (the claim's critical
            # section).  SQLite needs no equivalent: its single-writer lock serializes the
            # claim transaction itself.
            await db.execute(
                select(CompanyConfig.id).order_by(CompanyConfig.id).limit(1).with_for_update()
            )
        local_day_start, _local_day_end = day_bounds(day)
        claim_floor = as_bound_instant(local_day_start - timedelta(hours=1), db)
        existing = (
            await db.execute(
                select(ActivityLog.id)
                .where(
                    ActivityLog.action == CYCLE_CLAIM_ACTION,
                    ActivityLog.created_at >= claim_floor,
                    ActivityLog.detail["cycle"].as_string() == cycle,
                    ActivityLog.detail["day"].as_string() == day.isoformat(),
                )
                .order_by(ActivityLog.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db.rollback()
            return False, int(existing)

        claim = await log_activity(
            db,
            agent_type="orchestrator",
            action=CYCLE_CLAIM_ACTION,
            summary=(
                f"{cycle} cycle claimed for the company-local day {day.isoformat()} — committed "
                "before any spend; a redelivered tick of this window skips here"
            ),
            detail={"cycle": cycle, "day": day.isoformat(), "window": window},
        )
        claim_id = int(claim.id)
        # Durable NOW: the claim is its own committed transaction, written before the caller
        # can reach the budget gate, the run ledger or the model call.
        await db.commit()
        return True, claim_id


async def _skip_claimed_cycle(
    *, cycle: str, day: date, window: dict[str, Any], claim_id: int | None
) -> dict[str, Any]:
    """Record the NAMED skip a redelivered tick exits with — one activity row, ZERO spend."""
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity

    async with get_session_factory()() as db:
        await log_activity(
            db,
            agent_type="orchestrator",
            action=CYCLE_SKIP_ACTION,
            summary=(
                f"{cycle} cycle tick skipped — {day.isoformat()} is already claimed "
                f"(claim #{claim_id}); no provider call"
            ),
            level="warning",
            detail={
                "cycle": cycle,
                "day": day.isoformat(),
                "reason": "already_claimed",
                "claim_id": claim_id,
                "window": window,
            },
        )
        await db.commit()
    logger.info("%s cycle tick skipped (already_claimed) day=%s claim=%s", cycle, day, claim_id)
    return {
        "status": "skipped",
        "reason": "already_claimed",
        "cycle": cycle,
        "day": day.isoformat(),
        "claim_id": claim_id,
        "window": window,
    }


async def _notify_cycle_report(*, kind: str, report_date: date, summary: str) -> None:
    """V8/C5: the cycle-report email leg — called AFTER the report's own commit.

    NEVER fails the cycle: the helper it calls is contractually never-raising, and this
    wrapper swallows even a bug in that contract (a report that was written must not be
    un-written by an email problem).  Own short session: the step runs after the cycle's
    report transaction has closed and committed.
    """
    try:
        from app.core.database import get_session_factory
        from app.services.notification_service import notify_owner_cycle_report

        async with get_session_factory()() as db:
            await notify_owner_cycle_report(db, kind=kind, report_date=report_date, summary=summary)
    except Exception:  # noqa: BLE001 — never the cycle's failure
        logger.warning("cycle report email leg failed (kind=%s)", kind, exc_info=True)


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
    start_local = datetime.combine(day, clock_time(hour=hour), tzinfo=tz)
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


async def _run_recorded_cycle_agent(
    *,
    agent_type: str,
    task_dict: dict[str, Any],
    context: dict[str, Any],
    cycle: str,
) -> dict[str, Any]:
    """Run ONE cycle orchestrator call through the dispatch gate AND the run ledger (H-03).

    The audit (wave U2, H-03) measured that both cycles called `run_agent_for_task` DIRECTLY: the
    run never reached `agent_runs`, so `GET /finance/summary`'s `ai_spend`, the usage ledger and the
    dispatch budget gate (`_common.day_spend_by_agent`, whose window is the company-local day) all
    under-counted every scheduled day — and the cycle's own call was never gated at all.

    The recorded shape deliberately mirrors the queue worker (`agent_tasks._execute_agent_task`):

    1. the D3 gate runs FIRST (`assert_budget_allows`): a refused dispatch spends nothing and gets
       no ledger row.  `BudgetStop` propagates, so each caller decides skip-vs-fail;
    2. the run row is created and COMMITTED **before** the model call, so a process that dies
       mid-run leaves evidence (a `running` row), never silence;
    3. an exception closes that row `failed` (error recorded) and RE-RAISES — a crashed cycle must
       neither swallow its failure nor leave a forever-`running` row behind;
    4. a completed run is closed with its output, duration and accounting columns
       (`_apply_run_usage`), which is exactly what makes its cost visible to `ai_spend` and to the
       gate's own next decision.

    `task_id` stays None with `run_type="cycle"`: a cycle run is not a queue row, which the schema
    permits (the partial unique `ux_agent_runs_task_attempt` is `WHERE task_id IS NOT NULL`).

    Returns the agent's result dict (``{}`` when the agent returned a non-dict — never a
    fabricated shape).
    """
    from app.agents.crew_factory import run_agent_for_task
    from app.core.database import get_session_factory
    from app.services.task_service import create_agent_run, finish_agent_run

    # The usage->column mapping has ONE definition (the queue's); re-deriving it here would fork
    # the money rule. Same package, lazy import — the file's existing import discipline.
    from celery_app.tasks.agent_tasks import _apply_run_usage

    await assert_budget_allows(agent_type)

    session_factory = get_session_factory()
    async with session_factory() as db:
        run = await create_agent_run(
            db, agent_type, task_id=None, input_context=context, run_type="cycle"
        )
        await db.commit()
        run_id = int(run.id)

    started = time.monotonic()
    try:
        # The CLI call is blocking: off the loop, holding no DB session across it.
        result = await asyncio.to_thread(run_agent_for_task, agent_type, task_dict, context)
    except BaseException as exc:  # noqa: BLE001 — the run is closed as failed, then re-raised
        duration = round(time.monotonic() - started, 2)
        async with session_factory() as db:
            await finish_agent_run(
                db,
                run_id,
                "failed",
                duration_secs=duration,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            await db.commit()
        raise
    duration = round(time.monotonic() - started, 2)

    payload: dict[str, Any] | None = result if isinstance(result, dict) else None
    async with session_factory() as db:
        run_row = await finish_agent_run(
            db, run_id, "completed", output=payload, duration_secs=duration
        )
        if run_row is not None:
            _apply_run_usage(run_row, payload if payload is not None else {})
        await db.commit()
    return payload if payload is not None else {}


async def _deny_cycle_tick(
    *, cycle: str, day: date, window: dict[str, Any], code: str, reason: str
) -> dict[str, Any]:
    """Record a budget-refused cycle tick and return its named skip (H-03, the gate's caller half).

    Refused BEFORE execution: no model call, no `agent_runs` row, no planned work — the same D2/D3
    contract every other scheduled dispatch keeps.  The refusal is written as an ACTIVITY row
    (one `log_activity`, so the live feed sees the tick being refused) and returned as a named skip
    so the beat result carries it too.
    """
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity

    async with get_session_factory()() as db:
        await log_activity(
            db,
            agent_type="orchestrator",
            action="cycle_budget_denied",
            summary=f"{cycle} cycle deferred: {reason}",
            level="warning",
            detail={"cycle": cycle, "code": code, "reason": reason, "day": day.isoformat()},
        )
        await db.commit()
    logger.warning("%s cycle: budget denied (%s)", cycle, reason)
    return {
        "status": "skipped",
        "reason": "budget_denied",
        "code": code,
        "budget": reason,
        "window": window,
    }


async def _morning_cycle() -> dict[str, Any]:
    from app.agents.accounting import BudgetStop
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

    # 3-02 (B6): the once-per-(cycle, local-day) claim — BEFORE any spend.  A redelivered tick
    # of the same window finds the committed claim and exits as a named skip with ZERO provider
    # calls (the double-spend probe this item ships).
    claimed, claim_id = await _claim_cycle_tick(cycle="morning", day=day, window=window)
    if not claimed:
        return await _skip_claimed_cycle(
            cycle="morning", day=day, window=window, claim_id=claim_id
        )

    async with session_factory() as db:
        context = await get_full_context(db)

    # The orchestrator's CLI call is blocking: run it off the loop, and hold no DB
    # session across it (the upload pinned a connection for the whole run).
    #
    # H-03: it goes through the DISPATCH GATE + RUN LEDGER (`_run_recorded_cycle_agent`), so the
    # cycle's real model spend is measured where `ai_spend` and the day cap read it, and a full
    # day refuses this tick the same way it refuses every other scheduled dispatch.
    try:
        plan = await _run_recorded_cycle_agent(
            agent_type="orchestrator",
            task_dict={
                "title": "Morning planning cycle",
                "description": f"Generate today's task plan for {day.isoformat()}",
                # L3-06: the caller declares the cycle — the agent reads this FIRST and only falls
                # back to sniffing title/description for ad-hoc tasks. A model-authored plan entry
                # whose wording contains "close"/"report"/"summary" can no longer flip this run.
                "metadata": {"cycle": "morning"},
            },
            context=context,
            cycle="morning",
        )
    except BudgetStop as exc:
        # Refused before anything ran: no model call, no ledger row, no planned or fixed work for
        # the day (the fixed work's own gates would refuse it too — the whole day is full).
        return await _deny_cycle_tick(
            cycle="morning", day=day, window=window, code=exc.code, reason=exc.reason
        )
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

    # V8/C5: the cycle-report email — AFTER the report transaction committed (the email must
    # describe a committed report), never failing the cycle.
    await _notify_cycle_report(kind="morning", report_date=day, summary=plan_text)

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

    from app.agents.accounting import BudgetStop
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

    # 3-02 (B6): the evening cycle owns its OWN (cycle, local-day) claim — morning's claim
    # never blocks it; a redelivered evening tick skips here, before any spend.
    claimed, claim_id = await _claim_cycle_tick(cycle="evening", day=day, window=window)
    if not claimed:
        return await _skip_claimed_cycle(
            cycle="evening", day=day, window=window, claim_id=claim_id
        )

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

    # H-03: the evening cycle's own model call is gated and recorded too (the second call site).
    try:
        result = await _run_recorded_cycle_agent(
            agent_type="orchestrator",
            task_dict={
                "title": "Evening reporting cycle",
                "description": f"Generate evening summary for {day.isoformat()}",
                # L3-06: the caller declares the cycle; the agent reads this FIRST.
                "metadata": {"cycle": "evening"},
            },
            context=context,
            cycle="evening",
        )
    except BudgetStop as exc:
        # Refused before the model call: no run row, no report row — the tick is a named skip.
        return await _deny_cycle_tick(
            cycle="evening", day=day, window=window, code=exc.code, reason=exc.reason
        )
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

    # V8/C5: the cycle-report email — AFTER the report transaction committed, never failing
    # the cycle (the same step as the morning cycle's).
    await _notify_cycle_report(kind="evening", report_date=day, summary=summary_text)

    return {
        "status": "completed",
        "day": day.isoformat(),
        "tasks_completed": completed,
        "tasks_failed": failed,
        "counts": counts,
        "summary": summary_text,
        "window": window,  # L4-11: which company-local hour this tick matched (audit parity with the morning cycle)
    }
