"""tasks + agent_runs."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer, server_default="3", default=3)
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="pending", default="pending"
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
    cost_usd: Mapped[float | None] = mapped_column(Float)
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

    __table_args__ = (
        Index("ix_agent_runs_task_id", "task_id"),
        Index("ix_agent_runs_agent_type", "agent_type"),
        Index("ix_agent_runs_started_at", "started_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AgentRun id={self.id} {self.agent_type} status={self.status}>"
