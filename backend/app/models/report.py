"""activity_log + daily_reports."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type, string_list_type


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    # NULL is legal (A-T3 decision b): normalized to "info" at events.activity_payload; no migration.
    level: Mapped[str | None] = mapped_column(
        String(20), server_default="info", default="info"
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActivityLog id={self.id} {self.agent_type}:{self.action}>"


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    morning_plan: Mapped[str | None] = mapped_column(Text)
    evening_summary: Mapped[str | None] = mapped_column(Text)
    tasks_planned: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    tasks_completed: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    tasks_failed: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    metrics_snapshot: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    insights: Mapped[list[str] | None] = mapped_column(string_list_type())
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DailyReport {self.report_date}>"
