"""Shared runtime helpers for the task modules: one event loop per process, idempotent
task creation, dedupe keys.

Why one loop per process: the upload built a fresh engine **and** ran
`asyncio.run()` per task (lane 06 measured 4.279 ms warm / 179 ms cold per engine and
0.698 ms per loop, with a fresh connection pool and no warm state).  `run_async` submits
the coroutine to a single long-lived loop that lives on a daemon thread, so every task in
this process reuses the same `app.core.database` engine, its pool and its loop.  A
background-thread loop (rather than a module-level loop + `run_until_complete`) is safe
from any Celery pool — solo, threads and prefork — and from `.apply()` in-process runs.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Coroutine
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import now_utc
from app.models.task import Task

logger = logging.getLogger(__name__)

#: The leaf types the ledger's JSON columns cannot store.  `Decimal` is the one that bit on the
#: live stack (a company context carries `owner_budget_usd` as `NUMERIC(10,2)`); the others are
#: the same class of value (a JSON column has no date/UUID type either).
_NON_JSON_LEAVES = (Decimal, datetime, date, time, UUID)


def json_safe(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    """Return `(a JSON-encodable copy of value, the paths whose leaf had to become text)`.

    WHY THIS EXISTS (measured on the live stack, wave 8): `agent_runs.input_context` is a JSONB
    column and `create_agent_run` hands it the company context, which carries
    `company.owner_budget_usd` as a `Decimal` (`NUMERIC(10,2)` at rest).  SQLAlchemy's JSON
    serializer is `json.dumps`, so the INSERT raised `TypeError: Object of type Decimal is not
    JSON serializable` AFTER the task row had been claimed: the delivery died, the row stayed
    `in_progress` with no run row and no settlement, and — with Beat off — nothing requeued it.
    The write boundary now converts the leaves a JSON column cannot hold into their `str()` form
    (the SAME convention `task_service` already uses for metadata:
    `json.dumps(metadata, default=str)`) and the caller LOGS every converted path, so a stored
    context is never silently different from the runtime value.

    Identity when nothing needs converting: an unchanged value is returned as the SAME object and
    an empty path list, so a context without such leaves writes byte-identical bytes to before.
    Deliberately NOT a general "stringify anything" fallback — an unknown type stays as it is and
    keeps failing loudly at the serializer; only the four JSON-less leaf types are converted.
    """
    if isinstance(value, dict):
        converted: dict[Any, Any] = {}
        converted_paths: list[str] = []
        changed = False
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            new_item, item_paths = json_safe(item, path=child_path)
            converted[key] = new_item
            converted_paths += item_paths
            changed = changed or new_item is not item
        return (converted, converted_paths) if changed else (value, [])
    if isinstance(value, list | tuple):
        items: list[Any] = []
        converted_paths = []
        changed = False
        for index, item in enumerate(value):
            new_item, item_paths = json_safe(item, path=f"{path}[{index}]")
            items.append(new_item)
            converted_paths += item_paths
            changed = changed or new_item is not item
        if not changed:
            return value, []
        return items, converted_paths
    if isinstance(value, _NON_JSON_LEAVES):
        return str(value), [path or "value"]
    return value, []


_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()
_loop_guard = threading.Lock()


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    _loop_ready.set()
    loop.run_forever()


def get_process_loop() -> asyncio.AbstractEventLoop:
    """The single event loop this worker process uses for all database work."""
    global _loop, _loop_thread
    with _loop_guard:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_ready.clear()
            _loop_thread = threading.Thread(
                target=_run_loop_forever, args=(_loop,), name="polsia-task-loop", daemon=True
            )
            _loop_thread.start()
            _loop_ready.wait(timeout=10)
        return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run `coro` on the process loop and block the Celery task until it returns.

    Exceptions propagate unchanged, so `self.retry`/error classification in the task
    modules sees the real exception type (a `PolsiaError` from the service layer, a
    `ValueError` from `crew_factory`, …).
    """
    loop = get_process_loop()
    if loop.is_closed():  # pragma: no cover - only if someone disposed the loop
        raise RuntimeError("task event loop is closed; restart the worker process")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


# ── dedupe keys ──────────────────────────────────────────────────────────────
# `<agent_type>:<utc-day>:<bucket>` — the shape the queue contract names.  The unique
# index `ix_tasks_dedupe_key` (migration 0002) is what actually enforces it; the string
# is the human-readable identity of the work.
def dedupe_key(agent_type: str, bucket: str, day: datetime | None = None) -> str:
    utc_day = (day or now_utc()).date().isoformat()
    return f"{agent_type}:{utc_day}:{bucket}"


def sweep_bucket(period_hours: int, now: datetime | None = None) -> str:
    """The tick identity of a sweep whose period is `period_hours` (0-padded hour block).

    Two ticks of the same period produce the same bucket, so at-least-once delivery
    cannot create two tasks; the *next* period produces a new bucket, so the sweep still
    runs on schedule.
    """
    current = now or now_utc()
    return f"{current.hour // period_hours:02d}"


def plan_bucket(title: str) -> str:
    """Stable bucket for an orchestrator-planned task: same day + same title ⇒ same task."""
    digest = hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"plan:{digest}"


# ── idempotent task creation ─────────────────────────────────────────────────
async def create_task_deduped(
    db: AsyncSession,
    *,
    title: str,
    agent_type: str,
    dedupe_key: str,
    description: str | None = None,
    priority: int = 3,
    source: str = "scheduler",
    scheduled_date: datetime | None = None,
    status: str = "pending",
    metadata: dict[str, Any] | None = None,
) -> Task | None:
    """Insert a task unless `dedupe_key` already exists.

    Returns the new `Task` (flushed, so `.id` is set) or **None** when the row already
    existed — the caller must treat None as "already created", not as a failure.

    Validation matches `task_service.create_task` (unknown agent types raise ValueError)
    so a malformed orchestrator plan is caught here rather than written.  The `status` value is
    asserted against `task_service.VALID_TASK_STATUSES` the same way (fix-5 REQUESTS §7/T17): this
    creator constructs `Task(...)` DIRECTLY, so before this assertion a caller could bypass the
    service-level check and store an out-of-set — or unset (`None`) — status on SQLite, where the
    `ck_tasks_status` CHECK is only carried by fresh `create_all` schemas.  The
    insert uses a SAVEPOINT: if a concurrent writer wins the unique index, this call
    re-checks and returns None instead of poisoning the caller's transaction.

    `metadata` is the per-task payload channel (`tasks.metadata`, the frozen JSON column the
    worker threads into the agent's prompt since RD12): `task_service.create_task` already
    accepted it, but this deduped path did not — so the CS sweep's per-item attach
    (`metadata.inbox_item`, RD12 receipt §7 REQUEST 1) could not carry its item until R17.
    Additive: the default (None) writes exactly the previous row.

    F2-FEED-2 (2026-10-28): a PAUSED company creates nothing here.  This is the one funnel every
    scheduler fan-out uses (cadence, follow-ups, the morning plan, the night task, the sweeps, the
    CS inbox attach), so the owner's STOP is enforced before the first INSERT rather than only at
    claim time — a Beat that resurrects can no longer queue new work (and therefore cannot spend on
    it later).  The refusal is NAMED (`E_POLICY_DENIED`) and no row is written; the tick bodies
    catch the same decision themselves and return a clean skip, so this raise is the
    defense-in-depth half for any future caller that forgets.
    """
    from app.core.errors import PolsiaError
    from app.services.task_service import VALID_AGENT_TYPES, VALID_TASK_STATUSES

    paused_reason = await dispatch_paused_reason(db)
    if paused_reason is not None:
        raise PolsiaError(
            PAUSED_DISPATCH_CODE,
            f"task creation refused: {paused_reason}",
            context={
                "title": title,
                "agent_type": agent_type,
                "dedupe_key": dedupe_key,
                "source": source,
                "created": 0,
            },
        )

    if agent_type not in VALID_AGENT_TYPES:
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
        )
    if status not in VALID_TASK_STATUSES:
        # Refused the same way as an unknown agent type: this is a caller-side value error, not a
        # named `E_VALIDATION` refusal — the service route converts ValueError into the agent-type
        # refusal, and a status typo must not wear that code (task_service.create_task's note).
        # Covers "unset" too: `None` is not in the set.
        raise ValueError(
            f"Unknown task status: {status!r}. Valid: {', '.join(VALID_TASK_STATUSES)}"
        )

    existing = await db.scalar(select(Task.id).where(Task.dedupe_key == dedupe_key).limit(1))
    if existing is not None:
        logger.info("dedupe hit: %s already exists (task %s)", dedupe_key, existing)
        return None

    task = Task(
        title=title,
        description=description,
        agent_type=agent_type,
        priority=priority,
        status=status,
        source=source,
        scheduled_date=scheduled_date,
        dedupe_key=dedupe_key,
    )
    if metadata is not None:
        # Pass-through set AFTER construction, not `metadata_json=None`: the JSON column type
        # stores a Python None as the JSON literal 'null' (none_as_null=False), so passing
        # None would silently change the stored representation of every existing sweep row.
        # The additive promise is "the default writes exactly the previous row" (measured:
        # tests/unit/celery/test_support_inbox_sweep.py — the bare sweep task stays SQL NULL).
        task.metadata_json = metadata
    db.add(task)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        # Another writer inserted the same key between the SELECT and the INSERT.
        # Only swallow it when the key is really present; anything else is a real error.
        # (The SAVEPOINT rollback normally restores `task` to transient; the guard
        # covers a session that keeps the pending instance.)
        if sa_inspect(task).session is not None:
            db.expunge(task)
        if await db.scalar(select(Task.id).where(Task.dedupe_key == dedupe_key).limit(1)):
            logger.info("dedupe race lost: %s won by another writer", dedupe_key)
            return None
        raise
    return task


# ── broker hand-off ──────────────────────────────────────────────────────────
async def stamp_rearmed(db: AsyncSession, task_ids: list[int], *, reason: str) -> list[int]:
    """Mark rows whose delivery was owed and did not arrive — the reconciler's ONE marker.

    `maintenance._reconcile_stranded_pending` selects ANY `pending` row carrying
    `metadata.rearmed_at` older than one window and hands it to `run_agent_task.delay`, so a marker
    without a delivery is a bounded retry instead of a stranded row.  Until this writer existed the
    marker was produced only by the pause path, which is why an orchestrator/manual/chat row whose
    dispatch was refused waited for `night_task` (A2-03).

    Merge — never replace — the per-task payload channel, and stamp in the CALLER's transaction: a
    rolled-back sweep must not leave a marker claiming a retry that was never owed.  The row's
    `status` is NOT touched here: re-arming is only ever correct for a row that is already
    `pending` (claimable), and `enqueue_created` is the caller that guarantees it.
    """
    if not task_ids:
        return []
    rows = (
        await db.execute(select(Task).where(Task.id.in_(list(task_ids))))
    ).scalars().all()
    stamp = now_utc().isoformat()
    stamped: list[int] = []
    for row in rows:
        from app.services.task_service import task_metadata_bag  # local import: no cycle

        merged = task_metadata_bag(row.metadata_json)
        merged["rearmed_at"] = stamp
        merged["rearm_reason"] = reason
        row.metadata_json = merged
        stamped.append(int(row.id))
    await db.flush()
    return stamped


async def enqueue_created(task_ids: list[int], *, label: str) -> None:
    """Hand committed task rows to the broker; a refusal RE-ARMS them, it never terminalises them.

    The scheduler paths commit rows `pending` and then dispatch.  Measured defect (audit lane 4,
    L4-F1, wave-4/T19 dead-broker evidence): with the broker down, `run_agent_task.delay(...)`
    raised into the caller and nothing reconciled the undelivered rows.

    A2-03: the old fallback marked EVERY row `failed` on any exception, on the strength of a comment
    claiming "NO delivery was accepted" — which a `group` publish cannot know: a group is published
    one message at a time, so a connection that dies mid-publish leaves a PREFIX live in the queue
    while the code writes `failed` on all of them, and `claim_task` accepts only `pending`.  The
    work was queued AND written off.  A refusal proves nothing about what the broker took, so the
    rows stay `pending` and each is stamped with `metadata.rearmed_at`: a delivered row is claimed
    (the claim is its proof of life) and an undelivered one is re-dispatched by
    `maintenance._reconcile_stranded_pending` within one window.  The named refusal still reaches
    the caller — a fake queue would be worse than a loud one.
    """
    from celery_app.tasks.agent_tasks import run_agent_task  # lazy: agent_tasks imports this module

    try:
        if len(task_ids) == 1:
            run_agent_task.delay(task_ids[0])
        else:
            from celery import group

            group([run_agent_task.s(task_id) for task_id in task_ids]).apply_async()
    except Exception as exc:  # noqa: BLE001 — a broker refusal is NAMED below; the rows stay queued
        from app.core.database import get_session_factory
        from app.core.errors import E_REDIS, PolsiaError

        reason = f"enqueue_refused:{exc.__class__.__name__}"
        logger.warning(
            "failed to enqueue task(s) for %s (%s); re-arming %s so the reconciler re-dispatches "
            "the undelivered ones", label, reason, len(task_ids), exc_info=True,
        )
        session_factory = get_session_factory()
        async with session_factory() as db:
            rearmed = await stamp_rearmed(db, task_ids, reason=reason)
            await db.commit()
        raise PolsiaError(
            E_REDIS,
            (
                f"{label}: {len(task_ids)} task(s) could not be handed to a worker; they stay "
                f"queued and are re-dispatched by the stranded-row reconciler"
            ),
            context={"task_ids": task_ids, "rearmed": rearmed, "reason": reason},
        ) from exc


# ── the run's action list (§4.13 / F-13) ─────────────────────────────────────
#: The `tasks.metadata` key carrying a task's CROSS-ATTEMPT action list.  The runtime publishes the
#: same records under the same name on its envelope (`deepseek_runtime.ACTION_KEY`); the two
#: literals are pinned equal by `tests/unit/test_a3_writepath.py`.
ACTION_KEY = "actions"
#: §4.13's list bound.  The per-ENTRY bound (4 KB) is applied where the record is built — the
#: runtime is the only layer that holds the tool result — so this module owns the list itself.
MAX_ACTIONS = 50
#: The only status a record can terminalise to: the call was made and this is its answer.  A
#: `replayed` record points at an effect that already existed, and an `evicted` row is the summary
#: the bound leaves behind (it carries a count, never a `call_key`).
_EXECUTED = "executed"


def prior_actions(metadata: Any) -> list[dict[str, Any]]:
    """The task's recorded action list (`[]` when absent) — never a coerced value.

    Read by the worker at the CLAIM and handed to the agent as `task_dict["prior_actions"]`, so the
    replay set is the one committed when the task was claimed: a concurrent writer cannot hand a
    run a half-list, and an absent/unreadable value is an empty list, never a fabricated record.
    """
    if isinstance(metadata, dict):
        records = metadata.get(ACTION_KEY)
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    return []


def merge_actions(existing: Any, new: Any) -> list[dict[str, Any]]:
    """Merge two action lists by `call_key` into the persisted list (§4.13).

    * the newest record for a `call_key` wins, EXCEPT that a terminal `executed` record beats a
      later `replayed` one — the effect happened once, and a replay must not erase that fact;
    * keyless and non-object entries are DROPPED: a record with no `call_key` cannot be replayed,
      and persisting it would claim otherwise;
    * at most `MAX_ACTIONS` rows.  Past the bound the OLDEST `executed` rows are dropped and ONE
      `{"status": "evicted", "count": N}` summary row records how many, cumulative and always last.
    """
    incoming: list[Any] = []
    for candidate in (existing, new):
        if isinstance(candidate, list):
            incoming.extend(candidate)

    kept: list[dict[str, Any]] = []
    slot: dict[str, int] = {}
    evicted = 0
    for item in incoming:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "evicted":
            try:
                evicted += int(item.get("count") or 0)
            except (TypeError, ValueError):
                continue
            continue
        key = item.get("call_key")
        if not isinstance(key, str) or not key:
            continue
        if key in slot:
            current = kept[slot[key]]
            if current.get("status") != _EXECUTED and status == _EXECUTED:
                kept[slot[key]] = item
            continue
        slot[key] = len(kept)
        kept.append(item)

    # One row of the bound is the summary row once anything has been evicted, so the survivors are
    # held one short — otherwise appending the summary would push the list over `MAX_ACTIONS`.
    capacity = MAX_ACTIONS - 1 if (evicted or len(kept) > MAX_ACTIONS) else MAX_ACTIONS
    if len(kept) > capacity:
        dropped = len(kept) - capacity
        kept = kept[dropped:]
        evicted += dropped
    if evicted:
        kept.append({"status": "evicted", "count": evicted})
    return kept


def actions_from_outcome(result: Any, failure: BaseException | None) -> list[dict[str, Any]]:
    """The list a finished (or failed) run produced: the result's key, else the error's context.

    A COMPLETED run carries its list in the result (`agent_runs.output` receives the same copy); a
    FAILED run stores no output at all (`finish_agent_run(output=None)`), so its list exists only in
    the named refusal's context — which is precisely the case the retry needs.
    """
    records = result.get(ACTION_KEY) if isinstance(result, dict) else None
    if not isinstance(records, list):
        context = getattr(failure, "context", None)
        records = context.get(ACTION_KEY) if isinstance(context, dict) else None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


# ── paused dispatch: the owner's STOP, enforced where work is CREATED ────────
#: The named refusal a PAUSED company produces at the CREATION site (F2-FEED-2, 2026-10-28).
#:
#: `company_config.dispatch_paused` is the owner's Settings Pause/Resume switch.  The worker's
#: dispatch gate (`agent_tasks.run_agent_task`) already refuses at CLAIM time — but a Beat that
#: comes back (compose recreate / `restart: unless-stopped`) could still CREATE scheduler rows and
#: a cycle could still spend on its own orchestrator call while the owner has pressed STOP.  These
#: helpers are the creation-side half: a paused Beat creates NOTHING and reports the refusal BY
#: NAME, so a resurrected scheduler cannot silently spend.  Live evidence that this mattered:
#: 2026-10-28 19:30 UTC, a restarted worker's queued reconciler tick re-dispatched four stranded
#: rows and four real DeepSeek calls were made with no owner action (agent_runs 295-298, $0.0029).
PAUSED_DISPATCH_CODE = "E_POLICY_DENIED"
PAUSED_DISPATCH_REASON = (
    "E_POLICY_DENIED: company paused by owner (company_config.dispatch_paused=true; "
    "resume in Settings to allow dispatch)"
)


async def dispatch_paused_reason(db: AsyncSession) -> str | None:
    """The owner's STOP resolved at the creation site, or None when dispatch is open.

    Read through `crew_factory.resolve_dispatch_paused` — the SAME resolver the worker's claim-time
    gate uses, so creation and dispatch cannot disagree about whether the company is paused (no
    second source of truth, no env override to drift).
    """
    from app.agents.crew_factory import resolve_dispatch_paused

    if await resolve_dispatch_paused(db):
        return PAUSED_DISPATCH_REASON
    return None


def paused_skip(site: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    """The named skip a Beat body returns when the owner paused dispatch (no row, no spend).

    Shape: `status="skipped"`, `reason="dispatch_paused"`, the policy code, the human-readable
    reason, and the calling site — plus whatever counts the caller reports (`created=0` etc.).
    A Beat tick that refuses must be observable in the worker log and in the task result, the same
    contract `run_agent_task`'s paused refusal keeps.
    """
    return {
        "status": "skipped",
        "reason": "dispatch_paused",
        "code": PAUSED_DISPATCH_CODE,
        "detail": reason or PAUSED_DISPATCH_REASON,
        "site": site,
        **extra,
    }


# ── dispatch budget gate ─────────────────────────────────────────────────────
async def day_spend_by_agent(db: AsyncSession) -> tuple[float, dict[str, float]]:
    """MEASURED spend for the company-local day: (total, per-agent) from `agent_runs.cost_usd`.

    Money is exact at rest (A-T17/T18); an absent day is a measured 0, never a guess. The window
    starts at the company-local midnight, bound through `as_bound_instant` for the SQLite dialect
    (the same pattern the claim path uses).
    """
    from sqlalchemy import func as sa_func

    from app.core.timeutil import as_bound_instant, day_bounds, today_in
    from app.models.task import AgentRun

    start, _end = day_bounds(today_in())
    rows = (
        await db.execute(
            select(AgentRun.agent_type, sa_func.coalesce(sa_func.sum(AgentRun.cost_usd), 0))
            .where(AgentRun.started_at >= as_bound_instant(start, db))
            .group_by(AgentRun.agent_type)
        )
    ).all()
    per_agent = {str(agent_type): float(total) for agent_type, total in rows}
    return sum(per_agent.values()), per_agent


async def assert_budget_allows(agent_type: str) -> None:
    """Dispatch-time budget gate (D3; audit A1 / lane-2 L2-F01). Raises `BudgetStop` when the day is full.

    The DAG planner's `authorize` gate had no production caller; this puts the same decision on
    the paths that actually dispatch. The policy is what the settings DECLARE
    (`agent_daily_cost_cap_usd` / `agent_run_cost_cap_usd` via `accounting.budget_policy_from_settings`;
    kill switch frozen when `POLSIA_KILL_SWITCH` says freeze). The estimate is the policy's own
    per-run cap — the flat plan carries no per-node estimate, so the gate uses the conservative
    worst case rather than an invented small number.

    F-26 (wave 3): the day's measured total is now a LOWER BOUND in one specific way — a settled
    run whose cost was never measured (an interrupted run from before the live-usage sink, a
    provider that reported no usage) contributes nothing to the sum. The refusal's context names
    `day_runs_with_unmeasured_cost` so the arithmetic that let a run through (or stopped it) is
    never presented as complete when part of it is unknown.
    """
    from app.agents.accounting import authorize, budget_policy_from_settings
    from app.agents.policy_firewall import kill_grade_from_env
    from app.core.database import get_session_factory

    policy = budget_policy_from_settings(kill_switch=kill_grade_from_env() == "freeze")
    async with get_session_factory()() as db:
        total, per_agent = await day_spend_by_agent(db)
        unmeasured = await day_runs_with_unmeasured_cost(db)
    try:
        authorize(  # raises BudgetStop; the caller decides skip-vs-fail
            policy,
            agent_type=agent_type,
            est_cost_usd=policy.per_run_cap_usd,
            day_spent_usd=total,
            agent_spent_usd=per_agent.get(agent_type, 0.0),
        )
    except Exception as exc:
        context = getattr(exc, "context", None)
        if isinstance(context, dict):
            context.setdefault("day_runs_with_unmeasured_cost", unmeasured)
        raise


async def day_runs_with_unmeasured_cost(db: AsyncSession) -> int:
    """How many of today's SETTLED runs carry no measured cost (the day cap's blind spot).

    F-26: `agent_runs.cost_usd` is NULL for a run whose spend was never measured — an interrupted
    run from before the live usage sink existed, or a provider that reported no usage. Such a run
    adds nothing to `day_spend_by_agent`, so the day's total is a lower bound by exactly this
    count. A `running` row is NOT counted here: with the live sink it is still reporting, and
    counting it as unmeasured would double-count the moment it settles.
    """
    from sqlalchemy import func as sa_func

    from app.core.timeutil import as_bound_instant, day_bounds, today_in
    from app.models.task import AgentRun

    start, _end = day_bounds(today_in())
    total = await db.execute(
        select(sa_func.count())
        .select_from(AgentRun)
        .where(
            AgentRun.started_at >= as_bound_instant(start, db),
            AgentRun.cost_usd.is_(None),
            AgentRun.status != "running",
        )
    )
    value = total.scalar_one_or_none()
    return int(value or 0)


__all__ = [
    "ACTION_KEY",
    "MAX_ACTIONS",
    "PAUSED_DISPATCH_CODE",
    "PAUSED_DISPATCH_REASON",
    "actions_from_outcome",
    "assert_budget_allows",
    "create_task_deduped",
    "day_runs_with_unmeasured_cost",
    "day_spend_by_agent",
    "dedupe_key",
    "dispatch_paused_reason",
    "enqueue_created",
    "get_process_loop",
    "merge_actions",
    "paused_skip",
    "plan_bucket",
    "prior_actions",
    "run_async",
    "stamp_rearmed",
    "sweep_bucket",
]
