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
    id: int
    title: str
    description: str | None = None
    agent_type: str
    priority: int
    status: str
    source: str | None = None
    scheduled_date: datetime | None = None
    result_summary: str | None = None
    error_message: str | None = None


class TaskStatusUpdate(BaseModel):
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
    tasks_today_total: int = 0
    tasks_today_completed: int = 0
    tasks_today_pending: int = 0
    tasks_today_failed: int = 0
    active_agents: list[str] = []
