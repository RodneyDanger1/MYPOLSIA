from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, field_validator

from app.schemas.common import ORMModel, UtcDatetime


class ActivityEventResponse(ORMModel):
    id: int
    agent_type: str
    action: str
    summary: str
    detail: dict[str, Any] | None = None
    level: str = "info"
    created_at: UtcDatetime | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _level_defaults_to_info(cls, value: object) -> object:
        """NULL means the writer did not set a level; it is not an outage (A-T3 decision b).

        The column is DDL-nullable and only raw/Core inserts reach NULL (the ORM fires its
        `default="info"`).  The response vocabulary stays one word, so the boundary
        normalises None -> "info" here — the same rule `events.activity_payload` applies to
        the websocket payload.  Without this the feed's `model_validate` raised on a NULL
        row and the endpoint answered 500 (parent-surgical completion of T3).
        """
        return "info" if value is None else value


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
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    # V9 B2: the cash/tax block is COMPOSED by the daily-report route from live finance reads
    # (report_service.build_cash_block) — it is not a `daily_reports` column, so the ORM row
    # leaves it at this default and the route fills it in.  None = not attached (no report).
    cash_block: dict[str, Any] | None = None


class DashboardSummaryResponse(BaseModel):
    tasks_today_total: int = 0
    tasks_today_completed: int = 0
    tasks_today_pending: int = 0
    tasks_today_failed: int = 0
    active_agents: list[str] = []
    kpis: dict[str, Any] = {}
    last_report_date: date | None = None


class ReportsListResponse(BaseModel):
    """`GET /dashboard/reports` — the documents overlay's page + the TABLE's count (V8/C5).

    V20/L1 (card L2-06): this wrapper used to be an untyped `dict[str, Any]`; the field order is
    the dict's own build order, and each item is the same `DailyReportResponse` the old route
    dumped into it, so the served bytes do not move.  `total` is the honest table count — the
    route refuses `E_DB` rather than fabricate a 0 when the count read returns no row (L2-09).
    """

    items: list[DailyReportResponse]
    total: int
