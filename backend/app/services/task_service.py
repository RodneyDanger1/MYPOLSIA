"""Task + AgentRun persistence — and the queue's claim discipline.

Three properties this module is responsible for, each with the measurement that
motivated it (lane 06, `research/06-data-perf-business/DATA-PERF.md`):

1. **A task is claimed atomically.**  The original shape was "SELECT the first
   pending row, then UPDATE it", which lets two workers run the same task.  The
   claim here is ONE statement: `UPDATE tasks SET ... WHERE id = (SELECT id ...
   LIMIT 1 [FOR UPDATE SKIP LOCKED]) RETURNING *`.  PostgreSQL skips rows another
   worker holds (`SKIP LOCKED`); SQLite gets the same exclusion from its
   single-writer lock plus the single-statement compare-and-set.
2. **A worker that dies does not lose its task.**  `heartbeat_at` + the lease in
   `settings.task_claim_lease_seconds` make a claim reclaimable by
   `requeue_stale_claims`.
3. **Repeating a sweep is safe.**  `create_task(dedupe_key=...)` is a single
   `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING RETURNING id` — never a
   check-then-insert, which is a race by construction.

Also here: `get_tasks_today_summary` is ONE grouped aggregate over
`day_window(...)` (was two queries, and on PostgreSQL the original's naive-local
window selected 0 of 303 rows for the same day — `matrix-s200k.json`
`day_window_rowcounts`), and `get_agent_statuses` is ONE statement that returns
exactly nine rows even on an empty database (the correlated-subquery form lane 06
measured at 84.8 ms vs 692.5 ms for the grouped-aggregate variant).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import utcnow
from app.core.errors import (
    E_NOT_FOUND,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.core.timeutil import as_bound_instant, day_window, dialect_name, today_in
from app.models.task import AgentRun, Task
from app.services.query_helpers import agent_rollup_expr, fetch_rows

VALID_AGENT_TYPES: tuple[str, ...] = (
    "orchestrator",
    "business_planning",
    "competitor_research",
    "social_media",
    "ads_management",
    "email_outreach",
    "code_generation",
    "customer_support",
    "finance",
)

VALID_TASK_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
)


def is_valid_agent_type(agent_type: str) -> bool:
    return agent_type in VALID_AGENT_TYPES


def _resolve_lease(lease_seconds: int | None) -> int:
    """The lease actually in force, validated.

    NOTE (declared limit): the frozen `Task` model has no per-row lease column, so
    a claim cannot *persist* a custom lease — `requeue_stale_claims` sweeps with
    `settings.task_claim_lease_seconds` unless the caller passes the same
    `lease_seconds` it claimed with.  A per-claim lease needs the column; that is
    REQUESTS #1 in the lane receipt.
    """
    lease = int(lease_seconds) if lease_seconds is not None else int(settings.task_claim_lease_seconds)
    if lease <= 0:
        raise PolsiaError(
            E_VALIDATION,
            f"lease_seconds must be > 0, got {lease!r}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"lease_seconds": lease},
        )
    return lease


async def create_task(
    db: AsyncSession,
    title: str,
    agent_type: str,
    description: str | None = None,
    priority: int = 3,
    source: str = "orchestrator",
    scheduled_date: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "pending",
    dedupe_key: str | None = None,
) -> Task:
    """Create (and flush) a Task.  Returns the ORM object with `.id` populated.

    With `dedupe_key` the insert is idempotent: two callers racing the same key
    (the same day's plan, a retried sweep) get ONE row — the loser re-selects the
    winner's row instead of raising `IntegrityError` or creating a duplicate.
    """
    if not is_valid_agent_type(agent_type):
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
        )

    values: dict[str, Any] = {
        "title": title,
        "description": description,
        "agent_type": agent_type,
        "priority": priority,
        "status": status,
        "source": source,
        "scheduled_date": scheduled_date,
        "metadata_json": metadata,
    }

    if dedupe_key is None:
        task = Task(**values)
        db.add(task)
        await db.flush()
        return task

    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(Task)
        .values(**values, dedupe_key=dedupe_key)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Task)
    )
    created = (await db.execute(stmt)).scalars().one_or_none()
    if created is not None:
        return created

    existing = (
        await db.execute(select(Task).where(Task.dedupe_key == dedupe_key))
    ).scalar_one_or_none()
    if existing is None:
        # Only reachable if a NULL-key insert raced or the conflicting row was
        # deleted in between; either way we must not report a fake success.
        raise PolsiaError(
            E_VALIDATION,
            f"dedupe_key {dedupe_key!r} conflicted but no row could be re-read",
            how_to_fix="retry create_task; if it persists, check the ix_tasks_dedupe_key unique index",
            context={"dedupe_key": dedupe_key},
        )
    return existing


async def get_task(db: AsyncSession, task_id: int) -> Task | None:
    result = await db.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()


async def list_tasks(
    db: AsyncSession,
    status: str | None = None,
    agent_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Task]:
    """Newest first, index-friendly: explicit LIMIT/OFFSET and a deterministic
    `(created_at DESC, id DESC)` order (ties cannot reorder the page)."""
    stmt = select(Task)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if agent_type is not None:
        stmt = stmt.where(Task.agent_type == agent_type)
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_task_status(
    db: AsyncSession,
    task_id: int,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> Task | None:
    """Set status (+ optional result/error).  Returns None when the task is absent.

    `result_summary`/`error_message` are assigned only when not None, so a
    completion cannot wipe a previous error message (and vice versa).
    """
    task = await get_task(db, task_id)
    if task is None:
        return None
    task.status = status
    if result_summary is not None:
        task.result_summary = result_summary
    if error_message is not None:
        task.error_message = error_message
    task.updated_at = utcnow()
    await db.flush()
    return task


# ── the queue: claim / heartbeat / release / requeue ─────────────────────────


def _claimable_filter(db: AsyncSession, *, task_id: int | None, agent_type: str | None):
    """The selectable half of the claim: one sargable row.

    `scheduled_date IS NULL OR scheduled_date <= now` is expressed as two
    comparisons rather than a `COALESCE(...) <= now` on the *column*, because the
    frozen model has no generated `runnable_at` column to index (REQUESTS #2).
    Rows with no schedule are runnable immediately, which is what makes
    `ORDER BY priority, coalesce(scheduled_date, created_at), id` deterministic
    on both engines (PostgreSQL sorts NULLs last on ASC, SQLite first — the
    coalesce removes the difference).
    """
    now = as_bound_instant(utcnow(), db)
    conditions = [
        Task.status == "pending",
        or_(Task.scheduled_date.is_(None), Task.scheduled_date <= now),
    ]
    if task_id is not None:
        conditions.append(Task.id == task_id)
    if agent_type is not None:
        conditions.append(Task.agent_type == agent_type)
    return conditions


async def claim_task(
    db: AsyncSession,
    *,
    worker: str,
    lease_seconds: int | None = None,
    task_id: int | None = None,
    agent_type: str | None = None,
) -> Task | None:
    """Atomically take the next runnable task, or return None when there is none.

    ONE statement on both engines:

        UPDATE tasks SET status='in_progress', claimed_by=:worker,
                         claimed_at=:now, heartbeat_at=:now, attempts=attempts+1
        WHERE id = (SELECT id FROM tasks
                    WHERE status='pending' AND (scheduled_date IS NULL OR
                                               scheduled_date <= :now)
                    ORDER BY priority, coalesce(scheduled_date, created_at), id
                    LIMIT 1 [FOR UPDATE SKIP LOCKED])      -- PostgreSQL only
        RETURNING *;

    Two workers can therefore never receive the same row: PostgreSQL hands each
    one a distinct row (`SKIP LOCKED`, documented pattern), and SQLite serialises
    the statement behind its single writer lock.

    `populate_existing=True` is deliberate: without it, SQLAlchemy returns the
    *stale* identity-map copy when the caller already loaded that task in this
    session (measured: `probe_populate_existing.py` case 1 vs case 2), and the
    worker would run a task it believes is still `pending`.

    With an explicit `task_id`, "no rows" is not silently None — it raises
    `E_NOT_FOUND` / `E_TASK_NOT_RUNNABLE` so a caller cannot mistake "someone else
    owns it" for "nothing to do".
    """
    if not worker or not str(worker).strip():
        raise PolsiaError(
            E_VALIDATION,
            "claim_task requires a non-empty worker id",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
        )
    _resolve_lease(lease_seconds)  # validates; the lease itself is applied by requeue_stale_claims
    if agent_type is not None and not is_valid_agent_type(agent_type):
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
        )

    now = as_bound_instant(utcnow(), db)
    inner = (
        select(Task.id)
        .where(*_claimable_filter(db, task_id=task_id, agent_type=agent_type))
        .order_by(
            Task.priority.asc(),
            func.coalesce(Task.scheduled_date, Task.created_at).asc(),
            Task.id.asc(),
        )
        .limit(1)
    )
    if dialect_name(db) == "postgresql":
        inner = inner.with_for_update(skip_locked=True)

    stmt = (
        update(Task)
        .where(Task.id == inner.scalar_subquery())
        .values(
            status="in_progress",
            claimed_by=str(worker),
            claimed_at=now,
            heartbeat_at=now,
            attempts=func.coalesce(Task.attempts, 0) + 1,
        )
        .returning(Task)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    claimed = (await db.execute(stmt)).scalars().one_or_none()
    if claimed is not None:
        return claimed

    if task_id is None:
        return None

    current = await get_task(db, task_id)
    if current is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task {task_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_id},
        )
    raise PolsiaError(
        E_TASK_CLAIMED if current.claimed_by else E_TASK_NOT_RUNNABLE,
        f"task {task_id} cannot be claimed: status={current.status!r}, "
        f"claimed_by={current.claimed_by!r}",
        how_to_fix=ERROR_HINTS[E_TASK_CLAIMED],
        context={"task_id": task_id, "status": current.status, "claimed_by": current.claimed_by},
    )


async def heartbeat_task(db: AsyncSession, task_id: int, worker: str) -> Task:
    """Renew THIS worker's claim.  Raises a named refusal when it no longer holds it.

    A heartbeat that silently no-ops would let a worker keep executing a task whose
    lease has been reassigned to someone else, so a lost claim is an error:
    `E_TASK_CLAIMED` (another worker owns it) or `E_TASK_NOT_RUNNABLE` (the row is
    no longer in flight).
    """
    now = as_bound_instant(utcnow(), db)
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            Task.claimed_by == str(worker),
            Task.status == "in_progress",
        )
        .values(heartbeat_at=now)
        .returning(Task)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    refreshed = (await db.execute(stmt)).scalars().one_or_none()
    if refreshed is not None:
        return refreshed

    current = await get_task(db, task_id)
    if current is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task {task_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_id, "worker": str(worker)},
        )
    if current.claimed_by is not None and current.claimed_by != str(worker):
        raise PolsiaError(
            E_TASK_CLAIMED,
            f"task {task_id} is claimed by {current.claimed_by!r}, not {worker!r}",
            how_to_fix=ERROR_HINTS[E_TASK_CLAIMED],
            context={"task_id": task_id, "worker": str(worker), "claimed_by": current.claimed_by},
        )
    raise PolsiaError(
        E_TASK_NOT_RUNNABLE,
        f"task {task_id} is {current.status!r}; only an in_progress claim can be heartbeated",
        how_to_fix=ERROR_HINTS[E_TASK_NOT_RUNNABLE],
        context={"task_id": task_id, "worker": str(worker), "status": current.status},
    )


async def release_claim(db: AsyncSession, task_id: int, worker: str) -> bool:
    """Give a claim back (graceful shutdown / re-plan).  ONE statement.

    Returns True when this worker's claim was released back to `pending`; a task
    held by somebody else raises `E_TASK_CLAIMED`, a missing task `E_NOT_FOUND` —
    a `False`-only contract would hide both.
    """
    stmt = (
        update(Task)
        .where(Task.id == task_id, Task.claimed_by == str(worker))
        .values(
            status="pending",
            claimed_by=None,
            claimed_at=None,
            heartbeat_at=None,
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    released = (await db.execute(stmt)).scalars().one_or_none()
    if released is not None:
        return True

    current = await get_task(db, task_id)
    if current is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task {task_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_id, "worker": str(worker)},
        )
    raise PolsiaError(
        E_TASK_CLAIMED,
        f"task {task_id} is not claimed by {worker!r} (claimed_by={current.claimed_by!r})",
        how_to_fix=ERROR_HINTS[E_TASK_CLAIMED],
        context={"task_id": task_id, "worker": str(worker), "claimed_by": current.claimed_by},
    )


async def requeue_stale_claims(
    db: AsyncSession,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> int:
    """Recover tasks whose worker died.  ONE statement, returns rows recovered.

    A task is stale when its last sign of life (`heartbeat_at`, else `claimed_at`,
    else `created_at`) is older than the lease.  A stale row that has already used
    its attempts is `failed` (with the reason recorded) rather than re-queued
    forever; everything else goes back to `pending` for the next worker.
    """
    lease = _resolve_lease(lease_seconds)
    cutoff = as_bound_instant((now or utcnow()) - timedelta(seconds=lease), db)
    max_attempts = int(settings.task_max_attempts)
    attempts = func.coalesce(Task.attempts, 0)
    stmt = (
        update(Task)
        .where(
            Task.status == "in_progress",
            func.coalesce(Task.heartbeat_at, Task.claimed_at, Task.created_at) < cutoff,
        )
        .values(
            status=case((attempts >= max_attempts, "failed"), else_="pending"),
            error_message=case(
                (
                    attempts >= max_attempts,
                    f"lease expired after {max_attempts} attempts (worker died?)",
                ),
                else_=Task.error_message,
            ),
            claimed_by=None,
            claimed_at=None,
            heartbeat_at=None,
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    return len((await db.execute(stmt)).scalars().all())


# ── reads the dashboard/agents screens depend on ─────────────────────────────


async def get_tasks_today_summary(
    db: AsyncSession,
    day: date | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """Counts for the company-local day — ONE grouped aggregate.

    The window comes from `day_window(db, day, tz_name)` (company timezone,
    dialect-correct half-open bounds); the original built a naive local midnight
    with no upper bound, which on PostgreSQL selected 0 of 303 rows for the same
    day when the process timezone differed from the session timezone.
    """
    local_day = day or today_in(tz_name)
    start, end = day_window(db, day=local_day, tz_name=tz_name)
    rollup = agent_rollup_expr(dialect_name(db), Task.agent_type)

    rows = await fetch_rows(
        db,
        select(Task.status, func.count().label("n"), rollup.label("agents"))
        .where(Task.created_at >= start, Task.created_at < end)
        .group_by(Task.status),
        what="tasks-today summary",
    )

    counts: dict[str, int] = {status: int(n) for status, n, _agents in rows}
    agents: set[str] = set()
    for _status, _n, group in rows:
        if group:
            agents.update(part for part in str(group).split(",") if part)

    return {
        "total": sum(counts.values()),
        "completed": counts.get("completed", 0),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "in_progress": counts.get("in_progress", 0),
        "cancelled": counts.get("cancelled", 0),
        "active_agents": sorted(agents),
        "day": local_day.isoformat(),
    }


async def get_agent_statuses(db: AsyncSession, tz_name: str | None = None) -> list[dict[str, Any]]:
    """One row per agent type — exactly nine, even on an empty database, in ONE query.

    The agent list is a CTE of constants (NOT a GROUP BY over `agent_runs`, which
    returns nothing on an empty database and is why the original needed 27 round
    trips: three queries per agent).  Each per-agent fact is a correlated
    subquery — the form lane 06 measured as the winner on an indexed schema
    (84.8 ms vs 692.5 ms for the grouped-aggregate variant, which materialises
    `tasks` and sorts it).

    `build_agent_values_cte` uses `SELECT <literal> UNION ALL ...`: SQLAlchemy's
    `values()` construct renders `FROM (VALUES ...) AS agents (agent_type)`, and
    SQLite rejects the column-list alias with a syntax error
    (`probe_agent_variants.py`), so the CTE is built explicitly.
    """
    local_day = today_in(tz_name)
    start, end = day_window(db, day=local_day, tz_name=tz_name)

    agents = build_agent_values_cte()

    last_run_at = (
        select(AgentRun.started_at)
        .where(AgentRun.agent_type == agents.c.agent_type)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
        .limit(1)
        .correlate(agents)
        .scalar_subquery()
    )
    last_run_status = (
        select(AgentRun.status)
        .where(AgentRun.agent_type == agents.c.agent_type)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
        .limit(1)
        .correlate(agents)
        .scalar_subquery()
    )
    tasks_today = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.agent_type == agents.c.agent_type,
            Task.created_at >= start,
            Task.created_at < end,
        )
        .correlate(agents)
        .scalar_subquery()
    )
    tasks_total = (
        select(func.count())
        .select_from(Task)
        .where(Task.agent_type == agents.c.agent_type)
        .correlate(agents)
        .scalar_subquery()
    )

    stmt = (
        select(
            agents.c.agent_type,
            last_run_at.label("last_run_at"),
            last_run_status.label("last_run_status"),
            tasks_today.label("tasks_today"),
            tasks_total.label("tasks_total"),
        )
        .order_by(agents.c.agent_type)
    )
    rows = await fetch_rows(db, stmt, what="agent status rollup")
    return [
        {
            "agent_type": agent_type,
            "last_run_at": last_run,
            "last_run_status": status,
            "tasks_today": int(today_count or 0),
            "tasks_total": int(total_count or 0),
        }
        for agent_type, last_run, status, today_count, total_count in rows
    ]


def build_agent_values_cte():
    """The nine agent types as a CTE of constants (never empty, never a join).

    `SELECT <literal> UNION ALL SELECT <literal> ...` — verified to run on SQLite
    and to compile for PostgreSQL (`probe_agent_variants.py`), and it is what makes
    `/agents/status` return nine rows on a completely empty database.
    """
    first, *rest = VALID_AGENT_TYPES
    anchor = select(literal(first).label("agent_type"))
    return anchor.union_all(*[select(literal(name).label("agent_type")) for name in rest]).cte(
        "agents"
    )


async def create_agent_run(
    db: AsyncSession,
    agent_type: str,
    task_id: int | None = None,
    input_context: dict[str, Any] | None = None,
    run_type: str = "task",
    *,
    cli_session_id: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    cost_usd: Decimal | float | None = None,
    usage_json: dict[str, Any] | None = None,
) -> AgentRun:
    """Start an agent run and flush it (so `.id` is usable by the caller).

    The original schema had `cost_usd`/`tokens_used` and populated neither, which
    made unit economics uncomputable; every field the CLI envelope carries
    (session id, token split, cost, raw usage) is persisted here.
    """
    run = AgentRun(
        agent_type=agent_type,
        task_id=task_id,
        run_type=run_type,
        status="running",
        input_context=input_context,
        cli_session_id=cli_session_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=_as_column_decimal(cost_usd),
        usage_json=usage_json,
    )
    db.add(run)
    await db.flush()
    return run


async def finish_agent_run(
    db: AsyncSession,
    run_id: int,
    status: str,
    output: dict[str, Any] | None = None,
    duration_secs: float | None = None,
    error_message: str | None = None,
    *,
    cli_session_id: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    cost_usd: Decimal | float | None = None,
    usage_json: dict[str, Any] | None = None,
) -> AgentRun | None:
    """Close an agent run.  Returns None when the run id is unknown.

    Each field is written only when supplied, so a late `finish` call cannot erase
    accounting the caller already recorded.
    """
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.status = status
    if output is not None:
        run.output = output
    if duration_secs is not None:
        run.duration_secs = duration_secs
    if error_message is not None:
        run.raw_log = error_message
    if cli_session_id is not None:
        run.cli_session_id = cli_session_id
    if tokens_input is not None:
        run.tokens_input = tokens_input
    if tokens_output is not None:
        run.tokens_output = tokens_output
    if cost_usd is not None:
        run.cost_usd = _as_column_decimal(cost_usd)
    if usage_json is not None:
        run.usage_json = usage_json
    run.ended_at = utcnow()
    await db.flush()
    return run


def _as_column_decimal(value: Decimal | float | None) -> Decimal | None:
    """`cost_usd` is a `Numeric(14, 4)` column (alembic `0003_money_exact`) — the ONE boundary.

    Money is handled as `Decimal` inside the service layer; this is the single place it
    crosses to the column's exact type.  A `Decimal` passes through untouched; anything
    else goes `Decimal(str(value))` — never binary-float arithmetic.  The name is
    greppable: every write of `agent_runs.cost_usd` in the tree routes through here
    (A-T17 single-site rule).
    """
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def get_agent_run(db: AsyncSession, run_id: int) -> AgentRun | None:
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    return result.scalar_one_or_none()


__all__ = [
    "VALID_AGENT_TYPES",
    "VALID_TASK_STATUSES",
    "claim_task",
    "create_agent_run",
    "create_task",
    "finish_agent_run",
    "get_agent_run",
    "get_agent_statuses",
    "get_task",
    "get_tasks_today_summary",
    "heartbeat_task",
    "is_valid_agent_type",
    "list_tasks",
    "release_claim",
    "requeue_stale_claims",
    "update_task_status",
]
