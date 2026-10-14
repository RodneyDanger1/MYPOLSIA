from __future__ import annotations

from datetime import datetime
from typing import Literal

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

    # ── wave U2 truth fields (ADDITIVE, all nullable, default None) ───────────────────────────
    # The tasks page needed two facts the API could not answer: when a task finished, and what it
    # cost.  Both are measured from rows that already exist (`agent_runs` carries the ledger; the
    # schema has no `completed_at` column), so nothing here changes the DDL or the wire shape of
    # the existing fields — an old client deserializing these models sees exactly its old keys plus
    # four nullables.
    #
    #   * `completed_at` — set ONLY when `status == "completed"`, then it is the completion
    #     timestamp `task_service.task_completed_at` derives (`updated_at`, the column the repo's own
    #     completion writer stamps — `update_task_status`).  A failed task did not complete: its
    #     `completed_at` stays null rather than wearing a synonym for `updated_at`.
    #   * `cost_usd` — the SUM of this task's `agent_runs.cost_usd` (NUMERIC(14,4) at rest; served as
    #     a float like every other `*_usd` response field in this repo).  NULL when the task has no
    #     runs — unmeasured is never zero.  A measured zero (runs exist, cost recorded as 0) sums to
    #     0.0 and IS served.
    #   * `tokens_in` / `tokens_out` — the SUMs of `agent_runs.tokens_input` / `tokens_output`, same
    #     NULL discipline (no runs, or no value ever recorded, => null).
    #
    # Filled at EVERY construction site (all four live in `app/api/v1/tasks.py` — recon:
    # `grep -rn "TaskResponse" backend/app`): a field defined here but left unpopulated at a site
    # with runs behind it is the defined-but-never-called defect class this wave exists to kill.
    completed_at: datetime | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


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


#: The frozen notification vocabulary (wave U2).  One Literal, one place: the route maps task
#: statuses onto these strings and pydantic refuses anything else at response-build time — a new
#: kind is a deliberate edit here, never a silent extra value on the wire.
NotificationKind = Literal["task_completed", "task_failed", "awaiting_approval"]


class NotificationItem(BaseModel):
    """One notification, DERIVED from a task row — there is no notifications table.

    `id` is stable for as long as the row is: `task:<task_id>:<kind>` is a pure function of the
    task, so two requests inside the window return the same identifier (the bell must be able to
    key its items).  `created_at` is the task's completion/park timestamp (see
    `services.task_service.task_terminal_at_expr`) serialized as ISO-8601.
    """

    id: str
    kind: NotificationKind
    title: str
    task_id: int
    created_at: datetime


class NotificationsResponse(BaseModel):
    """`count` is the items length — served explicitly so a client never has to trust its own
    parse of the array to know how many the server produced."""

    items: list[NotificationItem]
    count: int
