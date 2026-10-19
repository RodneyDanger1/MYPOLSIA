"""GET /api/v1/dashboard/*

`/summary` answers with ONE statement: the four KPI counts and the active-agent
list come from a single grouped scan of the day window, and the company KPIs and
the last report date ride along as scalar subqueries of the same SELECT.  The
original issued four `count(*)` calls plus a company read plus a report read
(measured at 294.968 ms on 200k rows; the grouped aggregate on an indexed schema is
0.905 ms — `matrix-s200k.json`, 4 → 1 round trips).

Day window: `day_window(db)` — the frozen helper's own default, i.e. the company
timezone from `settings.company_timezone`, dialect-bound.  Reading
`company_config.timezone` first would cost a second round trip, so the window uses
the configured company timezone and the statement *also* returns the row's timezone:
if the two disagree, the mismatch is logged with both values instead of silently
shifting the window (receipt: `## UNVERIFIED` #3 and REQUESTS #5).
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.params import StrictDate
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.config import settings
from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.core.timeutil import day_window, dialect_name, today_in
from app.models.company import CompanyConfig
from app.models.report import ActivityLog, DailyReport
from app.models.task import Task
from app.schemas.report import (
    ActivityEventResponse,
    DailyReportResponse,
    DashboardSummaryResponse,
    ReportsListResponse,
)
from app.services.activity_service import get_recent_activity
from app.services.query_helpers import agent_rollup_expr, fetch_one, fetch_scalars
from app.services.report_service import build_cash_block, get_daily_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(verify_api_key)])


def _summary_statement(db: AsyncSession):
    """The whole dashboard summary as one SELECT (see module docstring)."""
    start, end = day_window(db)
    rollup = agent_rollup_expr(dialect_name(db), Task.agent_type)

    today = (
        select(
            func.count().label("total"),
            func.sum(case((Task.status == "completed", 1), else_=0)).label("completed"),
            func.sum(case((Task.status == "pending", 1), else_=0)).label("pending"),
            func.sum(case((Task.status == "failed", 1), else_=0)).label("failed"),
            rollup.label("agents"),
        )
        .where(Task.created_at >= start, Task.created_at < end)
        .subquery("today")
    )
    company_row = (
        select(CompanyConfig.kpis.label("kpis"), CompanyConfig.timezone.label("timezone"))
        .order_by(CompanyConfig.id)
        .limit(1)
        .subquery("company_row")
    )
    # LEFT JOIN keeps the aggregate's single row when there is no company row at
    # all (the empty-database case the shipped test exercises).
    return select(
        today.c.total,
        today.c.completed,
        today.c.pending,
        today.c.failed,
        today.c.agents,
        select(func.max(DailyReport.report_date)).scalar_subquery().label("last_report_date"),
        company_row.c.kpis.label("kpis"),
        company_row.c.timezone.label("company_timezone"),
    ).select_from(today.outerjoin(company_row, true()))


def _split_agents(rollup_value: str | None) -> list[str]:
    if not rollup_value:
        return []
    return sorted({part for part in str(rollup_value).split(",") if part})


@router.get("/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(db: AsyncSession = Depends(get_db)) -> DashboardSummaryResponse:
    with refusal_guard():
        row = await fetch_one(db, _summary_statement(db), what="dashboard summary")
    if row is None:  # the aggregate always yields exactly one row; None here is a real fault
        # V20/L1 (card L2-09): this was a bare `RuntimeError` — an UNCODED 500 for a driver that
        # broke its own `SELECT ... aggregate` contract.  Refuse BY NAME, the sibling pattern
        # `run_service.count_runs` already uses (E_DB, 503, how_to_fix, the query named).
        raise PolsiaError(
            E_DB,
            "dashboard summary statement returned no row",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"query": "dashboard summary"},
        )

    configured_tz = settings.company_timezone or "UTC"
    if row.company_timezone and row.company_timezone != configured_tz:
        logger.warning(
            "dashboard summary computed the day window in COMPANY_TIMEZONE=%s but "
            "company_config.timezone=%s; align them so 'today' means the same day "
            "everywhere (the window is not re-computed per request, to keep this "
            "endpoint at one round trip)",
            configured_tz,
            row.company_timezone,
        )
    # V24b: `kpis` is jsonb on PostgreSQL, so a LIST is legal at rest; the response model
    # requires a mapping, and passing the list through was an uncoded 500 for the whole
    # dashboard (measured by V24/L6).  A mis-shaped cell is "not measured": `{}` plus a named
    # warning — never fabricated KPIs and never a crashed surface.
    kpis = row.kpis if isinstance(row.kpis, dict) else {}
    if row.kpis is not None and not isinstance(row.kpis, dict):
        logger.warning(
            "dashboard summary: company_config.kpis is %s, not a mapping; reading it as absent",
            type(row.kpis).__name__,
        )
    return DashboardSummaryResponse(
        tasks_today_total=int(row.total or 0),
        tasks_today_completed=int(row.completed or 0),
        tasks_today_pending=int(row.pending or 0),
        tasks_today_failed=int(row.failed or 0),
        active_agents=_split_agents(row.agents),
        kpis=kpis,
        last_report_date=row.last_report_date,
    )


@router.get("/activity", response_model=list[ActivityEventResponse])
async def dashboard_activity(
    response: Response,
    limit: int = Query(default=50, ge=1, le=500),
    before: int | None = Query(
        default=None,
        ge=1,
        description="cursor: return activity older than this row id (feed pagination)",
    ),
    db: AsyncSession = Depends(get_db),
) -> list[ActivityEventResponse]:
    """The activity feed (most-recent first) + the honest count that satisfies the cursor.

    V20/L1 (card L2-07): the cursor feed serves `limit` and no total, so a full page could not be
    told from the end of the feed.  `X-Polsia-Total` now declares how many rows match the SAME
    predicate the page used (`id < before` when a cursor is supplied) — a cursor feed's honest
    total is "rows at or below this point", not the whole table.
    """
    with refusal_guard():
        entries = await get_recent_activity(db, limit=limit, before_id=before)
        total = await declared_total(
            db,
            ActivityLog,
            [] if before is None else [ActivityLog.id < before],
            what="activity total",
        )
    response.headers[TOTAL_HEADER] = str(total)
    return [ActivityEventResponse.model_validate(entry) for entry in entries]


@router.get("/reports/daily", response_model=DailyReportResponse | None)
async def dashboard_daily_report(
    report_date: Annotated[StrictDate, Query()] = None,
    db: AsyncSession = Depends(get_db),
) -> DailyReportResponse | None:
    """200 with `null` when no report exists for the day (NOT 404).

    V9 B2: the response also carries the live `cash_block` (cash/tax) — composed here from the
    existing finance reads, so the block reflects the CURRENT money state next to the day's
    report text; a missing input renders null + reason on the block itself.
    """
    with refusal_guard():
        report = await get_daily_report(db, report_date or today_in())
        if report is None:
            return None
        response = DailyReportResponse.model_validate(report)
        response.cash_block = await build_cash_block(db)
    return response


@router.get("/reports", response_model=ReportsListResponse)
async def dashboard_reports_list(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ReportsListResponse:
    """V8/C5 — the documents REPORTS list: `daily_reports` rows, newest first, `{items,total}`.

    No new table and no cached shape: the rows are the truth.  `total` is the TABLE's count
    (not the page's), so the frontend documents overlay (V8b) can page honestly.  Same refusal
    discipline as the sibling route: a driver failure is a named `E_DB`, never a bare 500.

    V20/L1 (cards L2-06 + L2-09): the response is the declared `ReportsListResponse` instead of
    `dict[str, Any]` (so OpenAPI carries the shape), and an ABSENT count row is now the named
    `E_DB` refusal instead of `total: 0` — a fabricated measurement is the one thing the
    null-honesty rule forbids (the runs ledger's `count_runs` refuses the same state).
    """
    with refusal_guard():
        rows = await fetch_scalars(
            db,
            select(DailyReport)
            .order_by(DailyReport.report_date.desc(), DailyReport.id.desc())
            .limit(limit)
            .offset(offset),
            what="daily reports list",
        )
        total = await declared_total(db, DailyReport, [], what="daily reports total")
    return ReportsListResponse(
        items=[DailyReportResponse.model_validate(row) for row in rows],
        total=total,
    )
