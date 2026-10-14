"""daily_reports lifecycle (idempotent get-or-create).

Read paths refuse by name (R32): `get_daily_report` / `get_latest_daily_report` used raw
`db.execute`, so a database outage answered `GET /api/v1/dashboard/reports/daily` with a bare
500 — no code, no hint (R28 receipt, REQUESTS #4; measured in
`raw/probe_report_db_failure_before.txt`).  Both now go through `query_helpers.fetch_scalar`,
i.e. the same `E_DB`/503 refusal `task_service` got in R28.  Writes (`db.flush`) stay untouched:
the caller owns the transaction.

V9 B2 adds `build_cash_block` — the daily report's cash/tax block (plan §9 lines 454-456): cash
on hand, 30-day in/out, tax set-aside, the next 4 quarterly estimated-tax dates, the safe-harbor
note and the persistent "Not tax advice — verify with MN Revenue." line.  Every figure is derived
from EXISTING finance reads (revenue snapshots, expense records, stripe events); a missing input
renders null + a reason in `reasons` — never a fabricated 0.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.timeutil import day_window, today_in
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.report import DailyReport
from app.services import pricing_service
from app.services.query_helpers import fetch_rows, fetch_scalar


class _Unset:
    """A "not supplied" marker distinct from every real value (M5).

    `save_evening_summary` used to assign its optional fields unconditionally, so a
    re-run with default arguments erased what an earlier run had written — `None` cannot
    mean both "not supplied" and "set to null".  The sentinel makes the two states
    distinct; an explicit `None` still clears the field (that is the replacement path).
    D19 extends the same rule to the COUNTERS (`tasks_completed`/`tasks_failed` and the
    morning plan's `tasks_planned`): a re-run that supplies no counts must not zero the
    counts a previous run measured.
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
    tasks_planned: int | None | _Unset = _UNSET,
) -> DailyReport:
    """Idempotent per date; the counter is written only when supplied (D19).

    `tasks_planned` carried a bare `int = 0` default, so a re-run that supplied only the plan
    text silently reset a measured count to 0 — the same "not supplied" vs "measured zero"
    collapse the evening counters had.  The daily cycle always supplies the count, so the
    production write is unchanged; an explicit `None` clears it (the column is nullable).
    """
    report = await get_or_create_daily_report(db, report_date)
    report.morning_plan = plan
    if not isinstance(tasks_planned, _Unset):
        report.tasks_planned = tasks_planned
    report.updated_at = utcnow()
    await db.flush()
    return report


async def save_evening_summary(
    db: AsyncSession,
    report_date: date,
    summary: str | None | _Unset = _UNSET,
    insights: list[str] | None | _Unset = _UNSET,
    tasks_completed: int | None | _Unset = _UNSET,
    tasks_failed: int | None | _Unset = _UNSET,
    metrics_snapshot: dict[str, Any] | None | _Unset = _UNSET,
) -> DailyReport:
    """Idempotent per date; each OPTIONAL field is written only when supplied (M5 + D19).

    A re-run with default arguments must not erase what a previous run wrote, so
    `summary`/`insights`/`metrics_snapshot` default to the `_Unset` sentinel: not
    supplied -> left as-is; an explicit `None` still clears the field (the replacement
    path).  D19: the two task counters follow the SAME rule — they used to default to a
    literal `0` and were assigned unconditionally, so a re-run that measured nothing zeroed
    the completion/failure counts an earlier run had measured (the exact silent regression
    the sentinel exists to prevent).  The daily cycle always supplies both counts, so the
    production write is unchanged; an explicit `None` clears them (the columns are
    nullable).
    """
    report = await get_or_create_daily_report(db, report_date)
    if not isinstance(summary, _Unset):
        report.evening_summary = summary
    if not isinstance(insights, _Unset):
        report.insights = list(insights or [])
    if not isinstance(tasks_completed, _Unset):
        report.tasks_completed = tasks_completed
    if not isinstance(tasks_failed, _Unset):
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


# ── the daily report's cash/tax block (V9 B2, plan §9 lines 454-456) ──────────────────────────

#: Stripe event types that are money IN (a payment that succeeded).
MONEY_IN_EVENT_TYPES = (
    "charge.succeeded",
    "invoice.paid",
    "invoice.payment_succeeded",
    "payment_intent.succeeded",
)
#: Expense categories that mean "money set aside for tax".
TAX_EXPENSE_CATEGORIES = ("tax", "taxes", "tax_set_aside")
#: The rolling window the in/out figures cover, in company-local days (today inclusive).
CASH_WINDOW_DAYS = 30


async def build_cash_block(db: AsyncSession) -> dict[str, Any]:
    """The cash/tax block the daily report carries — derived from EXISTING finance reads.

    Shape (plan §9 lines 454-456 + the (f) tax additions): `cash_on_hand`, `in_30d`, `out_30d`,
    `tax_set_aside`, `next_tax_dates` (the next 4 quarterly due dates, IRS schedule),
    `safe_harbor_note`, `sales_tax_note`, `tax_disclaimer` (the persistent line), and `reasons`
    for every null.

    Null-honesty, stated per field:
    * `cash_on_hand` — the newest revenue snapshot's Stripe balance; none recorded -> null.
    * `out_30d` — summed expense records in the window; no rows -> UNMEASURED (null), never 0.
    * `in_30d` — summed money-in stripe events in the window; no events at all -> UNMEASURED
      (the webhook may simply not be configured); events exist but none succeeded -> a measured 0.
    * `tax_set_aside` — tax-categorised expenses in the window; a ledger with rows but no tax
      category is a measured 0; an empty ledger is null.
    """
    reasons: dict[str, str] = {}
    today = today_in()
    start, _ = day_window(db, today - timedelta(days=CASH_WINDOW_DAYS - 1))

    latest = await fetch_scalar(
        db,
        select(RevenueSnapshot)
        .order_by(RevenueSnapshot.snapshot_date.desc(), RevenueSnapshot.id.desc())
        .limit(1),
        what="latest revenue snapshot",
    )
    cash_on_hand: float | None = None
    if latest is None:
        reasons["cash_on_hand"] = (
            "no revenue snapshot has been recorded yet (the finance agent writes one per run)"
        )
    elif latest.stripe_balance_cents is None:
        reasons["cash_on_hand"] = (
            f"the snapshot of {latest.snapshot_date} carries no measured Stripe balance"
        )
    else:
        cash_on_hand = round(float(latest.stripe_balance_cents) / 100, 2)

    expense_rows = await fetch_rows(
        db,
        select(func.count(), func.sum(ExpenseRecord.amount_cents)).where(
            ExpenseRecord.date >= start
        ),
        what="cash block expenses",
    )
    expense_count, expense_cents = expense_rows[0] if expense_rows else (0, None)
    out_30d: float | None = None
    if not expense_count:
        reasons["out_30d"] = (
            f"no expense records in the last {CASH_WINDOW_DAYS} days — outflow is unmeasured"
        )
    elif expense_cents is None:
        reasons["out_30d"] = "expense rows in the window carry no measured amounts"
    else:
        out_30d = round(float(expense_cents) / 100, 2)

    events_count = await fetch_scalar(
        db,
        select(func.count()).where(StripeEvent.processed_at >= start),
        what="cash block stripe telemetry",
    )
    in_30d: float | None = None
    if not events_count:
        reasons["in_30d"] = (
            f"no Stripe events in the last {CASH_WINDOW_DAYS} days — inflow is unmeasured "
            "(check that the webhook is configured)"
        )
    else:
        money_in = await fetch_scalar(
            db,
            select(func.sum(StripeEvent.amount_cents)).where(
                StripeEvent.processed_at >= start,
                StripeEvent.event_type.in_(MONEY_IN_EVENT_TYPES),
            ),
            what="cash block stripe money in",
        )
        # Telemetry exists and none of it succeeded: a MEASURED zero, unlike the no-telemetry case.
        in_30d = round(float(money_in or 0) / 100, 2)

    tax_set_aside: float | None = None
    if not expense_count:
        reasons["tax_set_aside"] = (
            f"no expense records in the last {CASH_WINDOW_DAYS} days — the tax set-aside is unmeasured"
        )
    else:
        tax_cents = await fetch_scalar(
            db,
            select(func.sum(ExpenseRecord.amount_cents)).where(
                ExpenseRecord.date >= start,
                func.lower(ExpenseRecord.category).in_(TAX_EXPENSE_CATEGORIES),
            ),
            what="cash block tax set aside",
        )
        # Rows exist but no tax category: a measured 0 (nobody has set anything aside yet).
        tax_set_aside = round(float(tax_cents or 0) / 100, 2)

    return {
        "cash_on_hand": cash_on_hand,
        "in_30d": in_30d,
        "out_30d": out_30d,
        "tax_set_aside": tax_set_aside,
        "next_tax_dates": [d.isoformat() for d in pricing_service.next_quarterly_tax_dates(today)],
        "safe_harbor_note": pricing_service.SAFE_HARBOR_NOTE,
        "sales_tax_note": pricing_service.SALES_TAX_NOTE,
        "tax_disclaimer": pricing_service.TAX_DISCLAIMER,
        "reasons": reasons,
    }


__all__ = [
    "build_cash_block",
    "get_daily_report",
    "get_latest_daily_report",
    "get_or_create_daily_report",
    "save_evening_summary",
    "save_morning_plan",
]
