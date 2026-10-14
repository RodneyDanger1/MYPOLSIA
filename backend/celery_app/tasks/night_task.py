"""The Night Task — a bounded, off-by-default drain of the queue top, plus ONE receipt.

What this is (and is not), stated honestly:

* The clone **drains continuously**: queued rows are claimed and run by the queue workers as
  soon as they exist.  The night task is therefore NOT a backlog-clearing mechanism — it is a
  **bounded drain + receipt on top** of the existing dispatch: at the configured company-local
  hour it hands the next `night_task_limit` rows the dispatcher would itself take to the SAME
  dispatcher (`_common.enqueue_created` -> `run_agent_task`), and writes one activity row saying
  how many it ran.  An empty queue is truthfully "ran 0 of 3 — the queue was empty"; nothing is
  fabricated to look busy.
* **Off by default.**  `company_config.night_task_enabled` is FALSE on every existing row after
  migration 0007.  A disabled deployment's tick returns `{"status": "off"}` and writes NOTHING —
  no activity row, no task touch, no broker contact.  The queue drain that workers already do is
  unchanged.
* **The rows it selects are NOT claimed here.**  `claim_task` accepts only `pending` rows, so a
  scheduler that pre-claimed its picks would make every dispatched message skip with
  `E_TASK_NOT_RUNNABLE` and strand the row until the lease sweep.  The selection is a READ in the
  dispatcher's own order (`task_service.claimable_task_ids` — same filter, same
  `priority, coalesce(scheduled_date, created_at), id` ordering as `claim_task`); the state
  transition stays on the existing claim path, which is what "no parallel semantics" means.

Timing.  The beat entry fires **hourly** and this task self-gates exactly like the daily cycles
(`daily_cycle._resolve_morning_hour` / `_morning_window` patterns): the hour is company-local
(`core/timeutil.company_tz()`), read from `company_config.night_task_hour`, and an UNSET value
(NULL) resolves to the documented 23:00 default.  A stored value outside 0-23 (and a
`night_task_limit` outside 1-5) is a named `E_CONFIG` refusal, never a silent fallback.  A tick
outside the window returns a named skip and writes nothing.

Receipt.  Exactly ONE `log_activity` per in-window tick, `action="night_task_ran"`, summary
`Night task ran X of Y` (Y = the configured limit), with the selected tasks' ids/titles and the
skip reasons in `detail` — so the morning card's feed answers "what did the night do?" without a
second row per task.  When the broker hand-off FAILS, `enqueue_created` marks the rows `failed`
with the measured reason and re-raises: no receipt is written then, because "ran X of Y" would be
false (the same contract every scheduler fan-out keeps).
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.timeutil import company_tz, now_utc
from celery_app.tasks._common import enqueue_created, run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

#: The documented default when `company_config.night_task_hour` holds NULL (the plan's "unset ->
#: 23:00").  A stored value always wins; this is a default, never a write-back.
DEFAULT_NIGHT_HOUR = 23

#: The activity action the receipt rides (`app/api/v1/dashboard` activity feed + the B5 morning
#: card read it from there).
RECEIPT_ACTION = "night_task_ran"

#: The accepted limit band (mirrors the wire validator in `schemas/company.py`).
LIMIT_MIN, LIMIT_MAX = 1, 5


@app.task(name="celery_app.tasks.night_task.run_night_task")
def run_night_task() -> dict[str, Any]:
    """Hourly Beat tick — self-gates on the company-local Night Task Hour, then drains <= limit."""
    return run_async(_night_task())


def _config_refusal(value: object, field: str, expected: str):
    """A named `E_CONFIG` refusal for a malformed stored value (the `_resolve_morning_hour` shape)."""
    from app.core.errors import E_CONFIG, ERROR_HINTS, PolsiaError

    return PolsiaError(
        E_CONFIG,
        f"company_config.{field} {value!r} is not a valid night task setting: {expected}",
        how_to_fix=ERROR_HINTS[E_CONFIG],
        context={"value": value, "source": "company", "expected": expected},
    )


def _resolve_night_hour(company: Any) -> tuple[int, str]:
    """The company-local hour the night task runs at, and which source supplied it.

    `None` (the nullable column's NULL) means "unset" and resolves to the documented 23:00
    default — reported as source `"default"`, never written back to the row.  Anything else
    outside 0-23 is a named `E_CONFIG` refusal (the morning cycle's rule), never a fallback.
    """
    value = company.night_task_hour
    if value is None:
        return DEFAULT_NIGHT_HOUR, "default"
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
        raise _config_refusal(value, "night_task_hour", "the accepted range is an integer 0-23")
    return value, "company"


def _resolve_limit(company: Any) -> int:
    """The stored drain limit, validated against the frozen 1-5 band (named refusal outside it)."""
    value = company.night_task_limit
    if isinstance(value, bool) or not isinstance(value, int) or not LIMIT_MIN <= value <= LIMIT_MAX:
        raise _config_refusal(
            value, "night_task_limit", f"the accepted range is an integer {LIMIT_MIN}-{LIMIT_MAX}"
        )
    return value


async def _night_task() -> dict[str, Any]:
    """The async body of `run_night_task` (runs on the process loop)."""
    from sqlalchemy import func, select

    from app.core.database import get_session_factory
    from app.models.task import Task
    from app.services.activity_service import log_activity
    from app.services.company_service import get_company
    from app.services.task_service import claimable_task_ids

    session_factory = get_session_factory()

    # ── 1. the enabled gate — OFF writes NOTHING (no activity, no task touch, no broker) ─────
    async with session_factory() as db:
        company = await get_company(db)
        if company is None or not company.night_task_enabled:
            logger.info("night task: disabled (company_config.night_task_enabled is off/unset)")
            return {"status": "off"}
        # Only an ENABLED deployment resolves (and can refuse) the hour + limit: a malformed
        # stored hour in a deployment that never turned the feature on must not fail a tick that
        # is contractually a no-op.  Both refusals are named E_CONFIG, never a silent default.
        limit = _resolve_limit(company)
        hour, hour_source = _resolve_night_hour(company)

    # ── 2. the self-gate: only the company-local [H:00, H+1:00) window runs ──────────────────
    now = now_utc()
    tz = company_tz()
    day = now.astimezone(tz).date()  # the company's local day, like every other cycle
    # Lazy on purpose: `daily_cycle` imports `celery_app.worker`, which eagerly imports THIS
    # module; a module-level import here closes the cycle (daily_cycle -> worker -> night_task ->
    # daily_cycle) and breaks collection for any test that imports daily_cycle directly (caught
    # by the full gate in the U5 closeout, after every scoped run had passed).
    from celery_app.tasks.daily_cycle import _morning_window

    start, end = _morning_window(day, hour, tz)
    window = {
        "hour": hour,
        "source": hour_source,
        "tz": tz.key,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if not (start <= now < end):
        logger.info(
            "night task skipped (outside_night_window): now=%s window=[%s, %s) hour=%s source=%s tz=%s",
            now.isoformat(),
            start.isoformat(),
            end.isoformat(),
            hour,
            hour_source,
            tz.key,
        )
        return {"status": "skipped", "reason": "outside_night_window", "window": window}

    # ── 3. ONE transaction: select the dispatcher's own TOP-N (read-only), plus receipts data ─
    async with session_factory() as db:
        task_ids = await claimable_task_ids(db, limit=limit)
        titles: list[str] = []
        if task_ids:
            rows = (
                await db.execute(
                    select(Task.id, Task.title).where(Task.id.in_(task_ids))
                )
            ).all()
            by_id = {int(task_id): str(title) for task_id, title in rows}
            titles = [by_id[task_id] for task_id in task_ids if task_id in by_id]
        # The skip reasons are MEASURED, not assumed: how much was runnable-but-over-limit, and
        # how much is pending but not runnable yet (scheduled in the future — the claim filter
        # excludes it).  An empty queue reports "empty", an empty RUNNABLE queue reports why.
        pending_total = int(
            (await db.scalar(select(func.count()).select_from(Task).where(Task.status == "pending")))
            or 0
        )
        runnable_total = len(await claimable_task_ids(db, limit=max(pending_total, 1)))

    skipped: list[str] = []
    remaining = runnable_total - len(task_ids)
    if remaining > 0:
        skipped.append(f"{remaining} more runnable pending task(s) beyond the limit of {limit}")
    not_yet = pending_total - runnable_total
    if not_yet > 0:
        skipped.append(f"{not_yet} pending task(s) are not runnable yet (scheduled in the future)")
    if pending_total == 0:
        skipped.append("the queue was empty")

    # ── 4. broker hand-off AFTER the read transaction: a dead broker marks the rows failed and
    #    raises (enqueue_created's contract) — and NO receipt is written then, because "ran X of Y"
    #    would be false. ────────────────────────────────────────────────────────────────────────
    if task_ids:
        await enqueue_created(task_ids, label="night task")

    # ── 5. the receipt: exactly ONE activity row, summary + titles + skip reasons ────────────
    summary = f"Night task ran {len(task_ids)} of {limit}"
    async with session_factory() as db:
        await log_activity(
            db,
            agent_type="orchestrator",
            action=RECEIPT_ACTION,
            summary=summary,
            level="success" if task_ids else "info",
            detail={
                "limit": limit,
                "ran": len(task_ids),
                "task_ids": task_ids,
                "titles": titles,
                "skipped": skipped,
                "pending_total": pending_total,
                "runnable_total": runnable_total,
                "day": day.isoformat(),
                "window": window,
                # The honest framing, carried on the row itself: the clone drains continuously,
                # so the night task is a bounded drain + receipt, never a backlog-clearer.
                "note": "bounded drain of the queue top; rows stay pending until a worker claims them",
            },
        )
        await db.commit()

    logger.info(
        "night task: ran=%s limit=%s pending=%s skipped=%s day=%s",
        len(task_ids),
        limit,
        pending_total,
        skipped,
        day,
    )
    return {
        "status": "completed",
        "ran": len(task_ids),
        "limit": limit,
        "task_ids": task_ids,
        "titles": titles,
        "skipped": skipped,
        "pending_total": pending_total,
        "window": window,
    }
