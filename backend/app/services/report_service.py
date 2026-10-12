"""daily_reports lifecycle (idempotent get-or-create)."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.models.report import DailyReport


async def get_daily_report(db: AsyncSession, report_date: date) -> DailyReport | None:
    result = await db.execute(
        select(DailyReport).where(DailyReport.report_date == report_date)
    )
    return result.scalar_one_or_none()


async def get_or_create_daily_report(db: AsyncSession, report_date: date) -> DailyReport:
    """Idempotent: the same report_date always yields the same row/id.

    Flushes (so `.id` is populated) but never commits - the caller owns the
    transaction.
    """
    existing = await get_daily_report(db, report_date)
    if existing is not None:
        return existing

    report = DailyReport(report_date=report_date)
    db.add(report)
    await db.flush()
    return report


async def save_morning_plan(
    db: AsyncSession,
    report_date: date,
    plan: str,
    tasks_planned: int = 0,
) -> DailyReport:
    report = await get_or_create_daily_report(db, report_date)
    report.morning_plan = plan
    report.tasks_planned = tasks_planned
    report.updated_at = utcnow()
    await db.flush()
    return report


async def save_evening_summary(
    db: AsyncSession,
    report_date: date,
    summary: str | None = None,
    insights: list[str] | None = None,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    metrics_snapshot: dict[str, Any] | None = None,
) -> DailyReport:
    report = await get_or_create_daily_report(db, report_date)
    report.evening_summary = summary
    report.insights = list(insights or [])
    report.tasks_completed = tasks_completed
    report.tasks_failed = tasks_failed
    report.metrics_snapshot = metrics_snapshot or {}
    report.updated_at = utcnow()
    await db.flush()
    return report


async def get_latest_daily_report(db: AsyncSession) -> DailyReport | None:
    result = await db.execute(
        select(DailyReport).order_by(DailyReport.report_date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


__all__ = [
    "get_daily_report",
    "get_latest_daily_report",
    "get_or_create_daily_report",
    "save_evening_summary",
    "save_morning_plan",
]
