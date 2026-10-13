"""tasks + agent_runs."""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class TaskStatus(enum.StrEnum):
    """The `tasks.status` value set, as a TYPE (D2 / fix-5 REQUESTS §6).

    Escalated from `String(50)` + a hand-maintained tuple so the value set exists in ONE place:
    the column's `Enum(native_enum=False, create_constraint=True)` renders
    `VARCHAR(50) ... CONSTRAINT ck_tasks_status CHECK (...)` on every backend (SQLAlchemy 2.0:
    "When False, uses VARCHAR + check constraint for all backends"), and the `CHECK` value list and
    `task_service.VALID_TASK_STATUSES` are both DERIVED from this class — they cannot drift.

    `StrEnum` (not `str, enum.Enum`) on purpose: the shipped code compares statuses to plain strings
    (`"pending"`, `"in_progress"`), formats them into activity summaries and JSON, and `StrEnum.__str__`
    is the value, so every one of those keeps its exact behaviour.  Values are what gets persisted
    (`values_callable` below): a bare class-typed Enum persists the member NAMES — measured, the CHECK
    would read `('PENDING', ...)` — which is not the shipped schema.

    `rejected` = refused before execution (budget/policy choke point; nothing charged), the live
    product's Rejected state (D2).  `awaiting_approval` = the approvals channel parked the run for the
    owner's green light (D1/T02).
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"


def task_status_values(enum_cls: type[enum.Enum] = TaskStatus) -> list[str]:
    """`Enum.values_callable`: persist the member VALUES, in definition order.

    Order is load-bearing twice: the CHECK constraint's value list (a stable, readable DDL) and
    `task_service.VALID_TASK_STATUSES` (the tuple the writers assert against).
    """
    return [str(member.value) for member in enum_cls]



class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer, server_default="3", default=3)
    status: Mapped[TaskStatus | None] = mapped_column(
        Enum(
            TaskStatus,
            native_enum=False,          # VARCHAR + CHECK on every backend (no PG ENUM type)
            create_constraint=True,     # the named CHECK is the at-rest enforcement
            name="ck_tasks_status",     # the SHIPPED constraint name (alembic 0002:89, re-created by 0006)
            length=50,                  # keeps the DDL VARCHAR(50), the shipped column width
            values_callable=task_status_values,  # persist values, not member names (see above)
        ),
        server_default="pending",
        default="pending",
    )
    source: Mapped[str | None] = mapped_column(
        String(100), server_default="orchestrator", default="orchestrator"
    )
    scheduled_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # NOTE: the DB column is named "metadata" (migration 0001).  The Python attribute
    # CANNOT be called `metadata` -- see SPEC-MODELS.md: that name is reserved by
    # Declarative for the MetaData object (SQLAlchemy emits
    # "SAWarning: Attribute name 'metadata' should be left reserved for the MetaData
    # instance" and `Task.metadata` then becomes an InstrumentedAttribute, breaking
    # `Task.metadata.create_all()` with AttributeError).
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", json_type())
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # --- queue discipline (added by migration 0002) ---------------------------
    # A task must be CLAIMED atomically before it runs: `claimed_by` names the worker,
    # `claimed_at`/`heartbeat_at` bound the lease so a crashed worker's task becomes
    # reclaimable instead of being lost forever. `attempts` drives the retry ladder and
    # `dedupe_key` makes a repeating sweep idempotent (the same day's plan cannot be
    # created twice). A plain UNIQUE index is portable: both PostgreSQL and SQLite
    # permit many NULLs in a unique index, so only rows that opt in are constrained.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_tasks_pending_queue", "status", "priority", "scheduled_date"),
        Index("ix_tasks_created_at", "created_at"),
        Index("ix_tasks_dedupe_key", "dedupe_key", unique=True),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task id={self.id} {self.agent_type} status={self.status}>"


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"))
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    run_type: Mapped[str | None] = mapped_column(
        String(100), server_default="task", default="task"
    )
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="running", default="running"
    )
    input_context: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    output: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    raw_log: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    # money is exact at rest: NUMERIC(14, 4), written only through
    # task_service._as_column_decimal (A-T17 / alembic 0003).  `duration_secs`
    # stays FLOAT — it is a duration, not money.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    duration_secs: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- cost/session accounting (added by migration 0002) --------------------
    # The original schema had cost_usd/tokens_used and NOTHING ever populated them
    # (measured: nullable Float with no token breakdown -> unit economics were
    # uncomputable). `cli_session_id` enables `--resume` continuity and the raw usage
    # envelope is kept so costs can be re-derived without re-running an agent.
    cli_session_id: Mapped[str | None] = mapped_column(String(120))
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    # lane 06 §3.6 (migration 0002): the execution number of a retried task, so runs of
    # attempt 1 and attempt 3 are distinguishable.  Written by
    # `task_service.create_agent_run(..., attempt=<tasks.attempts>)` — the claim
    # (`claim_task`) is the one incrementer of that counter, so the number is stable once
    # the task is claimed.  Left NULL for runs with no task (cycle/manual), which the
    # partial unique below explicitly permits.
    attempt: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_agent_runs_task_id", "task_id"),
        Index("ix_agent_runs_agent_type", "agent_type"),
        Index("ix_agent_runs_started_at", "started_at"),
        # migration 0002:159-161, mapped here so `create_all` describes the same schema the
        # chain ships (the frozen pin hashes `create_all` DDL; without this the partial
        # unique is migration-only and the retry ledger's key is inert).  Partial in both
        # dialects so rows without a task_id never collide.
        Index(
            "ux_agent_runs_task_attempt",
            "task_id",
            "attempt",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
            sqlite_where=text("task_id IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AgentRun id={self.id} {self.agent_type} status={self.status}>"
