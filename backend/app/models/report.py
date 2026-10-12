"""activity_log + daily_reports."""
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, Integer, String, Text, func

from app.core.database import Base
from app.models.types import json_type, string_list_type


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True)
    agent_type = Column(String(100), nullable=False)
    action = Column(String(255), nullable=False)
    summary = Column(Text, nullable=False)
    detail = Column(json_type())
    level = Column(String(20), server_default="info", default="info")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ActivityLog id={self.id} {self.agent_type}:{self.action}>"


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True)
    report_date = Column(Date, unique=True, nullable=False)
    morning_plan = Column(Text)
    evening_summary = Column(Text)
    tasks_planned = Column(Integer, server_default="0", default=0)
    tasks_completed = Column(Integer, server_default="0", default=0)
    tasks_failed = Column(Integer, server_default="0", default=0)
    metrics_snapshot = Column(json_type())
    insights = Column(string_list_type())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DailyReport {self.report_date}>"
