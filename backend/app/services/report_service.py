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


class _Unset:
    """A "not supplied" marker distinct from every real value (M5).

    `save_evening_summary` used to assign its optional fields unconditionally, so a
    re-run with default arguments erased what an earlier run had written — `None` cannot
    mean both "not supplied" and "set to null".  The sentinel makes the two states
    distinct; an explicit `None` still clears the field (that is the replacement path).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<unset>"


_UNSET = _Unset()


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
    summary: str | None | _Unset = _UNSET,
    insights: list[str] | None | _Unset = _UNSET,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    metrics_snapshot: dict[str, Any] | None | _Unset = _UNSET,
) -> DailyReport:
    """Idempotent per date; each OPTIONAL field is written only when supplied (M5).

    A re-run with default arguments must not erase what a previous run wrote, so
    `summary`/`insights`/`metrics_snapshot` default to the `_Unset` sentinel: not
    supplied -> left as-is; an explicit `None` still clears the field (the replacement
    path).  The two task counters keep their documented `0` default — the daily cycle
    supplies measured counts, and whether a re-run should EVER replace rather than merge
    the counters is the recorded REQUESTS item, not this function's call.
    """
    report = await get_or_create_daily_report(db, report_date)
    if not isinstance(summary, _Unset):
        report.evening_summary = summary
    if not isinstance(insights, _Unset):
        report.insights = list(insights or [])
    report.tasks_completed = tasks_completed
    report.tasks_failed = tasks_failed
    if not isinstance(metrics_snapshot, _Unset):
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
