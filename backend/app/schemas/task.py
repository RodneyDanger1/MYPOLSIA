from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import TimestampedORM


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    agent_type: str
    description: str | None = None
    priority: int = 3
    source: str = "dashboard"
    scheduled_date: datetime | None = None


class TaskResponse(TimestampedORM):
    """`priority`/`status` are NULLABLE in the frozen schema (`models/task.py`; `0001`'s
    `CREATE TABLE tasks` carries `server_default`s but no `NOT NULL`, and on PostgreSQL
    `CHECK(NULL)` passes — `ck_tasks_status` cannot police a NULL).  Declaring them required turned
    ONE schema-legal NULL row (a raw-SQL/ops writer is an accepted path, the same premise R26/R31
    accepted for ads) into a bare, codeless 500 for the WHOLE list (L1-11).  `None` means "not
    recorded" and is served as null — never a fabricated `3`/"pending"; the tasks page renders the
    dash / gray badge for it.
    """

    id: int
    title: str
    description: str | None = None
    agent_type: str
    priority: int | None = None
    status: str | None = None
    source: str | None = None
    scheduled_date: datetime | None = None
    result_summary: str | None = None
    error_message: str | None = None


class TaskStatusUpdate(BaseModel):
    """The would-be task status-change request body.

    ZERO CONSUMERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — no status route exists:
    `update_task_status` is called by the worker and agent paths, never over HTTP (the
    L1-09/L2-02 fix asserts the value set at the SERVICE — it deliberately adds no route).
    Adding a route that accepts this body vs deleting it is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §2).
    """

    status: str
    result_summary: str | None = None
    error_message: str | None = None


class AgentStatusResponse(BaseModel):
    agent_type: str
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    tasks_today: int = 0
    tasks_total: int = 0


class AgentTriggerResponse(BaseModel):
    status: str = "queued"
    task_id: int
    message: str


class AgentTriggerRequest(BaseModel):
    task_title: str | None = None
    description: str | None = None
    priority: int = 3


class TasksTodaySummary(BaseModel):
    """The would-be "tasks today" summary response shape.

    ZERO CONSUMERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the service function it
    mirrors (`task_service.get_tasks_today_summary`) returns a plain dict, declares this model
    nowhere and is itself a labelled zero-caller (L2-04); the shipped dashboard builds its own
    statement.  Wiring the pair vs deleting them is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §2).
    """

    tasks_today_total: int = 0
    tasks_today_completed: int = 0
    tasks_today_pending: int = 0
    tasks_today_failed: int = 0
    active_agents: list[str] = []
