from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class ActivityEventResponse(ORMModel):
    id: int
    agent_type: str
    action: str
    summary: str
    detail: dict[str, Any] | None = None
    level: str
    created_at: datetime | None = None


class DailyReportResponse(ORMModel):
    id: int
    report_date: date
    morning_plan: str | None = None
    evening_summary: str | None = None
    tasks_planned: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    metrics_snapshot: dict[str, Any] | None = None
    insights: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DashboardSummaryResponse(BaseModel):
    tasks_today_total: int = 0
    tasks_today_completed: int = 0
    tasks_today_pending: int = 0
    tasks_today_failed: int = 0
    active_agents: list[str] = []
    kpis: dict[str, Any] = {}
    last_report_date: date | None = None
