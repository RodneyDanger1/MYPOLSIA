"""daily_reports lifecycle (idempotent get-or-create).

Read paths refuse by name (R32): `get_daily_report` / `get_latest_daily_report` used raw
`db.execute`, so a database outage answered `GET /api/v1/dashboard/reports/daily` with a bare
500 — no code, no hint (R28 receipt, REQUESTS #4; measured in
`raw/probe_report_db_failure_before.txt`).  Both now go through `query_helpers.fetch_scalar`,
i.e. the same `E_DB`/503 refusal `task_service` got in R28.  Writes (`db.flush`) stay untouched:
the caller owns the transaction.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.models.report import DailyReport
from app.services.query_helpers import fetch_scalar


async def get_daily_report(db: AsyncSession, report_date: date) -> DailyReport | None:
    """The day's report, or `None`; a driver failure is a named `E_DB` (R32).

    `fetch_scalar` is `scalar_one_or_none`'s single-column result — exactly equivalent under
    the UNIQUE(`report_date`) constraint — plus the refusal translation, the same shape R28
    used for `task_service.get_task`.
    """
    return await fetch_scalar(
        db,
        select(DailyReport).where(DailyReport.report_date == report_date),
        what=f"daily report {report_date}",
    )


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
    """Newest report, or `None`; a driver failure is a named `E_DB` (R32 — same raw-execute
    class as `get_daily_report` above; this file had exactly those two raw `db.execute` sites)."""
    return await fetch_scalar(
        db,
        select(DailyReport).order_by(DailyReport.report_date.desc()).limit(1),
        what="latest daily report",
    )


__all__ = [
    "get_daily_report",
    "get_latest_daily_report",
    "get_or_create_daily_report",
    "save_evening_summary",
    "save_morning_plan",
]
