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
    """The task status-change request body — now WIRED to `PATCH /api/v1/tasks/{task_id}/status`.

    fix-5 (2026-10-13) labelled this model zero-consumer: `update_task_status` was called by the
    worker and agent paths, never over HTTP (the L1-09/L2-02 fix asserts the value set at the
    SERVICE).  D20 ruled the model is the contract: the route now takes it as its typed body, so
    the write surface is documented and validated at the boundary instead of being an unwritten
    promise.  The vocabulary is NOT re-declared here — the service's `VALID_TASK_STATUSES`
    (`task_service.py`) stays the single authority, and a bad value is refused by name with the
    accepted set in the context.

    `result_summary`/`error_message` are assigned only when supplied (the service's rule), so a
    body that omits them cannot wipe a previously recorded message.
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
