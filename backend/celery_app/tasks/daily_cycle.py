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

F-02 (wave A3).  The plan's `depends_on` is no longer discarded at materialisation: every planned
row carries its edges (`metadata.depends_on_titles`, resolved to `depends_on_ids` in a second pass
inside the SAME transaction) and only the DAG's ROOTS are handed to the broker.  A dependent is
released by `task_service.release_ready_dependents` when its whole set has completed — OR-01: the
producer of that release is the WORKER's terminal-completion hook
(`agent_tasks._release_dag_dependents`, after the outcome commit), which also fails a dependent BY
NAME when its upstream ends without completing.  (Before OR-01 this docstring promised a release
that no production code ever called, so every dependent stranded `pending`.)  A title this
cycle did not create, and a model-authored dependency cycle, are RECORDED on the row and released
as roots — the plan can shorten the day but it can never make work vanish.
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
    stamp_rearmed,
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

# ── F-21: a broker blip must not burn the company-local day ─────────────────────────────────
# The claim above commits BEFORE any spend, so a fan-out the broker refuses used to leave the day
# CLAIMED with its plan unrecorded and no tick left to try: `enqueue_created` raised, the cycle
# aborted before its report, and every later tick of the window exited `already_claimed`.  These
# two ledger actions are the re-arm and the recovery:
#   * `cycle_rearmed` — ONE durable row per refused hand-off, carrying the ids that are still owed
#     a delivery (the same rows `stamp_rearmed` marks for the stranded-row reconciler).  Its
#     presence AFTER the day's claim is what RELEASES that claim (`_claim_cycle_tick`), so the day
#     can be finished by its own next tick — and only by a tick of the SAME local day.
#   * `cycle_recovered` — the catch-up's record: the hand-off was finished (from the plan already
#     committed: no second model call, no second set of rows).  A re-arm followed by a recovery is
#     finished business; a third tick sees `already_claimed` again.
CYCLE_REARM_ACTION = "cycle_rearmed"
CYCLE_RECOVERED_ACTION = "cycle_recovered"



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
            # F-21: the claim is only HELD if no re-arm followed it.  A `cycle_rearmed` row written
            # after this claim is the durable release of that claim — the day's hand-off is still
            # owed, and the tick that writes the next claim is the one that finishes it.
            released_by = (
                await db.execute(
                    select(ActivityLog.id)
                    .where(
                        ActivityLog.action == CYCLE_REARM_ACTION,
                        ActivityLog.created_at >= claim_floor,
                        ActivityLog.detail["cycle"].as_string() == cycle,
                        ActivityLog.detail["day"].as_string() == day.isoformat(),
                        ActivityLog.id > int(existing),
                    )
                    .order_by(ActivityLog.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if released_by is None:
                await db.rollback()
                return False, int(existing)
        else:
            released_by = None

        rearm_detail: dict[str, Any] = {}
        if released_by is not None:
            # Record WHY this tick may claim again, so the ledger reads as claim -> re-arm -> claim.
            rearm_detail["rearm_claim_id"] = int(released_by)
        claim = await log_activity(
            db,
            agent_type="orchestrator",
            action=CYCLE_CLAIM_ACTION,
            summary=(
                f"{cycle} cycle claimed for the company-local day {day.isoformat()} — committed "
                "before any spend; a redelivered tick of this window skips here"
                + (
                    f" (re-claim after re-arm #{int(released_by)}: the day's hand-off was refused)"
                    if released_by is not None
                    else ""
                )
            ),
            detail={"cycle": cycle, "day": day.isoformat(), "window": window, **rearm_detail},
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


async def _cycle_rearm_owed(*, cycle: str, day: date) -> dict[str, Any] | None:
    """The day's unfinished re-arm record — `None` when this day owes no catch-up (F-21).

    Bounded to ONE company-local day by construction: the query asks for exactly `day`'s rows and
    the caller passes TODAY's local date, so yesterday's re-arm can never be dispatched today.  The
    answer is the LAST re-arm that no recovery followed: a re-arm, then a recovery, is finished
    business and a third tick must not touch it again.
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.core.timeutil import as_bound_instant, day_bounds
    from app.models.report import ActivityLog

    async with get_session_factory()() as db:
        local_day_start, _local_day_end = day_bounds(day)
        floor = as_bound_instant(local_day_start - timedelta(hours=1), db)
        rows = (
            await db.execute(
                select(ActivityLog.id, ActivityLog.action, ActivityLog.detail)
                .where(
                    ActivityLog.action.in_((CYCLE_REARM_ACTION, CYCLE_RECOVERED_ACTION)),
                    ActivityLog.created_at >= floor,
                    ActivityLog.detail["cycle"].as_string() == cycle,
                    ActivityLog.detail["day"].as_string() == day.isoformat(),
                )
                .order_by(ActivityLog.id)
            )
        ).all()

    owed: dict[str, Any] | None = None
    for row_id, action, detail in rows:
        if action == CYCLE_REARM_ACTION:
            owed = {
                "rearm_id": int(row_id),
                "detail": detail if isinstance(detail, dict) else {},
            }
        elif action == CYCLE_RECOVERED_ACTION:
            owed = None
    return owed


async def _still_undelivered(task_ids: list[int]) -> list[int]:
    """The ids still owed a delivery: `pending`, unclaimed, still on disk (F-21's catch-up set)."""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.task import Task

    if not task_ids:
        return []
    async with get_session_factory()() as db:
        rows = (
            await db.execute(
                select(Task.id)
                .where(
                    Task.id.in_(task_ids),
                    Task.status == "pending",
                    Task.claimed_at.is_(None),
                )
                .order_by(Task.id)
            )
        ).all()
    return [int(row[0]) for row in rows]


async def _rearm_cycle_tick(
    *,
    cycle: str,
    day: date,
    window: dict[str, Any],
    claim_id: int | None,
    task_ids: list[int],
    reason: str,
    code: str,
) -> dict[str, Any]:
    """Record that this day's hand-off is OWED, and mark the rows for the reconciler (F-21).

    ONE committed transaction: the `cycle_rearmed` ledger row (the durable release of the day's
    claim — see `_claim_cycle_tick`) plus `stamp_rearmed`'s per-row marker on the ids that were
    never delivered.  `stamp_rearmed` is the shipped marker `maintenance._reconcile_stranded_pending`
    reads, so the work is re-dispatched even if this process dies before the catch-up tick runs —
    no second scheduler, no new table.
    """
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity

    ids = [int(task_id) for task_id in task_ids]
    async with get_session_factory()() as db:
        rearmed = await stamp_rearmed(db, ids, reason="cycle_publish_refused")
        row = await log_activity(
            db,
            agent_type="orchestrator",
            action=CYCLE_REARM_ACTION,
            summary=(
                f"{cycle} cycle fan-out refused for the company-local day {day.isoformat()} — "
                f"{len(ids)} task(s) stay queued; the day's next tick finishes the hand-off"
            ),
            level="warning",
            detail={
                "cycle": cycle,
                "day": day.isoformat(),
                "window": window,
                "reason": reason,
                "code": code,
                "claim_id": claim_id,
                "task_ids": ids,
                "rearmed": rearmed,
            },
        )
        await db.commit()
        rearm_id = int(row.id)
    logger.warning(
        "%s cycle: fan-out refused (%s) — re-armed %s task(s) as rearm #%s for day %s",
        cycle, reason, len(ids), rearm_id, day,
    )
    return {"rearm_id": rearm_id, "task_ids": ids, "reason": reason}


async def _catch_up_cycle(
    *, cycle: str, day: date, window: dict[str, Any], claim_id: int | None, owed: dict[str, Any]
) -> dict[str, Any]:
    """Finish a refused hand-off from the plan that is ALREADY committed (F-21's catch-up).

    The model call and the plan rows happened before the refusal, so nothing here re-plans: the
    ids the re-arm recorded are re-dispatched (only those still `pending` and unclaimed — a row that
    already ran is not sent twice), and the recovery is recorded so no later tick repeats it.  A
    second broker refusal re-arms again and answers with a named `deferred` — never a crash, and
    never a second model call.
    """
    from app.core.database import get_session_factory
    from app.core.errors import E_REDIS, PolsiaError
    from app.services.activity_service import log_activity

    detail = owed.get("detail") or {}
    recorded = [int(task_id) for task_id in detail.get("task_ids") or [] if isinstance(task_id, int)]
    undelivered = await _still_undelivered(recorded)
    dispatched: list[int] = []
    if undelivered:
        try:
            await enqueue_created(undelivered, label=f"{cycle} cycle catch-up")
            dispatched = undelivered
        except PolsiaError as exc:
            if exc.code != E_REDIS:
                raise
            rearm = await _rearm_cycle_tick(
                cycle=cycle,
                day=day,
                window=window,
                claim_id=claim_id,
                task_ids=undelivered,
                reason="broker_refused",
                code=exc.code,
            )
            return {
                "status": "deferred",
                "reason": "broker_refused",
                "cycle": cycle,
                "day": day.isoformat(),
                "task_ids": undelivered,
                "rearm_id": rearm["rearm_id"],
                "window": window,
            }

    async with get_session_factory()() as db:
        await log_activity(
            db,
            agent_type="orchestrator",
            action=CYCLE_RECOVERED_ACTION,
            summary=(
                f"{cycle} cycle hand-off recovered for {day.isoformat()} — {len(dispatched)} "
                "task(s) re-queued from the committed plan (no second plan, no second model call)"
            ),
            level="success",
            detail={
                "cycle": cycle,
                "day": day.isoformat(),
                "window": window,
                "rearm_id": owed.get("rearm_id"),
                "task_ids": dispatched,
                "already_done": [task_id for task_id in recorded if task_id not in dispatched],
            },
        )
        await db.commit()
    logger.info(
        "%s cycle: catch-up dispatched %s task(s) for day %s (rearm #%s)",
        cycle, len(dispatched), day, owed.get("rearm_id"),
    )
    return {
        "status": "recovered",
        "cycle": cycle,
        "day": day.isoformat(),
        "task_ids": dispatched,
        "rearm_id": owed.get("rearm_id"),
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
    from app.core.errors import E_REDIS, PolsiaError
    from app.models.task import Task
    from app.services.activity_service import log_activities, log_activity
    from app.services.company_service import get_full_context
    from app.services.finance_service import recent_failed_payments
    from app.services.report_service import get_evening_summary_for, save_morning_plan
    from app.services.task_service import reap_stale_approvals

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
    # F-21: BEFORE the hour gate (and before any spend), ask whether TODAY's cycle owes a hand-off.
    # A broker refusal re-arms the day (its plan and rows are already committed), and the next tick
    # of the same company-local day finishes it FROM THAT PLAN — never a second plan, never a second
    # model call.  The hour window cannot answer this: the refusal usually lands at the window's
    # edge, so the tick that must retry is often the next hour's.  Bounded to `day` by construction.
    owed = await _cycle_rearm_owed(cycle="morning", day=day)
    if owed is not None:
        claimed, claim_id = await _claim_cycle_tick(cycle="morning", day=day, window=window)
        if not claimed:
            return await _skip_claimed_cycle(
                cycle="morning", day=day, window=window, claim_id=claim_id
            )
        return await _catch_up_cycle(
            cycle="morning", day=day, window=window, claim_id=claim_id, owed=owed
        )

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
        # F-43(c): the parked-approval reaper.  A run that asked for approval and was never
        # answered sat `awaiting_approval` FOREVER — the claim sweep only looks at `in_progress`,
        # so nothing ever ended it and every node depending on it stayed blocked.  The morning
        # tick is the cycle's natural place to sweep yesterday's dead parks: bounded, one activity
        # row per reaped task, committed here with the context read.
        reaped_approvals = await reap_stale_approvals(db)
        await db.commit()

    # F-43(b): `yesterday_summary` must be YESTERDAY's, not "the newest summary in the table"
    # (`company_service` reads `LIMIT 1`); the cycle asks for the date it actually means, and
    # records the absence when there is none instead of feeding the agent an unknown day.
    yesterday = day - timedelta(days=1)
    async with session_factory() as db:
        yesterday_summary = await get_evening_summary_for(db, yesterday)
    context["yesterday_summary"] = yesterday_summary
    context["yesterday_report_date"] = yesterday.isoformat() if yesterday_summary else None

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
    fixed_ids: list[int] = []
    planned_rows: list[Task] = []
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
                fixed_ids.append(task.id)
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
            # F-02: the plan's DAG and its non-title fields are part of the task, not decoration.
            # `depends_on` is a list of TITLES (the schema's contract, agent_outputs.TaskSpec);
            # they are resolved to ids in the second pass below, once every row has one.
            raw_depends = entry.get("depends_on")
            plan_meta: dict[str, Any] = {
                "plan_day": day.isoformat(),
                "depends_on_titles": (
                    [str(t) for t in raw_depends][:8] if isinstance(raw_depends, list) else []
                ),
                "side_effect": str(entry.get("side_effect") or "internal_write"),
                "acceptance": str(entry.get("acceptance") or "")[:500],
                "estimate_usd": entry.get("estimate_usd"),
                "estimate_seconds": entry.get("estimate_seconds"),
            }
            try:
                task = await create_task_deduped(
                    db,
                    title=title,
                    agent_type=agent_type,
                    dedupe_key=key,
                    description=entry.get("description"),
                    priority=priority,
                    source="orchestrator",
                    metadata=plan_meta,
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
                planned_rows.append(task)
                queued.append((agent_type, task.id))

        if len(created) < 5:
            logger.warning(
                "morning cycle created %s task(s) for %s — below the documented 5-8 band "
                "(the orchestrator planned %s usable entries); not padding with fake work",
                len(created),
                day,
                len(planned),
            )

        # F-02 second pass: titles -> ids on the rows this cycle OWNS (a dedupe hit is another
        # day's row and is left untouched), so a dependent can be dispatched by id later.  A
        # title this cycle did not create — or the row's own title — can never be an edge:
        # recorded in `depends_on_unresolved`, never guessed onto another row.
        by_title = {str(row.title): int(row.id) for row in planned_rows}
        edges: dict[int, list[int]] = {}
        for row in planned_rows:
            meta = dict(row.metadata_json or {})
            own = int(row.id)
            resolved: list[int] = []
            unresolved: list[str] = []
            for dep_title in meta.get("depends_on_titles") or []:
                target = by_title.get(str(dep_title))
                if target is None or target == own:
                    unresolved.append(str(dep_title))
                else:
                    resolved.append(target)
            meta["depends_on_ids"] = resolved
            if unresolved:
                meta["depends_on_unresolved"] = unresolved
            row.metadata_json = meta
            edges[own] = resolved

        # A node whose edges can never be satisfied — a model-authored cycle — would wait for
        # ever.  Release it as a root and NAME the cycle on the row: work must not vanish because
        # the plan was not a DAG (the flat dispatch this fix replaces at least ran everything).
        reachable = {node for node, deps in edges.items() if not deps}
        changed = True
        while changed:
            changed = False
            for node, deps in edges.items():
                if node not in reachable and all(dep in reachable for dep in deps):
                    reachable.add(node)
                    changed = True
        stranded = [node for node in edges if node not in reachable]
        if stranded:
            titles_by_id = {int(row.id): str(row.title) for row in planned_rows}
            rows_by_id = {int(row.id): row for row in planned_rows}
            for node in stranded:
                row = rows_by_id[node]
                meta = dict(row.metadata_json or {})
                meta["depends_on_cycle"] = [titles_by_id[dep] for dep in edges[node]]
                # B2-C4-R2: the wire flattened the cycle to a BOOLEAN, throwing away the members
                # the producer already had in hand.  Record them with their ids so the plan edge is
                # answerable ("which nodes formed the cycle?") instead of merely "yes, one did".
                meta["depends_on_cycle_members"] = [
                    {"id": int(dep), "title": str(titles_by_id[dep])} for dep in edges[node]
                ]
                meta["depends_on_ids"] = []
                row.metadata_json = meta
                logger.warning(
                    "morning plan: dependency cycle %s — releasing task %s as a root (recorded)",
                    meta["depends_on_cycle"],
                    node,
                )

        # Roots only: the fixed work and the plan's dependency-free entries.  Every dependent is
        # released by `task_service.release_ready_dependents` when its whole set has completed —
        # called by the WORKER's completion hook (`agent_tasks._release_dag_dependents`, OR-01),
        # which is the producer that was missing when this comment first claimed it.
        roots = [
            int(row.id)
            for row in planned_rows
            if not (row.metadata_json or {}).get("depends_on_ids")
        ]
        dispatch_ids = fixed_ids + roots

        await db.commit()
    # Fan out AFTER the commit: every task row exists before a worker can see its message.
    # A group dispatches them independently (the upload ran the finance agent inline with
    # .apply(), serialising the whole cycle behind it). D2/T19 parity: a dead broker marks
    # the created rows failed instead of stranding them (see enqueue_created).
    #
    # F-21: a refused hand-off no longer ABORTS the cycle.  The rows are committed, the plan is
    # in hand and the claim is already spent — so the refusal is RECORDED (a durable re-arm plus
    # `stamp_rearmed`'s per-row marker) and the cycle's own report below still gets written.  The
    # day's next tick finishes the hand-off from that plan; a blip must not cost the company day.
    deferred: dict[str, Any] | None = None
    if dispatch_ids:
        try:
            await enqueue_created(dispatch_ids, label="morning cycle")
        except PolsiaError as exc:
            if exc.code != E_REDIS:
                raise
            deferred = await _rearm_cycle_tick(
                cycle="morning",
                day=day,
                window=window,
                claim_id=claim_id,
                task_ids=dispatch_ids,
                reason="broker_refused",
                code=exc.code,
            )

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
                # F-02: the plan's SIZE and its hand-off are two different facts now that only
                # the DAG's roots are dispatched at cycle time.  F-21: a refused hand-off means
                # NOTHING was dispatched, so the row says 0 and names the deferral rather than
                # claiming a fan-out that did not happen.
                "dispatched": 0 if deferred is not None else len(dispatch_ids),
                "fanout": "celery_group",
                "deferred": deferred is not None,
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
    if deferred is not None:
        # F-21: the cycle RAN (plan, rows, report, announcement); only its hand-off is owed.  The
        # day's next tick finishes it from this plan — so the answer is a named deferral, not a
        # crash and not a false `completed`.
        return {
            "status": "deferred",
            "reason": "broker_refused",
            "cycle": "morning",
            "day": day.isoformat(),
            "created": len(created),
            "task_ids": created,
            "deferred_task_ids": deferred["task_ids"],
            "rearm_id": deferred["rearm_id"],
            "queued": [],
            "duplicates": duplicates,
            "skipped": skipped,
            "plan_entries": len(planned),
            "plan_entries_dropped_by_cap": dropped_by_cap,
            "plan_summary": plan_text,
            "window": window,
        }
    return {
        "status": "completed",
        "day": day.isoformat(),
        "created": len(created),
        "task_ids": created,
        # F-02: `queued` is the hand-off, not the creation — a dependent this cycle created but
        # did not dispatch (its parent has not completed yet) is NOT reported as queued.
        "queued": [{"agent_type": a, "task_id": t} for a, t in queued if t in set(dispatch_ids)],
        "duplicates": duplicates,
        "skipped": skipped,
        "plan_entries": len(planned),
        "plan_entries_dropped_by_cap": dropped_by_cap,
        "plan_summary": plan_text,
        # F-43(c): what the tick's reaper ended (a parked approval nobody answered).
        "reaped_approvals": reaped_approvals,
        "yesterday_report_date": context.get("yesterday_report_date"),
        "window": window,
    }


#: (OR-09) The outcome statuses the evening digest counts — the same set `build_day_digest` reads.
_DAY_OUTCOME_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


async def _exact_day_outcome_counts(db, day: date) -> dict[str, Any]:
    """The day's outcome counts, EXACT — the digest's own lists are BOUNDED (OR-09).

    `report_service.build_day_digest` reads at most `DAY_DIGEST_MAX_ROWS` outcome rows and derives
    its counts from that page (`len(digest.completed)`), so a 35-outcome day is reported as
    "25 completed" with `status="measured"` and no hint that 10 rows were clipped — the report
    presents a page as the day (F-43's own contract: a report that cannot see its data says so).

    This reads the SAME company-local window (`timeutil.day_window`, the digest's own window
    function, same default timezone) with a GROUP BY.  It returns measured counts, or a NAMED
    unknown (`status="unavailable"`, the `E_DB` code in `reason`) — never zeros for a failed read.
    The digest dataclass itself (and its `headline()`) belongs to `report_service`; this is the
    report WRITER's own measurement, and its result is additive to the stored digest.
    """
    from sqlalchemy import func, select

    from app.core.errors import PolsiaError
    from app.core.timeutil import day_window
    from app.models.task import Task
    from app.services.query_helpers import fetch_rows

    start, end = day_window(db, day=day)
    window = {"start": start.isoformat(), "end": end.isoformat()}
    try:
        rows = await fetch_rows(
            db,
            select(Task.status, func.count())
            .where(
                Task.updated_at >= start,
                Task.updated_at < end,
                Task.status.in_(_DAY_OUTCOME_STATUSES),
            )
            .group_by(Task.status),
            what=f"exact day outcome counts {day.isoformat()}",
        )
    except PolsiaError as exc:
        return {"status": "unavailable", "reason": exc.code, "by_status": {},
                "counted": None, "window": window}
    by_status = {str(status): int(count) for status, count in rows}
    return {
        "status": "measured",
        "reason": None,
        "by_status": by_status,
        "counted": sum(by_status.values()),
        "window": window,
    }


async def _evening_cycle() -> dict[str, Any]:
    from app.agents.accounting import BudgetStop
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity
    from app.services.company_service import get_full_context
    from app.services.report_service import (
        DAY_DIGEST_MAX_ROWS,
        build_day_digest,
        get_evening_summary_for,
        save_evening_summary,
    )

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
        # F-43(b): "yesterday" must be YESTERDAY's summary, not the newest row in the table.
        yesterday = day - timedelta(days=1)
        yesterday_summary = await get_evening_summary_for(db, yesterday)
        # F-43(a): what actually HAPPENED today.  The tick used to count only tasks CREATED today
        # (so yesterday's task finished today was invisible) and to record nothing about what the
        # agents PRODUCED.  `build_day_digest` reads the day's real outcomes, typed and bounded,
        # and answers `unavailable` (with the code) when it cannot read — a report that cannot see
        # its data says so instead of printing zeros.
        digest = await build_day_digest(db, day)
        # OR-09: the digest's lists are a bounded PAGE; measure the day itself in the same session
        # and same window, so the report can never present a clipped list as the whole day.
        exact_outcomes = await _exact_day_outcome_counts(db, day)

    context["yesterday_summary"] = yesterday_summary
    context["yesterday_report_date"] = yesterday.isoformat() if yesterday_summary else None

    counts = digest.counts_created
    shown_completed = len(digest.completed)
    shown_failed = len(digest.failed)
    shown_total = shown_completed + shown_failed + digest.cancelled
    digest_usable = digest.status in ("measured", "empty")
    if exact_outcomes["status"] == "measured" and digest_usable:
        by_status = exact_outcomes["by_status"]
        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        cancelled = by_status.get("cancelled", 0)
        counted = exact_outcomes["counted"]
        truncated: bool | None = counted > shown_total
        truncation: dict[str, Any] = {
            "truncated": truncated,
            "counted": counted,
            "shown": shown_total,
            "by_status": by_status,
            "bound": DAY_DIGEST_MAX_ROWS,
            "window": exact_outcomes["window"],
            "reason": None,
        }
    else:
        # Either the day could not be measured (exact read failed) or the digest itself could not
        # be read.  The counts fall back to the page the digest carries and the report says the
        # completeness is UNKNOWN (`truncated: None`) — never "complete" and never a fake total.
        by_status = {}
        completed = shown_completed
        failed = shown_failed
        cancelled = digest.cancelled
        counted = exact_outcomes["counted"] if exact_outcomes["status"] == "measured" else None
        truncated = None
        truncation = {
            "truncated": None,
            "counted": counted,
            "shown": shown_total,
            "by_status": None,
            "bound": DAY_DIGEST_MAX_ROWS,
            "window": exact_outcomes["window"],
            "reason": digest.reason or exact_outcomes.get("reason") or "digest_unavailable",
        }
    completeness_note: str | None = None
    if truncated:
        # The model reads the clipped list; the brief must state what the list is (a page), or the
        # summary it writes will describe 25 outcomes as the day.
        completeness_note = (
            f"DIGEST COMPLETENESS: the outcome lists above show {shown_total} of {counted} "
            f"rows for {day.isoformat()} (the digest bound is {DAY_DIGEST_MAX_ROWS}); the counts "
            "in this brief are the exact aggregate. Do not describe the shown rows as the whole "
            "day."
        )

    # H-03: the evening cycle's own model call is gated and recorded too (the second call site).
    try:
        result = await _run_recorded_cycle_agent(
            agent_type="orchestrator",
            task_dict={
                "title": "Evening reporting cycle",
                # F-26 + F-43: the reporting agent READS what the other agents produced today —
                # the day digest is part of its brief, not a similarity-memory guess.
                # OR-09: a clipped digest carries its own completeness statement INTO the brief,
                # so the agent never writes "25 things happened" for a 35-outcome day.
                "description": (
                    f"Generate evening summary for {day.isoformat()}\n\n{digest.prompt_block()}"
                    + (f"\n\n{completeness_note}" if completeness_note else "")
                ),
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
    model_summary = result.get("summary")
    if isinstance(model_summary, str) and model_summary.strip():
        summary_text = model_summary.strip()
    elif truncated:
        # F2/OR-09: `digest.headline()` is built from the BOUNDED page, so reusing it here would put
        # "25 completed" into `daily_reports.evening_summary` for a 30-outcome day — the exact
        # sentence OR-09 exists to stop.  The fallback carries the EXACT aggregate and states the
        # clip; the page never stands in for the day.
        summary_text = (
            f"{completed} completed, {failed} failed, {cancelled} cancelled today; the outcome "
            f"list is clipped to {shown_total} of {counted} rows (bound {DAY_DIGEST_MAX_ROWS}). "
            "The reporting agent returned no summary for this run."
        )
    else:
        # F-43: never the fabricated "Evening summary generated".  When the model returned no
        # summary the report states the records' own answer and says the model gave nothing.
        summary_text = (
            f"{digest.headline()}. The reporting agent returned no summary for this run."
        )
    raw_insights = result.get("insights")
    insights = [str(item) for item in raw_insights] if isinstance(raw_insights, list) else []
    if truncated:
        # OR-09: the persisted report itself states the clip (the model's `insights` are not the
        # only place a reader looks); the counts on the row above are the exact aggregate.
        insights.append(
            f"day digest clipped: {shown_total} of {counted} outcome rows shown "
            f"(the digest bound is {DAY_DIGEST_MAX_ROWS}); the task counts are exact"
        )
    day_digest_metrics = dict(digest.as_metrics())
    # OR-09: additive — the completeness measurement rides the digest the report already stores.
    day_digest_metrics["truncation"] = truncation

    async with session_factory() as db:
        await save_evening_summary(
            db,
            report_date=day,
            summary=summary_text,
            insights=insights,
            tasks_completed=completed,
            tasks_failed=failed,
            # F-43: the full digest rides the report's own JSON column — the counts alone could
            # not answer "what did the agents actually do today?".
            metrics_snapshot={
                **dict(context.get("kpis") or {}),
                "day_digest": day_digest_metrics,
            },
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
                # F-43: the report says what it could and could not see.
                "day_digest_status": digest.status,
                "day_digest_reason": digest.reason,
                # OR-09: the measured completeness of the outcome lists (None = could not measure).
                "day_digest_truncated": truncated,
                "day_digest_counted": counted,
                "day_digest_shown": shown_total,
                "tasks_cancelled": cancelled,
                "requeued": digest.requeued,
                "reaped_approvals": digest.reaped_approvals,
                "yesterday_report_date": context.get("yesterday_report_date"),
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
        "tasks_cancelled": cancelled,
        "counts": counts,
        "summary": summary_text,
        "day_digest": day_digest_metrics,
        # OR-09: `{"truncated": bool|None, "counted": int|None, "shown": int, "by_status": {...},
        # "bound": 25, "window": {...}, "reason": ...}` — the clipped-list disclosure.
        "day_digest_truncation": truncation,
        "reaped_approvals": digest.reaped_approvals,
        "window": window,  # L4-11: which company-local hour this tick matched (audit parity with the morning cycle)
    }
