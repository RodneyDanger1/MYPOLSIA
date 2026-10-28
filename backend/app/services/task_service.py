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

Also here: `get_agent_statuses` is ONE statement that returns exactly nine rows even on an
empty database (the correlated-subquery form lane 06 measured at 84.8 ms vs 692.5 ms for the
grouped-aggregate variant).  T13/D20 removed the two labelled zero-caller reads
(`get_tasks_today_summary` — a duplicate of `GET /dashboard/summary`'s own statement — and
`get_agent_run` — no run-detail surface); the day rollup and the single-run lookup both live
where their consumers are.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import and_, case, func, literal, or_, select, update
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
from app.models.task import AgentRun, Task, TaskStatus
from app.services.query_helpers import fetch_rows, fetch_scalar, fetch_scalars

logger = logging.getLogger(__name__)

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

#: The `tasks.status` value set the writers assert — DERIVED from `TaskStatus` (D2), so this tuple
#: and the column's own `Enum(native_enum=False, create_constraint=True)` CHECK cannot drift: the
#: CHECK's value list comes from the same enum, and migration 0006 (which carries `rejected`) states
#: the same list.  RECONCILED with the SHIPPED `ck_tasks_status` CHECK (alembic 0002:89-90 for the
#: first six; `0006_statuses_and_keys.py` re-creates it with `rejected` appended, order preserved).
VALID_TASK_STATUSES: tuple[str, ...] = tuple(member.value for member in TaskStatus)


#: The ONE `tasks.status` transition matrix (F-17, wave B1 / seat C1).  Every edge below exists
#: because a real writer needs it — the table does not invent edges, it STATES the ones the product
#: already relied on, so an API write can be checked against the same authority the queue verbs
#: implement atomically in SQL (`_claimable_filter`/`claim_task`: pending -> in_progress;
#: `release_claim`: in_progress -> pending).
#:
#: TERMINAL states (`completed`, `cancelled`, `rejected`) have NO outgoing edge.  That is the
#: measured F-17 defect: `PATCH /tasks/{id}/status` used to write any value over any state and
#: answered 200 for `completed -> pending`, silently erasing a completion.  `failed -> pending` is
#: the ONE re-arm the product ships (V12's control: a manually failed row may be queued again);
#: `awaiting_approval -> pending` is `attach_approval_token`'s re-queue after the owner approves.
TASK_TRANSITIONS: dict[TaskStatus, tuple[TaskStatus, ...]] = {
    TaskStatus.PENDING: (
        TaskStatus.IN_PROGRESS,      # claim_task
        TaskStatus.COMPLETED,        # operator completes a queued row (T12/U2 pins)
        TaskStatus.FAILED,           # operator records a failure (V12 redteam control)
        TaskStatus.CANCELLED,        # owner cancels a queued row
        TaskStatus.REJECTED,         # refused BEFORE execution (budget/policy choke point)
    ),
    TaskStatus.IN_PROGRESS: (
        TaskStatus.PENDING,          # release_claim / a retryable failure's requeue
        TaskStatus.COMPLETED,        # the worker's outcome (agent_tasks)
        TaskStatus.FAILED,           # the worker's outcome (agent_tasks)
        TaskStatus.CANCELLED,        # owner cancels a run mid-flight (release_claim's docstring)
        TaskStatus.AWAITING_APPROVAL,  # the worker parks the run for approval (T02)
        TaskStatus.REJECTED,         # the budget gate's refusal (agent_tasks:826)
    ),
    TaskStatus.AWAITING_APPROVAL: (
        TaskStatus.PENDING,          # attach_approval_token re-queues after the owner's approval
        TaskStatus.CANCELLED,        # task_repeat: "approve it … or cancel"
    ),
    TaskStatus.FAILED: (
        TaskStatus.PENDING,          # re-arm for a retry (V12 redteam control)
    ),
    TaskStatus.COMPLETED: (),
    TaskStatus.CANCELLED: (),
    TaskStatus.REJECTED: (),
}

#: For every edge above, the writer that needs it — the matrix's own provenance.  Pinned equal to
#: the edges by the test, so an edge cannot be added without naming who needs it (an invented
#: transition is how a blind setter grows back).
TASK_TRANSITION_WRITERS: dict[tuple[TaskStatus, TaskStatus], str] = {
    (TaskStatus.PENDING, TaskStatus.IN_PROGRESS): "task_service.claim_task",
    (TaskStatus.PENDING, TaskStatus.COMPLETED): "PATCH /tasks/{id}/status (operator completion)",
    (TaskStatus.PENDING, TaskStatus.FAILED): "PATCH /tasks/{id}/status (operator failure)",
    (TaskStatus.PENDING, TaskStatus.CANCELLED): "PATCH /tasks/{id}/status (owner cancel)",
    (TaskStatus.PENDING, TaskStatus.REJECTED): "policy/budget refusal before execution",
    (TaskStatus.IN_PROGRESS, TaskStatus.PENDING): "task_service.release_claim / retryable requeue",
    (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED): "celery_app.tasks.agent_tasks (run outcome)",
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED): "celery_app.tasks.agent_tasks (run outcome)",
    (TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED): "PATCH /tasks/{id}/status (owner cancel)",
    (TaskStatus.IN_PROGRESS, TaskStatus.AWAITING_APPROVAL): "agent_tasks (parked for approval)",
    (TaskStatus.IN_PROGRESS, TaskStatus.REJECTED): "agent_tasks (budget gate, :826)",
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.PENDING): "task_service.attach_approval_token",
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED): "PATCH /tasks/{id}/status (owner cancel)",
    (TaskStatus.FAILED, TaskStatus.PENDING): "PATCH /tasks/{id}/status (re-arm for retry)",
}

#: The activity_log action every accepted transition writes (the audit half of F-17).
TASK_STATUS_CHANGED_ACTION = "task_status_changed"
#: The activity_log `agent_type` such a row carries: this is queue bookkeeping, not one agent's
#: work — the same role `requeue_stale_claims` writes its recovery rows under.
_TASK_STATUS_ACTOR_ROLE = "orchestrator"


def legal_task_status_targets(status: str | TaskStatus) -> tuple[str, ...]:
    """The statuses `status` may move to — the accepted set a refusal quotes.  Empty = terminal.

    An UNKNOWN current status (a NULL/blank row, or a value outside the enum) has no row in the
    matrix and therefore no accepted target: it answers `()` rather than guessing an edge.
    ALPHABETICAL on purpose: the tuple goes onto the wire (`context.accepted`) and into the
    refusal's message, so its order must be deterministic and independent of the matrix's
    declaration order (a client can print it verbatim).
    """
    try:
        targets = TASK_TRANSITIONS[TaskStatus(str(status))]
    except (KeyError, ValueError):
        return ()
    return tuple(sorted(str(target) for target in targets))



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

    An unknown `status` is refused by NAME (`E_VALIDATION`) before any row is built — the
    same value set the writer `update_task_status` asserts (L1-09/L2-02).  `agent_type`
    keeps its historical `ValueError` contract (the tasks route converts it to
    `E_AGENT_UNKNOWN`); `status` deliberately does NOT raise `ValueError`, because that same
    route converts `ValueError` to an agent-type refusal and a typo'd status would wear the
    wrong code.
    """
    if not is_valid_agent_type(agent_type):
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid: {', '.join(VALID_AGENT_TYPES)}"
        )
    _require_valid_task_status(status)

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
    """One task by id, or `None` when it does not exist; a driver failure is a named `E_DB`.

    READ-ONLY-LANE NOTE (R28): this used to be a raw `db.execute`, so a database outage
    answered `GET /api/v1/tasks/{id}` with a bare **500** carrying no code, while every other
    read surface refuses by name through `query_helpers` (measured: R28
    `raw/probe_tasks_db_failure.txt`; the same defect reached `list_tasks` below).  `fetch_scalar`
    is that shared plumbing (`scalar_one_or_none`'s single-column result, with the `E_DB`
    refusal), so both reads now refuse `E_DB`/503 with the fix hint.
    """
    return await fetch_scalar(db, select(Task).where(Task.id == task_id), what=f"task {task_id}")


async def get_runnable_task(db: AsyncSession, task_id: int) -> Task:
    """Load the ONE row `POST /api/v1/tasks/{task_id}/run` may dispatch — runnable now, or refuse.

    V24/L1's guard rail: the run route must never hand a terminal/in-flight row to the broker,
    because the worker's `claim_task` accepts ONLY `pending` rows — a dispatch of anything else
    would produce a retry ladder around a refusal instead of an answer to the caller.

    A2A6-01: "runnable" is the CLAIM's question, not half of it.  The first revision re-derived
    it from `status == 'pending'` alone and omitted the claim's schedule window
    (`_claimable_filter`: `scheduled_date IS NULL OR scheduled_date <= now`), so a pending row
    dated for later answered `202 dispatched`, the worker's own claim refused it, the message
    was consumed and nothing re-dispatched it (`source='dashboard'`, no re-arm marker).  The
    check below is literally `_claimable_filter` — one predicate, one answer, both verbs — so
    the two can never disagree again.

    Three named refusals, all BEFORE anything is mutated (the row is left byte-identical):
      * a missing id is `E_NOT_FOUND` — the same envelope every task read answers;
      * a `pending` row outside its schedule window is `E_TASK_NOT_RUNNABLE` with
        `reason="scheduled_in_future"` and the row's own instant in `context`;
      * any other status (null included) is `E_TASK_NOT_RUNNABLE` with the CURRENT status in
        `context`, so an `already running` click is distinguishable from a `completed` one.

    The status comparison is against `TaskStatus.PENDING` (a `StrEnum`), so a raw string row
    and an ORM-loaded row both compare by VALUE; the message spells the status in the caller's
    vocabulary ("in_progress"), never as a Python repr.
    """
    task = await get_task(db, task_id)
    if task is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task {task_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_id},
        )
    # A2A6-01: ask the CLAIM's question (`_claimable_filter` is the claim's own predicate).
    runnable_id = await db.scalar(
        select(Task.id)
        .where(*_claimable_filter(db, task_id=task_id, agent_type=None))
        .limit(1)
    )
    if runnable_id is not None:
        return task
    current_status = str(task.status or "")
    if current_status == TaskStatus.PENDING:
        scheduled = task.scheduled_date
        raise PolsiaError(
            E_TASK_NOT_RUNNABLE,
            f"task {task_id} is scheduled for "
            f"{scheduled.isoformat() if scheduled else 'an unset date'} and is not runnable yet",
            how_to_fix=(
                "wait until the scheduled time and run it again, or create the task without "
                "`scheduled_date`; nothing was dispatched"
            ),
            context={
                "task_id": task_id,
                "status": current_status,
                "reason": "scheduled_in_future",
                "scheduled_date": scheduled.isoformat() if scheduled else None,
            },
        )
    raise PolsiaError(
        E_TASK_NOT_RUNNABLE,
        f"task {task_id} is {current_status or 'unknown'!r}, not 'pending'; only a queued "
        "task can be run",
        how_to_fix=(
            "inspect the task's status (GET /api/v1/tasks/{task_id}); a pending task can be "
            "run, an in-progress one is already claimed, and a terminal row must be "
            "re-created through POST /api/v1/tasks if the work is still needed"
        ),
        context={
            "task_id": task_id,
            "status": current_status,
            "expected": str(TaskStatus.PENDING),
        },
    )
    return task


async def list_tasks(
    db: AsyncSession,
    status: str | None = None,
    agent_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Task]:
    """Newest first, index-friendly: explicit LIMIT/OFFSET and a deterministic
    `(created_at DESC, id DESC)` order (ties cannot reorder the page).

    An unknown `status` is a NAMED refusal, never an empty list standing in for a typo — the
    rule `support_inbox_service.list_items` implements and whose docstring says `list_tasks`
    carries too (L1-09 made that true): `?status=complete` answering `200 []` is
    indistinguishable from "no completed tasks", and the frontend renders both as the empty
    state.  A driver failure is a named `E_DB` (via `fetch_scalars`), never an empty list and
    never a bare 500 — see the note on `get_task` above (R28).
    """
    if status is not None and status not in VALID_TASK_STATUSES:
        raise PolsiaError(
            E_VALIDATION,
            f"unknown task status {status!r}; valid: {', '.join(VALID_TASK_STATUSES)}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"status": status, "valid": list(VALID_TASK_STATUSES)},
        )
    stmt = select(Task)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if agent_type is not None:
        stmt = stmt.where(Task.agent_type == agent_type)
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset)
    return await fetch_scalars(db, stmt, what="tasks")


def _require_valid_task_status(status: str) -> None:
    """Refuse an unknown `tasks.status` value BY NAME — the value-set authority, in ONE place.

    The writers (`create_task`, `update_task_status`, `transition_task_status`) each called this
    same refusal inline; extracting it keeps the message, the context keys and the code identical
    across all three, so a caller can never get two different spellings of the same typo.
    """
    if status not in VALID_TASK_STATUSES:
        raise PolsiaError(
            E_VALIDATION,
            f"unknown task status {status!r}; valid: {', '.join(VALID_TASK_STATUSES)}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"status": status, "valid": list(VALID_TASK_STATUSES)},
        )


_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
     TaskStatus.CANCELLED.value, TaskStatus.REJECTED.value}
)

# ── F-07: the run's own verdict, beside the lifecycle status ─────────────────────────────────
#: The TOP-LEVEL result key `app/agents/run_verdict.py` (the agent-side F-07 producer) writes with
#: the run's own `AgentEnvelope.status`.  Read by its WIRE name here — the service layer must not
#: import the agent package (the worker/agent import graph), and the key is a wire contract, not an
#: implementation detail.
RUN_PAYLOAD_STATUS_KEY = "payload_status"
#: The `tasks.metadata` key the WORKER stamps at completion from `RUN_PAYLOAD_STATUS_KEY`.  This is
#: the durable side of the verdict: `tasks.status` is the lifecycle (`completed`), this says how the
#: run itself ended (`partial`).  A JSON bag key, so no migration and no change to the frozen
#: 7-value status vocabulary (`ck_tasks_status`, the frontend's own status matrix).
RESULT_STATUS_KEY = "result_status"
#: The verdict record (`{status,label,reason,source}`) copied beside it, bounded.
VERDICT_METADATA_KEY = "verdict"
#: The machine-written label on a partial run's summary (`run_verdict.PARTIAL_SUMMARY_PREFIX`).
#: Read ONLY as the legacy fallback: rows completed before `RESULT_STATUS_KEY` existed still carry
#: it, and it is our own producer's text, never the model's prose.
PARTIAL_SUMMARY_PREFIX = "[partial] "
#: The execution-status vocabulary this service serves.  NOT `tasks.status` values.
PARTIAL_EXECUTION_STATUS = "partial"
REFUSED_EXECUTION_STATUS = "refused"
NEEDS_ATTENTION_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {PARTIAL_EXECUTION_STATUS, REFUSED_EXECUTION_STATUS}
)


def task_execution_state(task: Task) -> tuple[str | None, bool]:
    """The run's OWN verdict for this row + whether it needs the owner's attention (F-07).

    Live defect: tasks 311/312/314 carried `status: completed`, a `[partial] ` summary and a
    `payload_status: "partial"` in the stored run, while every badge, ledger row and dashboard card
    read COMPLETED.  The lifecycle status is TRUE (the run ended and its work is persisted) — what
    was false was presenting that outcome as a clean completion.  So the persisted status keeps its
    frozen meaning and the verdict is served BESIDE it:

      * `metadata.result_status` — written by the worker at completion from the run's own
        `payload_status` (`partial` / `refused`); the durable, structured truth;
      * the legacy `[partial] ` summary prefix — for rows completed before that marker existed
        (the live 311/312/314 rows), our own producer's label, read as a fallback only;
      * nothing else: prose is never scanned for a verdict (a keyword rule would relabel healthy
        runs and invent a status, the same reason `run_verdict` has no prose scan).

    Returns `(execution_status, needs_attention)`; `(None, False)` means no non-ok verdict was
    recorded — an honest clean completion, or a row with no run at all.
    """
    bag = task.metadata_json if isinstance(task.metadata_json, dict) else {}
    recorded = bag.get(RESULT_STATUS_KEY)
    if isinstance(recorded, str) and recorded in NEEDS_ATTENTION_EXECUTION_STATUSES:
        return recorded, True
    summary = task.result_summary
    if isinstance(summary, str) and summary.startswith(PARTIAL_SUMMARY_PREFIX):
        return PARTIAL_EXECUTION_STATUS, True
    return None, False


def _clear_parked_approval(task: Task, *, status: str, actor: str) -> bool:
    """Drop a parked approval from the row's bag in the WRITER's own transaction (R3/B2-C4-R3).

    A `rejected`/`cancelled`/`completed`/`failed` write used to leave `metadata.approval_required`
    exactly as the worker parked it, so the served `approval_required` flag (derived from that
    list) still said "pending" for a row that had been DENIED — the owner's Approve control offered
    a token for an approval the row no longer had.  The list is emptied (the flag's one source),
    the fingerprints that were dropped are kept in `metadata.approval_cleared` with who ended them
    and when, and the change lands in the same flush as the status write.

    Returns True when there was a parked approval to clear.
    """
    bag = task.metadata_json
    if not isinstance(bag, dict):
        return False
    parked = bag.get(PARKED_APPROVAL_KEY)
    if not parked:
        return False
    merged = dict(bag)
    merged[PARKED_APPROVAL_KEY] = []
    # The parked set is a list of RECORD dicts (the wire shape) in the current producer, and a
    # list of fingerprint strings in rows parked by older builds.  The audit wants the
    # FINGERPRINTS either way — `str(record_dict)` was storing a Python dict repr, which is not an
    # identifier an operator can clear or a test can match (live-proven on the F2 Deny probe).
    fingerprints: list[str] = []
    if isinstance(parked, list):
        for item in parked:
            if isinstance(item, dict):
                fingerprint = str(item.get("fingerprint") or "").strip()
            else:
                fingerprint = str(item).strip()
            if fingerprint:
                fingerprints.append(fingerprint)
    merged[APPROVAL_CLEARED_KEY] = {
        "status": status,
        "actor": actor,
        "at": utcnow().isoformat(),
        "fingerprints": fingerprints,
    }
    # OR-02: the token dies WITH the parked set it was minted for.  `attach_approval_token` writes
    # `metadata.approval_token`, and a terminal write used to empty `approval_required` while
    # leaving that bearer credential on the row — so a later re-arm of the same task handed the
    # worker a token that no longer names anything parked (the resume path reads it, the parked
    # set is gone, and the agent re-runs under a stale approval).  Dropping it here keeps the
    # rule in ONE place: "no parked set ⇒ no token".  `approval_cleared` records the fingerprints
    # that ended, so the audit keeps what the token was for.
    merged.pop(APPROVAL_TOKEN_METADATA_KEY, None)
    task.metadata_json = merged
    return True


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

    An unknown `status` is refused by NAME (`E_VALIDATION`) BEFORE anything is mutated —
    0002's own doctrine is that the value sets are "asserted by the code paths that write
    them", and `ck_tasks_status` can only police that on PostgreSQL: on SQLite (every unit
    test) a bogus value used to be stored, and on PG it answered a bare
    `IntegrityError`/codeless 500 instead of a named caller error (L2-02).

    This is the WORKER's and the services' writer: it asserts the value set and writes, with no
    transition check (the worker's outcome write happens after its own claim, and the shipped
    service contract — pinned by `tests/unit/celery/test_service_contract.py` — is exactly these
    five parameters).  A caller-facing status write goes through `transition_task_status`, which
    adds the `TASK_TRANSITIONS` check and the activity row on top of this same write.
    """
    _require_valid_task_status(status)
    task = await get_task(db, task_id)
    if task is None:
        return None
    # The value was validated against the same enum the column is built from, so this conversion
    # cannot raise; it is what keeps the attribute typed (`Mapped[TaskStatus | None]`) while the
    # service's public signature stays `status: str` — the F2 signature pin
    # (tests/unit/celery/test_service_contract.py:172) is deliberately untouched.
    task.status = TaskStatus(status)
    if status in _TERMINAL_TASK_STATUSES:
        # R3: a terminal write ENDS any parked approval — the flag must stop saying "pending".
        _clear_parked_approval(task, status=status, actor="update_task_status")
    if result_summary is not None:
        task.result_summary = result_summary
    if error_message is not None:
        task.error_message = error_message
    elif status == TaskStatus.COMPLETED:
        # F-23 residue (see `transition_task_status`'s twin): a completion ends the failure
        # history — a `completed` row must not keep an earlier attempt's error text.
        task.error_message = None
    task.updated_at = utcnow()
    await db.flush()
    return task


async def transition_task_status(
    db: AsyncSession,
    task_id: int,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
    *,
    actor: str = "owner",
) -> Task | None:
    """Move ONE task through `TASK_TRANSITIONS` and record it — or refuse by name (F-17).

    The API's status verb must DECIDE, not set: `PATCH /tasks/{id}/status` used to write any value
    over any state, so a `completed` row could be sent back to `pending` with a 200 and no trace
    (measured F-17).  Order of operations, each deliberate:

      1. the target VALUE is asserted first (`_require_valid_task_status`) — an unknown word is
         still the value-set refusal (`E_VALIDATION`, the accepted values in `context.valid`), the
         shipped L1-09/L2-02 contract; a known but ILLEGAL value is the matrix refusal below;
      2. a missing row answers `None` (the route's `E_NOT_FOUND`), writing nothing;
      3. a same-status write and an illegal cell are the SAME answer: `E_TASK_NOT_RUNNABLE` (409 —
         the request conflicts with the row's current state) with `from`, `to` and the ACCEPTED
         set in the context, and nothing is mutated;
      4. an accepted cell is written on the ALREADY-LOADED row (the same four assignments
         `update_task_status` makes — doing them here keeps the check and the write on ONE read,
         so no writer can slip between them) and an activity row is written in the SAME
         transaction, so a rolled-back write cannot leave a feed entry claiming a change that
         never happened (the H-02 rule).

    `result_summary`/`error_message` keep the service's "only when supplied" rule — a transition
    must not wipe a previously recorded message.
    """
    _require_valid_task_status(status)
    target = TaskStatus(status)
    task = await get_task(db, task_id)
    if task is None:
        return None

    current = str(task.status or "")
    accepted = legal_task_status_targets(current)
    if current not in VALID_TASK_STATUSES:
        raise PolsiaError(
            E_TASK_NOT_RUNNABLE,
            f"task {task_id} carries an unrecognised status {current!r}; there is no transition "
            "out of it (nothing was written)",
            how_to_fix=(
                f"repair the row's status first — GET /api/v1/tasks/{task_id} shows it; the value "
                f"set is {', '.join(VALID_TASK_STATUSES)}"
            ),
            context={"task_id": task_id, "from": current, "to": target.value, "accepted": []},
        )
    accepted_text = ", ".join(accepted) if accepted else "none (terminal state)"
    if target.value == current:
        raise PolsiaError(
            E_TASK_NOT_RUNNABLE,
            f"task {task_id} is already {current!r}; a status write must name a DIFFERENT status "
            f"(nothing was written)",
            how_to_fix=(
                "omit the write — the row already carries that status; from "
                f"{current!r} the accepted targets are: {accepted_text}"
            ),
            context={
                "task_id": task_id,
                "from": current,
                "to": target.value,
                "accepted": list(accepted),
            },
        )
    if target not in TASK_TRANSITIONS[TaskStatus(current)]:
        raise PolsiaError(
            E_TASK_NOT_RUNNABLE,
            f"illegal task transition {current!r} -> {target.value!r} for task {task_id}; from "
            f"{current!r} the accepted targets are: {accepted_text} (nothing was written)",
            how_to_fix=(
                f"re-read the task (GET /api/v1/tasks/{task_id}) and choose one of the accepted "
                "targets named here; a terminal row cannot be reopened and its history does not "
                "change retroactively"
            ),
            context={
                "task_id": task_id,
                "from": current,
                "to": target.value,
                "accepted": list(accepted),
            },
        )

    task.status = target
    if target.value in _TERMINAL_TASK_STATUSES:
        # R3: a denial/cancellation is an END to the parked approval too — the served
        # `approval_required` flag reads this list, so it must be cleared in THIS transaction.
        _clear_parked_approval(task, status=target.value, actor=actor)
    if result_summary is not None:
        task.result_summary = result_summary
    if error_message is not None:
        task.error_message = error_message
    elif target is TaskStatus.COMPLETED:
        # F-23 residue: a COMPLETED row carrying an earlier attempt's failure text is a lie the
        # owner reads (live: task 313 completed at 21:23 with `E_AGENT_BAD_OUTPUT … <= 280 chars`
        # still on the row).  A completion ENDS the failure history; the retry path that preserves
        # it writes `pending`, not `completed`, so it is untouched.
        task.error_message = None
    task.updated_at = utcnow()
    await db.flush()

    # Lazy import, for the same reason `requeue_stale_claims` uses one: this module's import graph
    # stays exactly what it was.  `log_activity` shares the CALLER's transaction — the audit row
    # and the status write commit (or roll back) together.
    from app.services.activity_service import log_activity

    await log_activity(
        db,
        _TASK_STATUS_ACTOR_ROLE,
        TASK_STATUS_CHANGED_ACTION,
        f"task #{task_id} status {current} -> {target.value} (accepted, by {actor})",
        detail={"task_id": task_id, "from": current, "to": target.value, "actor": actor},
    )

    # ── F2/OR-01: a terminal status written HERE still settles the plan DAG ──────────────────
    # The worker's completion hook only sees the worker's own terminal write.  This service is
    # the owner/API terminal writer (`PATCH /tasks/{id}/status`), so without this leg an owner
    # who cancelled a plan step (or an operator who completed/failed a queued row) left every
    # dependent `pending` for ever — the same OR-01 stranding through a second writer.
    #   * completed  -> the DAG edge is released in THIS transaction (`dag_released_at` + the
    #                   `rearmed_at` delivery-debt marker).  The service has no broker: the
    #                   shipped stranded-row reconciler owns the hand-off (5-min tick; a re-armed
    #                   row is dispatched once its 30-min window passes), exactly as it does for
    #                   every other service-written `rearmed_at` row.
    #   * failed/cancelled/rejected -> the dependents can never run; they are failed BY NAME here
    #                   (direct and transitive), never left `pending`.
    if target is TaskStatus.COMPLETED:
        await release_ready_dependents(db, completed_id=task_id)
    elif target in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REJECTED):
        await _fail_dependents_of_terminal(
            db,
            upstream_id=task_id,
            upstream_status=target.value,
            upstream_error=error_message,
        )
    return task



#: The `tasks.metadata` key the owner's minted approval token travels under (T02/T02b).  The
#: READER is `celery_app.tasks.agent_tasks.approval_token_from_metadata`, whose own
#: `APPROVAL_TOKEN_KEY` names the same string — the producer writes the key here, the consumer
#: names it there (the same producer/consumer split `metadata.failed_payments` uses:
#: `finance/agent.py`'s `FAILED_PAYMENTS_KEY` reads what `daily_cycle.py` writes literally).
APPROVAL_TOKEN_METADATA_KEY = "approval_token"


async def attach_approval_token(db: AsyncSession, task_id: int, token: str) -> Task:
    """Deliver the owner's approval token onto a PARKED task and re-queue it (T02b, D1).

    The missing second half of the approvals loop (wave-W1 REQUESTS §1, option (b)): the
    firewall's `verify_approval` ALLOW path already consumed `tasks.metadata["approval_token"]`
    (the worker publishes the run's `RunApproval` from exactly that key), but nothing over HTTP
    could WRITE it — `TaskCreate` carries no `metadata`, so the loop's producer had no route.
    `POST /api/v1/approvals` (its optional `task_id`) calls this.

    Guardrails, each BEFORE any mutation — a refusal leaves the row byte-identical:
      * a missing row is `E_NOT_FOUND` (404 at the API boundary);
      * a row NOT `awaiting_approval` is refused by NAME (`E_VALIDATION`, 422, the current status
        in the context): a token is meaningful only for the fingerprint a PARKED run recorded,
        and writing one onto any other row would be a silent metadata overwrite;
      * a non-object `metadata` blob is refused, never replaced — the merge IS the promise.

    The re-queue mirrors EXACTLY how the parking side wrote the row
    (`celery_app.tasks.agent_tasks._execute_agent_task`, the `awaiting_approval` branch): the
    dispatcher's claim (`claim_task`) accepts only `status='pending'` inside the runnable window,
    with `claimed_by`/`claimed_at`/`heartbeat_at` cleared — the same fields the parking run
    cleared when it walked away.  `scheduled_date` is deliberately untouched: the dispatcher's
    own window rule decides when the row becomes claimable.
    """
    if not isinstance(token, str) or not token.strip():
        raise PolsiaError(
            E_VALIDATION,
            f"refusing to attach an empty approval token to task {task_id}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_id},
        )
    task = await get_task(db, task_id)
    if task is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"task {task_id} does not exist; there is nothing to attach the token to",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_id},
        )
    current_status = str(task.status or "")
    if current_status != TaskStatus.AWAITING_APPROVAL:
        raise PolsiaError(
            E_VALIDATION,
            f"task {task_id} is {current_status or 'unknown'!r}, not 'awaiting_approval'; an "
            "approval token can only be delivered to a task the approvals channel parked "
            "(nothing was written)",
            how_to_fix=(
                "mint without task_id if the token is for another task, or inspect this task's "
                "status first (GET /api/v1/tasks/{task_id}); the row was left untouched"
            ),
            context={
                "task_id": task_id,
                "status": current_status,
                "expected": TaskStatus.AWAITING_APPROVAL.value,
            },
        )
    existing = task.metadata_json
    if existing is not None and not isinstance(existing, dict):
        raise PolsiaError(
            E_VALIDATION,
            f"task {task_id} carries a non-object metadata blob ({type(existing).__name__}); "
            "refusing to replace it — merging the token would destroy the existing payload",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_id, "metadata_type": type(existing).__name__},
        )
    merged = dict(existing or {})
    merged[APPROVAL_TOKEN_METADATA_KEY] = token.strip()
    task.metadata_json = merged
    # The re-queue: the same fields the parking side wrote (status + cleared claim), so the
    # dispatcher's claim sees a runnable row whose metadata carries the token.
    task.status = TaskStatus.PENDING
    task.claimed_by = None
    task.claimed_at = None
    task.heartbeat_at = None
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
    takeover_stale: bool = False,
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

    `takeover_stale` (F-22) is the REDELIVERY boundary and only ever applies to an explicit
    `task_id`.  A message that comes back from the broker after its worker died names a row that is
    still `in_progress`; refusing it (`E_TASK_CLAIMED`) and acking the message is how the live
    incident turned a stranded claim into a PERMANENTLY stranded task — the delivery is the only
    copy, and the lease sweep that would have requeued the row is a Beat tick (off by design this
    wave).  With `takeover_stale=True` the claim makes ONE atomic statement that takes the row when
    its last sign of life (`heartbeat_at` else `claimed_at` else `created_at`) is older than the
    lease, and leaves a live claim exactly as it was.  The attempt ladder is the bound: a stale
    claim that has already spent `settings.task_max_attempts` executions is written `failed` with
    the SAME reason `requeue_stale_claims` uses, so a crash-looping row ends by name instead of
    being re-run forever.
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

    # F-22: the message came back.  If the row's claim is EXPIRED the redelivery is the recovery
    # path this function must not refuse; `takeover_stale` makes it atomic (one UPDATE whose WHERE
    # carries the lease) so two redeliveries cannot both take the row.
    if takeover_stale:
        recovered = await _take_over_expired_claim(
            db, task_id, worker=worker, lease=_resolve_lease(lease_seconds)
        )
        if recovered is not None:
            return recovered
        # Re-read: the takeover may have written the row `failed` (the ladder is spent) — the
        # refusal below must describe the row as it is now, not as it was before that write.
        # `populate_existing` because the two UPDATEs above deliberately do not sync the session.
        fresh = (
            await db.execute(
                select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
            )
        ).scalars().one_or_none()
        current = fresh or current

    raise PolsiaError(
        E_TASK_CLAIMED if current.claimed_by else E_TASK_NOT_RUNNABLE,
        f"task {task_id} cannot be claimed: status={str(current.status)!r}, "
        f"claimed_by={current.claimed_by!r}",
        how_to_fix=ERROR_HINTS[E_TASK_CLAIMED],
        context={"task_id": task_id, "status": current.status, "claimed_by": current.claimed_by},
    )


async def _take_over_expired_claim(
    db: AsyncSession,
    task_id: int,
    *,
    worker: str,
    lease: int,
) -> Task | None:
    """F-22: take a row whose claim lease expired, atomically — or end it by name.

    Returns the claimed row, or None when the row is not in a stale-claim state (a live claim, a
    different status).  When the stale claim has already spent the attempt ladder the row goes
    `failed` with `requeue_stale_claims`'s own reason and None is returned, so the caller's claim
    refusal names the true state.  Both writes are single compare-and-set statements: two workers
    racing the same redelivery cannot both win.
    """
    from app.services.activity_service import log_activity

    now = as_bound_instant(utcnow(), db)
    cutoff = as_bound_instant(utcnow() - timedelta(seconds=lease), db)
    max_attempts = int(settings.task_max_attempts)
    stale_predicates = (
        Task.status == TaskStatus.IN_PROGRESS,
        func.coalesce(Task.heartbeat_at, Task.claimed_at, Task.created_at) < cutoff,
    )
    attempts = func.coalesce(Task.attempts, 0)
    takeover = (
        update(Task)
        .where(Task.id == task_id, *stale_predicates, attempts < max_attempts)
        .values(
            status=TaskStatus.IN_PROGRESS,
            claimed_by=str(worker),
            claimed_at=now,
            heartbeat_at=now,
            attempts=attempts + 1,
        )
        .returning(Task)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    claimed = (await db.execute(takeover)).scalars().one_or_none()
    if claimed is not None:
        await log_activity(
            db,
            agent_type=str(claimed.agent_type or "orchestrator"),
            action="task_claim_recovered",
            summary=(
                f"task {task_id}: expired claim on {claimed.claimed_by or 'a dead worker'} taken "
                f"over by {worker} after a redelivery; lease {lease} s, attempt "
                f"{int(claimed.attempts or 0)}"
            ),
            level="warning",
            detail={
                "task_id": task_id,
                "worker": str(worker),
                "lease_seconds": lease,
                "attempts": int(claimed.attempts or 0),
                "reason": "stale_claim_taken_over",
            },
        )
        return claimed

    exhausted = (
        update(Task)
        .where(Task.id == task_id, *stale_predicates, attempts >= max_attempts)
        .values(
            status=TaskStatus.FAILED,
            error_message=f"lease expired after {max_attempts} attempts (worker died?)",
            claimed_by=None,
            claimed_at=None,
            heartbeat_at=None,
        )
        .returning(Task.id, Task.agent_type)
        .execution_options(synchronize_session=False)
    )
    ended = (await db.execute(exhausted)).all()
    for ended_id, agent_type in ended:
        await log_activity(
            db,
            agent_type=str(agent_type or "orchestrator"),
            action="task_claim_recovery_exhausted",
            summary=(
                f"task {ended_id}: its claim expired with {max_attempts} attempts already spent; "
                "the row is terminal instead of being run again"
            ),
            level="error",
            detail={
                "task_id": int(ended_id),
                "lease_seconds": lease,
                "max_attempts": max_attempts,
                "reason": "stale_claim_attempts_exhausted",
            },
        )
    if ended:
        # COMMIT HERE, deliberately: the caller's claim is about to raise `E_TASK_NOT_RUNNABLE`
        # (the worker's skip path), and a rollback on the way out would erase the terminal write
        # that the raise is describing — the crash-loop bound would never land.  This is a
        # self-contained recovery outcome, not part of the caller's work.
        await db.commit()
    return None


async def claimable_task_ids(db: AsyncSession, *, limit: int) -> list[int]:
    """The next `limit` task ids the dispatcher's claim would take, in the claim's OWN order.

    READ-ONLY, by design: this is the selection half of `claim_task` (same `_claimable_filter`,
    same `priority, coalesce(scheduled_date, created_at), id` ordering) exposed for the scheduled
    drain (the night-task tick).  It does NOT move a row: `claim_task` accepts only `pending`
    rows, so a caller that pre-claimed here would make every dispatched run skip with
    `E_TASK_NOT_RUNNABLE` — the state transition belongs to the claim path alone, which is how
    "each row transitions through the existing claim path (no parallel semantics)" is kept true.

    The caller owns the transaction (one read, one commit) and the broker hand-off
    (`_common.enqueue_created`, after the commit).  `limit` is a positive int — `bool` is
    rejected explicitly (it IS an int subclass; `True` must not silently mean 1) and anything
    below 1 is a caller-side value error, not a named refusal.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"claimable_task_ids requires a positive int limit, got {limit!r}")
    stmt = (
        select(Task.id)
        .where(*_claimable_filter(db, task_id=None, agent_type=None))
        .order_by(
            Task.priority.asc(),
            func.coalesce(Task.scheduled_date, Task.created_at).asc(),
            Task.id.asc(),
        )
        .limit(limit)
    )
    return [int(task_id) for task_id in (await db.execute(stmt)).scalars().all()]


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
        f"task {task_id} is {str(current.status)!r}; only an in_progress claim can be heartbeated",
        how_to_fix=ERROR_HINTS[E_TASK_NOT_RUNNABLE],
        context={"task_id": task_id, "worker": str(worker), "status": current.status},
    )


#: The `release_claim(release_kind=...)` vocabulary.  `paused` is the V15 pause gate's release:
#: the gate refuses BEFORE anything ran (no provider call, no ledger row), so that release is NOT
#: an execution and must not consume the retry ladder (E7 F-05).
RELEASE_KIND_PAUSED = "paused"
_RELEASE_KINDS = frozenset({RELEASE_KIND_PAUSED})


async def release_claim(
    db: AsyncSession, task_id: int, worker: str, *, release_kind: str | None = None
) -> bool:
    """Give a claim back (graceful shutdown / re-plan / pause).  ONE statement.

    Returns True when this worker's claim was released back to `pending`; a task
    held by somebody else raises `E_TASK_CLAIMED`, a missing task `E_NOT_FOUND` —
    a `False`-only contract would hide both.

    `attempts` counts executions, and `claim_task` is what increments it — so a claim that is
    released WITHOUT having run must return that increment.  `release_kind="paused"` does exactly
    that (floored at 0): the pause gate claims, sees the owner's STOP, and releases before any
    work, and without the refund three pause/resume cycles burned `task_max_attempts` (3) on work
    that ran zero times — one later lease expiry then wrote `failed`/"attempts exhausted"
    (E7 F-05).  A plain release (no kind) keeps the shipped semantics byte-for-byte: an unknown
    kind is a caller error, refused rather than silently treated as plain.
    """
    if release_kind is not None and release_kind not in _RELEASE_KINDS:
        raise ValueError(
            f"release_claim got unknown release_kind {release_kind!r}; "
            f"valid kinds: {sorted(_RELEASE_KINDS)} or None"
        )
    values: dict[str, Any] = {
        "status": "pending",
        "claimed_by": None,
        "claimed_at": None,
        "heartbeat_at": None,
    }
    if release_kind == RELEASE_KIND_PAUSED:
        attempts = func.coalesce(Task.attempts, 0)
        values["attempts"] = case((attempts > 0, attempts - 1), else_=0)
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            # A2A6-02: the from-state is part of the transition.  `claimed_by` alone is NOT a
            # from-state: `update_task_status` never clears it, so a row the OWNER cancelled
            # mid-run still reads `claimed_by='worker-A'` and this release used to rewrite the
            # human decision back to `pending` (refunding the attempt, and — with the pause
            # gate's marker — dispatching a cancelled task after resume).  A row that moved on
            # is refused below instead of being written over.
            Task.status == "in_progress",
            Task.claimed_by == str(worker),
        )
        .values(**values)
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
        f"task {task_id} is not an in_progress claim of {worker!r} "
        f"(status={str(current.status)!r}, claimed_by={current.claimed_by!r})",
        how_to_fix=ERROR_HINTS[E_TASK_CLAIMED],
        context={
            "task_id": task_id,
            "worker": str(worker),
            "claimed_by": current.claimed_by,
            "status": str(current.status or ""),
            "expected": str(TaskStatus.IN_PROGRESS),
        },
    )


#: How many claims ONE sweep may recover (F-51).  The UPDATE used to be unbounded: a lease
#: misconfiguration rewrote every `in_progress` row in one transaction, and the single activity
#: row it wrote carried every id.  A bounded pass keeps the transaction small and the feed honest;
#: the next tick continues from the oldest remaining id.
STALE_CLAIM_BATCH = 200


def _stale_claim_event(
    task_id: int, *, requeued: bool, lease: int, sweep_size: int
) -> dict[str, Any]:
    """ONE recovered task as ONE activity event (F-51: never a row per sweep).

    The row names exactly its own id and outcome — the shape a task's own timeline can be read
    from.  `sweep_size` records how many rows the same pass recovered, so the batch is still
    measurable without every id riding one `detail` blob.
    """
    if requeued:
        summary = f"1 stalled run(s) requeued: #{task_id}"
        detail = {
            "task_id": task_id,
            "requeued_ids": [task_id],
            "failed_ids": [],
            "outcome": "requeued",
            "rearmed": True,
        }
    else:
        summary = f"1 stalled run(s) sent terminal (attempts exhausted): #{task_id}"
        detail = {
            "task_id": task_id,
            "requeued_ids": [],
            "failed_ids": [task_id],
            "outcome": "failed",
            "rearmed": False,
        }
    detail["lease_seconds"] = lease
    detail["sweep_size"] = sweep_size
    return {
        # The scheduler role, the same `agent_type` the cycle's own bookkeeping rows carry: this
        # is queue maintenance, not one agent's work.
        "agent_type": "orchestrator",
        "action": "stale_claims_requeued",
        "summary": summary,
        "level": "warning",
        "detail": detail,
    }


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

    H-02 (wave U3, backend auditor).  The recovery used to be INVISIBLE: the count went to the
    sweep task's return dict and a `logger.info` (`celery_app/tasks/maintenance.py`), so no owner
    surface could tell that a stalled run had been requeued — B7's "stalled run requeued" line had
    no producer.  The statement now returns the recovered ids AND their outcome, and this function
    writes ONE activity row PER recovered task (F-51: one sweep row carrying every id was
    unbounded with the queue, so nobody could read one task's history from it).  The rows ride
    the CALLER's transaction (the sweep task commits them together with the UPDATE), so a rolled-back
    sweep cannot leave an activity row claiming a recovery that never happened; a sweep that
    recovers nothing writes nothing.  The return value stays the recovered-row COUNT (the sweep
    task's result shape and every shipped pin depend on it).
    """
    lease = _resolve_lease(lease_seconds)
    cutoff = as_bound_instant((now or utcnow()) - timedelta(seconds=lease), db)
    max_attempts = int(settings.task_max_attempts)
    attempts = func.coalesce(Task.attempts, 0)
    stale_predicates = (
        Task.status == "in_progress",
        func.coalesce(Task.heartbeat_at, Task.claimed_at, Task.created_at) < cutoff,
    )
    # F-51: the recovery UPDATE was UNBOUNDED — one statement rewrote every stale claim in the
    # table, so a bad lease setting swept the whole queue in a single transaction and wrote a
    # single activity row carrying every id.  The id set is now bounded by `STALE_CLAIM_BATCH`
    # (oldest id first), so one pass recovers at most that many and the next tick continues.
    stale_ids = (
        select(Task.id)
        .where(*stale_predicates)
        .order_by(Task.id)
        .limit(STALE_CLAIM_BATCH)
    )
    stmt = (
        update(Task)
        .where(*stale_predicates, Task.id.in_(stale_ids))
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
        # RETURNING reads the row AS UPDATED, so the outcome of each recovery travels with its id
        # (probe: probes/probe_returning_status.py in the U3 L4 lane — SQLite + SQLAlchemy type the
        # post-update value back as a `TaskStatus`).
        .returning(Task.id, Task.status)
        .execution_options(synchronize_session=False)
    )
    recovered = (await db.execute(stmt)).all()
    if recovered:
        # The UPDATE's CASE has exactly two branches, so `pending` is the requeue and anything
        # else is the terminal branch — the split is REPORTED, never collapsed into one number.
        requeued_ids = sorted(
            int(task_id) for task_id, status in recovered if str(status) == "pending"
        )
        failed_ids = sorted(
            int(task_id) for task_id, status in recovered if str(status) != "pending"
        )
        # Lazy import: this module's import graph stays exactly what it was, and the batch writer is
        # the documented seam for "one round trip, one shared `created_at`, no Redis publish".
        from app.services.activity_service import log_activities

        # F-15: a requeued row has NO delivery — the message died with the worker.  Stamp the marker
        # `maintenance._reconcile_stranded_pending` already reads, so ALL four sources are
        # re-dispatched within one window instead of only `source='scheduler'` (the other three
        # waited for `night_task`, ~24 h).  FAILED ids are not stamped: they are terminal by the
        # attempts rule, and a marker on them would resurrect work the ladder has given up on.
        # The stamp rides THIS transaction (the caller commits it with the UPDATE), so a
        # rolled-back sweep cannot leave a marker claiming a retry that was never owed.
        if requeued_ids:
            from celery_app.tasks._common import stamp_rearmed  # lazy: worker layer, cycle-free

            await stamp_rearmed(db, requeued_ids, reason="stale_claim_requeued")

        # F-51: ONE ACTIVITY ROW PER RECOVERED TASK.  The sweep used to write a single row whose
        # summary and `detail` carried EVERY id of the pass — unbounded with the queue, and
        # unreadable as one task's history.  Each row now names exactly its own id and outcome;
        # the writer call stays batched (one round trip, one shared `created_at`).
        await log_activities(
            db,
            [
                _stale_claim_event(
                    task_id, requeued=True, lease=lease, sweep_size=len(recovered)
                )
                for task_id in requeued_ids
            ]
            + [
                _stale_claim_event(
                    task_id, requeued=False, lease=lease, sweep_size=len(recovered)
                )
                for task_id in failed_ids
            ],
        )
        if len(recovered) >= STALE_CLAIM_BATCH:
            logger.info(
                "requeue_stale_claims: recovered %s rows, the batch bound; more remain for the "
                "next sweep",
                len(recovered),
            )
    return len(recovered)


# ── F-43(c): the approval reaper (a parked row nobody answered) ──────────────────────────────

#: How long a parked approval may sit before the reaper fails it.  72 h: long enough for a
#: weekend answer, short enough that a forgotten park cannot hold a plan hostage for a week.
#: A module constant, not a new environment variable — no knob without a measured need.
APPROVAL_PARK_TTL_S = 72 * 3600
#: How many parked rows ONE reap pass may end (the same bounded-pass rule as the claim sweep).
APPROVAL_REAP_BATCH = 200


async def reap_stale_approvals(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    ttl_s: int | None = None,
) -> list[int]:
    """Fail `awaiting_approval` rows parked longer than the TTL, one activity row each (F-43).

    F-43's measured gap: nothing ever reaped a parked approval.  A run that asked for approval and
    was never answered stayed `awaiting_approval` FOREVER — invisible to the claim sweep (it only
    looks at `in_progress`), never re-dispatched, and its plan node blocked the rows that depended
    on it.  The owner-facing surfaces called that "waiting on you"; the truth was "dead".

    A reaped row is `failed` with the reason recorded (not `pending`: re-queueing would re-run an
    agent whose question nobody answered), its claim columns cleared, and its parked approval
    cleared through `_clear_parked_approval` — so the served `approval_required` flag stops saying
    "pending" at the same instant the row stops being able to accept a token.  The rows ride the
    CALLER's transaction and are BOUNDED to `APPROVAL_REAP_BATCH` per pass.

    Returns the reaped task ids (oldest first).
    """
    ttl = APPROVAL_PARK_TTL_S if ttl_s is None else int(ttl_s)
    if ttl <= 0:
        raise ValueError(f"reap_stale_approvals requires a positive ttl_s, got {ttl!r}")
    cutoff = as_bound_instant((now or utcnow()) - timedelta(seconds=ttl), db)
    parked_ids = (
        await db.execute(
            select(Task.id)
            .where(
                Task.status == TaskStatus.AWAITING_APPROVAL.value,
                func.coalesce(
                    Task.heartbeat_at, Task.claimed_at, Task.updated_at, Task.created_at
                ) < cutoff,
            )
            .order_by(Task.id)
            .limit(APPROVAL_REAP_BATCH)
        )
    ).scalars().all()

    reaped: list[int] = []
    events: list[dict[str, Any]] = []
    for parked_id in parked_ids:
        task = await get_task(db, int(parked_id))
        if task is None or str(task.status) != TaskStatus.AWAITING_APPROVAL.value:
            continue                      # a concurrent writer answered it — not ours to end
        bag = task.metadata_json if isinstance(task.metadata_json, dict) else {}
        fingerprints = bag.get(PARKED_APPROVAL_KEY)
        task.status = TaskStatus.FAILED
        task.error_message = (
            f"no approval was given within {ttl} s of parking; the run expired and nothing "
            "was sent"
        )
        task.claimed_by = None
        task.claimed_at = None
        task.heartbeat_at = None
        task.updated_at = utcnow()
        _clear_parked_approval(task, status=TaskStatus.FAILED.value, actor="approval_reaper")
        task_id = int(task.id)
        reaped.append(task_id)
        events.append(
            {
                "agent_type": "orchestrator",
                "action": "stale_approvals_reaped",
                "summary": f"task #{task_id} was parked awaiting approval for more than "
                           f"{ttl} s and has been failed",
                "level": "warning",
                "detail": {
                    "task_id": task_id,
                    "ttl_s": ttl,
                    "fingerprints": (
                        [str(item) for item in fingerprints]
                        if isinstance(fingerprints, list)
                        else []
                    ),
                    "status": TaskStatus.FAILED.value,
                },
            }
        )
    if reaped:
        await db.flush()
        # Lazy import, the file's standing rule: one batched write, one row per reaped task.
        from app.services.activity_service import log_activities

        await log_activities(db, events)
    return reaped


# ── F-26: the upstream-result channel (one agent's output, typed and read downstream) ────────

#: How many upstream results ONE dependent row records.  A node with a wider fan-in keeps the
#: first `depends_on_ids` entries (the plan's own order) and the rest are simply not copied — the
#: payload can never grow with the plan.
UPSTREAM_MAX_RESULTS = 5
#: Characters of ONE upstream `result_summary` a dependent row carries (truncation is STATED).
UPSTREAM_RESULT_SUMMARY_CHARS = 1200

#: The metadata keys this channel writes (mirrored in `app.schemas.task`, the wire's own read).
PARKED_APPROVAL_KEY = "approval_required"          # the same key `agent_tasks` parks under
APPROVAL_CLEARED_KEY = "approval_cleared"


def _depends_on_ids(metadata: Any) -> list[int]:
    """The planned edges on a row, as ints (a malformed entry is dropped, never coerced)."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("depends_on_ids")
    if not isinstance(raw, list):
        return []
    return [
        dep for dep in raw
        if isinstance(dep, int) and not isinstance(dep, bool)
    ]


def _upstream_record(
    task_id: Any,
    title: Any,
    status: Any,
    result_summary: Any,
    completed_at: Any,
) -> dict[str, Any]:
    """ONE upstream row as the RECORDED shape: bounded summary, stated truncation.

    The cut keeps the summary at exactly `UPSTREAM_RESULT_SUMMARY_CHARS` characters (the last one
    is an ellipsis), and `truncated` says it happened — a consumer must never read a clipped
    summary as the agent's whole output.
    """
    text: str | None = None
    truncated = False
    if result_summary is not None:
        text = str(result_summary)
        if len(text) > UPSTREAM_RESULT_SUMMARY_CHARS:
            text = text[: UPSTREAM_RESULT_SUMMARY_CHARS - 1] + "…"
            truncated = True
    return {
        "task_id": int(task_id),
        "title": str(title or ""),
        "status": str(status or ""),
        "result_summary": text,
        "truncated": truncated,
        "completed_at": completed_at.isoformat() if completed_at is not None else None,
    }


async def _load_upstream(db: AsyncSession, metadata: Any) -> list[Any]:
    """The COMPLETED upstream rows of `metadata`'s edges, typed and bounded, in plan order.

    Only `completed` rows are copied: a failed upstream produced no result to consume, and
    `release_ready_dependents` only releases a node whose whole set completed — the filter is the
    second half of that same rule, so a concurrent writer cannot smuggle a non-terminal row in.
    """
    from app.schemas.task import UpstreamResult

    ids = _depends_on_ids(metadata)[:UPSTREAM_MAX_RESULTS]
    if not ids:
        return []
    rows = (
        await db.execute(
            select(
                Task.id, Task.title, Task.status, Task.result_summary, Task.updated_at
            )
            .where(Task.id.in_(ids))
            .order_by(Task.id)
        )
    ).all()
    by_id = {int(row[0]): row for row in rows}
    results: list[Any] = []
    for upstream_id in ids:                      # the PLAN's order, not the database's
        row = by_id.get(upstream_id)
        if row is None or str(row[2]) != "completed":
            continue
        results.append(UpstreamResult(**_upstream_record(*row)))
    return results


async def upstream_results_for(db: AsyncSession, task_id: int) -> list[Any]:
    """A task's upstream results, read LIVE from its edges (F-26's agent-facing read).

    This is the reader `tasks.result_summary` never had: given the downstream row, it answers
    exactly what the upstream agents produced, typed as `UpstreamResult`.  The recorded copy on
    the row (`materialize_upstream_results`) is what the wire serves; this is the same truth read
    on demand, for a run that was re-delivered before the copy was refreshed.
    """
    task = await get_task(db, task_id)
    if task is None:
        return []
    return await _load_upstream(db, task.metadata_json)


async def materialize_upstream_results(db: AsyncSession, task: Task) -> list[Any]:
    """Copy the task's completed upstream results onto its OWN metadata (F-26's producer).

    Called by `release_ready_dependents` in the SAME transaction that releases the row, so by the
    time the row is dispatchable it already records what it owes to whom — and the worker forwards
    that bag to the agent as `task_dict["metadata"]`.  Idempotent: a second pass overwrites with
    the same truth and a fresh `recorded_at`.  A row with no completed upstream writes nothing.
    Returns the typed records it recorded (empty when there was nothing to record).
    """
    from app.schemas.task import UPSTREAM_RESULTS_KEY, UPSTREAM_RESULTS_RECORDED_KEY

    results = await _load_upstream(db, task.metadata_json)
    if not results:
        return []
    bag = dict(task.metadata_json or {})
    bag[UPSTREAM_RESULTS_KEY] = [record.model_dump(mode="json") for record in results]
    bag[UPSTREAM_RESULTS_RECORDED_KEY] = utcnow().isoformat()
    task.metadata_json = bag
    await db.flush()
    return results


#: One release pass' page size (the same order as the sibling ticks).
DAG_RELEASE_BATCH = 200
#: How many candidate PAGES one release pass may scan.  A single window was not enough: a backlog
#: of unrelated pending dependent rows ahead of this plan's rows consumed the 200-row limit, so the
#: completed parent's OWN dependent was never examined and stranded `pending` with no marker and no
#: reconciler to see it (F2 finding, pinned by
#: `test_w3_dag_release.py::test_a_completed_parent_releases_a_dependent_beyond_the_global_batch`).
#: The scan is targeted at `completed_id` and paged by id; reaching the page bound is LOGGED.
DAG_RELEASE_PAGES = 50

#: The `tasks.metadata` key that records a DAG release (F-02 / OR-01).  A dependent is released
#: ONCE: this marker is written in the SAME transaction as the release, so a second completion
#: hook (a redelivered parent, another parent of the same node finishing) can never hand the same
#: row to a worker twice.  A released row whose delivery was refused is re-armed with
#: `metadata.rearmed_at` (see `release_ready_dependents`) and retried by the stranded-row
#: reconciler — a DIFFERENT mechanism on purpose: a release is a state change, a re-arm is a
#: delivery debt, and conflating them is how a completion hook double-dispatches.
DAG_RELEASED_KEY = "dag_released_at"


async def release_ready_dependents(db: AsyncSession, *, completed_id: int) -> list[int]:
    """Dispatch the rows whose WHOLE dependency set is `completed` — the DAG's edge release (F-02).

    `daily_cycle._morning_cycle` persists the plan's edges on the row (`metadata.depends_on_ids`,
    titles resolved to ids in the same transaction) and dispatches only the roots; this is the
    other half: when a task finishes, the rows waiting on it become runnable.  Its production
    callers are the worker's completion hook (`celery_app.tasks.agent_tasks`, after the outcome
    transaction commits — that leg dispatches immediately) and the service's own terminal writer
    (`transition_task_status`, below), which runs in the status write's transaction and leaves the
    broker hand-off to the stranded-row reconciler.  OR-01: before those callers existed this
    function had zero production callers and every dependent stranded `pending` forever.

    Bounded and dialect-portable in the house way: a LIKE prefilter on the rendered JSON column
    (the same shape `maintenance._reconcile_stranded_pending` uses), a TARGET filter on this
    completion's id and a PAGED id-ordered scan (`DAG_RELEASE_PAGES`), so a backlog of unrelated
    pending rows can never consume the window and hide this parent's own dependents; the page
    bound is stated and logged.  Every released id is stamped with `stamp_rearmed` BEFORE the
    caller dispatches, so a broker that refuses the hand-off leaves a bounded retry (the
    stranded-row reconciler, which re-arms for a 30-min window) instead of a row only a human can
    rescue.  The caller owns the transaction — commit, THEN hand off — so a dependent can never be
    dispatched to a row that is not yet `completed`.

    IDEMPOTENT (OR-01).  Each released row is also stamped with `metadata.dag_released_at` in this
    same transaction; a second pass skips it.  "The caller's dispatch message was lost" is NOT a
    reason to release again — `metadata.rearmed_at` (stamped by `stamp_rearmed` right here) is the
    delivery-debt marker the reconciler consumes, so the retry path is single-owner.

    NOT this function's business: a dependent of a terminal NON-completed upstream.  That edge can
    never be satisfied, so releasing it is impossible; the worker's completion hook fails those
    rows BY NAME (`agent_tasks._block_dag_dependents`) instead of leaving them `pending` forever.

    Returns the ids this pass released (empty when nothing was waiting on `completed_id`, or when
    everything waiting had already been released by an earlier pass).
    """
    from sqlalchemy import String, cast

    target_id = int(completed_id)
    candidates: list[tuple[int, list[int]]] = []
    cursor = 0
    for _page in range(DAG_RELEASE_PAGES):
        rows = (
            await db.execute(
                select(Task.id, Task.metadata_json)
                .where(
                    Task.status == "pending",
                    cast(Task.metadata_json, String).like("%depends_on_ids%"),
                    Task.id > cursor,
                )
                .order_by(Task.id)
                .limit(DAG_RELEASE_BATCH)
            )
        ).all()
        if not rows:
            break
        cursor = int(rows[-1][0])
        for task_id, metadata in rows:
            if not isinstance(metadata, dict) or metadata.get(DAG_RELEASED_KEY):
                continue
            raw_deps = metadata.get("depends_on_ids")
            if not isinstance(raw_deps, list):
                continue
            dep_ids = [
                dep for dep in raw_deps
                if isinstance(dep, int) and not isinstance(dep, bool)
            ]
            # Only the rows THIS completion can make runnable: a row whose edges do not name
            # `completed_id` cannot have been made ready by it.  Without this target filter a
            # backlog page could be consumed by unrelated rows (the F2 starvation finding).
            if target_id not in dep_ids:
                continue
            candidates.append((int(task_id), dep_ids))
        if len(rows) < DAG_RELEASE_BATCH:
            break
    else:
        logger.warning(
            "dag release: the %s-page scan bound was reached while releasing the dependents of "
            "task %s; a later completion or the stranded-row reconciler must finish the rest",
            DAG_RELEASE_PAGES,
            completed_id,
        )
    if not candidates:
        return []
    needed = {dep for _task_id, dep_ids in candidates for dep in dep_ids}
    done = set(
        (
            await db.execute(
                select(Task.id).where(Task.id.in_(needed), Task.status == "completed")
            )
        )
        .scalars()
        .all()
    )
    ready = [
        task_id for task_id, dep_ids in candidates if all(dep in done for dep in dep_ids)
    ]
    if ready:
        # F-26: the rows about to run RECORD their upstream agents' results HERE, in the same
        # transaction as the release — so agent B's run payload (`task_dict["metadata"]`, which the
        # worker forwards verbatim) carries what agent A produced.  A failure to record is not
        # swallowed: it propagates and the caller's commit rolls the release back with it.
        ready_tasks = (
            await db.execute(select(Task).where(Task.id.in_(ready)))
        ).scalars().all()
        released_at = utcnow().isoformat()
        for ready_task in ready_tasks:
            await materialize_upstream_results(db, ready_task)
            # OR-01: the release marker rides the SAME transaction as the release itself, so a
            # second completion hook sees it (the marker cannot be lost without the release).
            merged = dict(ready_task.metadata_json or {})
            merged[DAG_RELEASED_KEY] = released_at
            merged["dag_released_by"] = int(completed_id)
            ready_task.metadata_json = merged
        await db.flush()

        from celery_app.tasks._common import stamp_rearmed  # lazy: worker layer, cycle-free

        await stamp_rearmed(db, ready, reason=f"dag_parent_{completed_id}")
    return ready


#: The bound on ONE failed-dependent pass, and on the transitive expansion (a plan DAG can be a
#: chain).  Reaching either bound is LOGGED, never silently truncated — the same rule the release
#: pass above follows.
_DEPENDENT_BLOCK_BATCH = 200
_DEPENDENT_BLOCK_PASSES = 32
#: Candidate PAGES per block pass.  One window was not enough: a backlog of unrelated pending
#: dependent rows could occupy the whole 200-row page, so the terminal parent's own dependent was
#: never examined (F2 starvation finding, pinned by the `*_beyond_the_global_batch` tests).  The
#: scan is paged by id; reaching this bound is LOGGED, never silently truncated.
_DEPENDENT_BLOCK_PAGES = 50
#: The `tasks.metadata` key naming why a dependent can never run.  The worker's
#: `agent_tasks._block_dag_dependents` writes the SAME string (one vocabulary, two writers); this
#: module keeps its own private name so the service surface (`__all__`) does not grow.
_DEPENDENCY_BLOCKED_KEY = "dependency_blocked"


async def _fail_dependents_of_terminal(
    db: AsyncSession,
    *,
    upstream_id: int,
    upstream_status: str,
    upstream_error: str | None,
) -> list[int]:
    """Fail — BY NAME — the pending dependents of a terminal NON-completed row (OR-01, service leg).

    The service-side twin of the worker's `agent_tasks._block_dag_dependents`: the worker hook
    covers the worker's own terminal write, this covers `transition_task_status` (the owner/API
    `PATCH /tasks/{id}/status`).  Both write the same marker (`metadata.dependency_blocked`), the
    same `error_message` shape and the same `task_dependency_blocked` activity row.

    Runs in the CALLER's transaction — the status write and the blocked rows commit or roll back
    together, so a failed transaction never leaves a dependent written off for a cancellation that
    did not happen.  Transitive: a blocked row is itself a dead dependency, so the next pass ends
    the rows waiting on IT; each pass is bounded and reaching the bound is logged.

    Returns the ids this call settled (empty when nothing was waiting, or when every candidate was
    already settled by an earlier writer).
    """
    from sqlalchemy import String, cast

    from app.services.activity_service import log_activities

    if upstream_error:
        terminal_cause = f"ended {upstream_status!r} ({str(upstream_error)[:300]})"
    else:
        terminal_cause = f"ended {upstream_status!r}"
    causes: dict[int, str] = {int(upstream_id): terminal_cause}
    blocked: list[int] = []
    for _pass in range(_DEPENDENT_BLOCK_PASSES):
        blocked_now: list[int] = []
        next_causes: dict[int, str] = {}
        events: list[dict[str, Any]] = []
        stamp = utcnow().isoformat()
        cursor = 0
        for _page in range(_DEPENDENT_BLOCK_PAGES):
            rows = (
                await db.execute(
                    select(Task)
                    .where(
                        Task.status == "pending",
                        cast(Task.metadata_json, String).like("%depends_on_ids%"),
                        Task.id > cursor,
                    )
                    .order_by(Task.id)
                    .limit(_DEPENDENT_BLOCK_BATCH)
                )
            ).scalars().all()
            if not rows:
                break
            cursor = int(rows[-1].id)
            for row in rows:
                meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
                if meta.get(_DEPENDENCY_BLOCKED_KEY):
                    continue          # an owner re-arm of a row this writer already settled
                dep_ids = [
                    dep for dep in (meta.get("depends_on_ids") or [])
                    if isinstance(dep, int) and not isinstance(dep, bool)
                ]
                hit = [dep for dep in dep_ids if dep in causes]
                if not hit:
                    continue
                direct = hit[0]
                reason = (
                    f"dependency_blocked: upstream task #{direct} {causes[direct]}; "
                    "this task was never run"
                )
                blocked_id = int(row.id)
                row.status = TaskStatus.FAILED
                row.error_message = reason
                row.claimed_by = None
                row.claimed_at = None
                row.heartbeat_at = None
                row.updated_at = utcnow()
                merged = dict(meta)
                merged[_DEPENDENCY_BLOCKED_KEY] = {
                    "upstream_id": direct,
                    "upstream_status": upstream_status,
                    "at": stamp,
                    "reason": reason,
                }
                row.metadata_json = merged
                blocked_now.append(blocked_id)
                blocked.append(blocked_id)
                next_causes[blocked_id] = f"ended 'failed' (it was blocked by task #{direct})"
                events.append(
                    {
                        "agent_type": str(row.agent_type or _TASK_STATUS_ACTOR_ROLE),
                        "action": "task_dependency_blocked",
                        "summary": reason,
                        "level": "warning",
                        "detail": {
                            "task_id": blocked_id,
                            "upstream_id": direct,
                            "upstream_status": upstream_status,
                            "marker": _DEPENDENCY_BLOCKED_KEY,
                        },
                    }
                )
            if len(rows) < _DEPENDENT_BLOCK_BATCH:
                break
        else:
            logger.warning(
                "dependent block: the %s-page scan bound was reached while settling the "
                "dependents of task %s; a later terminal outcome or pass continues",
                _DEPENDENT_BLOCK_PAGES,
                upstream_id,
            )
        if not blocked_now:
            break
        await db.flush()
        await log_activities(db, events)
        causes.update(next_causes)
    else:
        logger.warning(
            "dependent block: stopped after %s transitive pass(es) while settling the dependents "
            "of task %s; a later terminal outcome ends the rest",
            _DEPENDENT_BLOCK_PASSES,
            upstream_id,
        )
    return blocked


# ── reads (get_agent_statuses is wired to /agents/status)
#
# T13/D20 removal — `get_tasks_today_summary` lived here.  It was a labelled zero-caller
# (fix-5 §4; prod 0, tests 0) AND a duplicate: the shipped `GET /dashboard/summary` builds its
# own `_summary_statement` (app/api/v1/dashboard.py:46-74, the same `agent_rollup_expr` rollup),
# so deleting the service read takes nothing away from the product.  Its would-be wire model
# `TasksTodaySummary` was removed in the same pass for the same reason.  Re-introducing either
# needs a real consumer, not a mention.


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
    attempt: int | None = None,
) -> AgentRun:
    """Start an agent run and flush it (so `.id` is usable by the caller).

    The original schema had `cost_usd`/`tokens_used` and populated neither, which
    made unit economics uncomputable; every field the CLI envelope carries
    (session id, token split, cost, raw usage) is persisted here.

    `attempt` is the retried-task execution number (lane 06 §3.6, migration 0002):
    the caller that HOLDS the claimed task passes `attempt=task.attempts` — the claim
    incremented it, so the number is stable.  It stays `None` when the caller has no
    task, which the partial unique `ux_agent_runs_task_attempt` permits.
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
        attempt=attempt,
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


# ── task truth for the wire (wave U2): completed_at + the runs-ledger sums ────────────────────
#
# The frozen U2 contract adds four nullable TaskResponse fields.  Two facts are involved and each
# has exactly ONE definition here, because both the tasks route (TaskResponse.completed_at, the
# cost/token sums) and the notifications route (the completion/park timestamp the 48 h window and
# the newest-first order run on) read them — a second definition is how the two surfaces would
# start disagreeing about when a task "finished".


def task_terminal_at_expr():
    """The completion/park instant as SQL: `coalesce(tasks.updated_at, tasks.created_at)`.

    `tasks` has no `completed_at` column (no DDL change rides this wave), and the repo's own
    writer stamps the moment: `update_task_status` sets `updated_at = utcnow()` on every status
    transition, and the worker records the outcome through exactly that service call
    (`celery_app.tasks.agent_tasks`).  `created_at` is the fallback for a row some raw/Core writer
    left with a NULL `updated_at` — the same last-sign-of-life chain `requeue_stale_claims` uses
    (heartbeat_at else claimed_at else created_at), so a row is never dropped from the window or
    ordered by a different clock than the one it is filtered on.

    Dialect note: the expression is returned unbound — the caller compares it against a bound
    instant (`app.core.timeutil.as_bound_instant`), which is what keeps SQLite (naive UTC text)
    and PostgreSQL (timestamptz) selecting the same rows.
    """
    return func.coalesce(Task.updated_at, Task.created_at)


def task_completed_at(task: Task) -> datetime | None:
    """The python form of `task_terminal_at_expr` for an already-loaded row, gated on completion.

    Returns the completion timestamp ONLY for a `completed` task; every other status (failed,
    cancelled, still-running, awaiting approval) answers `None` — a failed task did not complete,
    and serving its last-update time under `completed_at` would be a renamed field, not a fact.
    """
    if str(task.status or "") != TaskStatus.COMPLETED:
        return None
    return task.updated_at or task.created_at


class TaskRunTotals(NamedTuple):
    """One task's runs-ledger sums.  `None` means unmeasured — never a fabricated zero.

    `unmeasured_runs` counts the runs that finished without recording a cost (the shape F-20
    documents: a failed run drops its measured usage).  A non-zero count means the sums below
    are a PARTIAL total, which is why `cost_usd` is null in that case: a reader must be able to
    tell "the money is $0.02" from "at least $0.02, and one attempt never reported" (A2A7-02).
    """

    cost_usd: Decimal | None
    tokens_in: int | None
    tokens_out: int | None
    unmeasured_runs: int


async def get_task_run_totals(
    db: AsyncSession, task_ids: Sequence[int]
) -> dict[int, TaskRunTotals]:
    """Per-task sums of the `agent_runs` ledger, in ONE grouped statement for the whole id set.

    The tasks list serves up to 500 rows; a per-row query would make the page O(n) round trips, so
    the route hands its whole id set over and this selects `GROUP BY task_id` once.

    NULL discipline (the frozen contract): a task with NO runs is simply ABSENT from the mapping —
    the caller serves null — and `SUM` over rows that never recorded a value is SQL NULL too, so an
    unmeasured cost is never reported as `0`.  A MEASURED zero (runs exist, the value recorded as
    0) sums to `0`/`Decimal("0.0000")` and IS reported.  A driver failure is the shared `E_DB`
    refusal (`fetch_rows`), never an empty mapping standing in for "could not read".

    A2A7-02: `SUM` also ignores NULLs ROW BY ROW, so the mixed case — one priced run and one that
    finished without reporting — used to serve the priced part as the task's complete cost.  The
    same statement now COUNTS those runs (`unmeasured`), and a non-zero count makes `cost_usd`
    null: a partial total is not a total.  A run still `running` is excluded from the count —
    it has not reported YET, which is not the same fact as a run that never will.
    """
    ids = sorted({int(task_id) for task_id in task_ids})
    if not ids:
        return {}
    unmeasured = func.sum(
        case(
            (
                and_(AgentRun.cost_usd.is_(None), AgentRun.status != "running"),
                1,
            ),
            else_=0,
        )
    ).label("unmeasured")
    stmt = (
        select(
            AgentRun.task_id,
            func.sum(AgentRun.cost_usd).label("cost_usd"),
            func.sum(AgentRun.tokens_input).label("tokens_in"),
            func.sum(AgentRun.tokens_output).label("tokens_out"),
            unmeasured,
        )
        .where(AgentRun.task_id.in_(ids))
        .group_by(AgentRun.task_id)
    )
    rows = await fetch_rows(db, stmt, what="task run totals")
    return {
        int(task_id): TaskRunTotals(
            cost_usd=None if int(partial or 0) else cost,
            tokens_in=int(tokens_in) if tokens_in is not None else None,
            tokens_out=int(tokens_out) if tokens_out is not None else None,
            unmeasured_runs=int(partial or 0),
        )
        for task_id, cost, tokens_in, tokens_out, partial in rows
    }


# T13/D20 removal — `get_agent_run` lived here: a labelled zero-caller (fix-5 §4; prod 0, one
# test) with no run-detail/ops route to serve, while the shipped worker addresses runs by the id
# `create_agent_run` returned and closes them through `finish_agent_run`.  A single-run lookup
# that no surface reads is dead weight; re-adding it needs a consumer first.


__all__ = [
    "APPROVAL_PARK_TTL_S",
    "APPROVAL_REAP_BATCH",
    "APPROVAL_TOKEN_METADATA_KEY",
    "NEEDS_ATTENTION_EXECUTION_STATUSES",
    "PARTIAL_EXECUTION_STATUS",
    "PARTIAL_SUMMARY_PREFIX",
    "REFUSED_EXECUTION_STATUS",
    "RESULT_STATUS_KEY",
    "RUN_PAYLOAD_STATUS_KEY",
    "STALE_CLAIM_BATCH",
    "TASK_STATUS_CHANGED_ACTION",
    "TASK_TRANSITIONS",
    "TASK_TRANSITION_WRITERS",
    "UPSTREAM_MAX_RESULTS",
    "UPSTREAM_RESULT_SUMMARY_CHARS",
    "VALID_AGENT_TYPES",
    "VALID_TASK_STATUSES",
    "TaskRunTotals",
    "attach_approval_token",
    "claim_task",
    "claimable_task_ids",
    "create_agent_run",
    "create_task",
    "finish_agent_run",
    "get_agent_statuses",
    "get_task",
    "get_task_run_totals",
    "heartbeat_task",
    "is_valid_agent_type",
    "legal_task_status_targets",
    "list_tasks",
    "materialize_upstream_results",
    "reap_stale_approvals",
    "release_claim",
    "release_ready_dependents",
    "requeue_stale_claims",
    "task_completed_at",
    "task_execution_state",
    "task_terminal_at_expr",
    "transition_task_status",
    "update_task_status",
    "upstream_results_for",
]
