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

from sqlalchemy import Text, and_, case, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.timeutil import day_window, dialect_name, today_in
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.report import DailyReport
from app.services import pricing_service
from app.services.finance_service import (
    REPORTING_CURRENCY,
    excluded_currency_filter,
    reporting_currency_filter,
)
from app.services.query_helpers import fetch_rows, fetch_scalar, json_or_none


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


async def _get_report_orm(db: AsyncSession, report_date: date) -> DailyReport | None:
    """The row as a LIVE ORM object — the WRITE path's read (`get_or_create` mutates it).

    Kept separate from the tolerant read below on purpose: a writer must hold a session-bound
    instance, and the tolerant loader returns a detached snapshot.  A corrupt JSON cell therefore
    still refuses the write path (documented residual, RECEIPT "RESIDUAL" #1); the READ paths the
    UI calls on every dashboard load must not 500 over it.
    """
    return await fetch_scalar(
        db,
        select(DailyReport).where(DailyReport.report_date == report_date),
        what=f"daily report {report_date}",
    )


def _report_read_columns(db: AsyncSession) -> tuple:
    """Every column of `daily_reports`, with the JSON cells read as TEXT.

    Reading `metrics_snapshot` as the ORM JSON column makes SQLAlchemy's result processor raise
    `JSONDecodeError` WHILE FETCHING when one at-rest row holds malformed bytes — the whole
    `GET /dashboard/reports/daily` answered a bare 500 (measured:
    `pre/probe_junk_json.txt`: `500 /api/v1/dashboard/reports/daily`).  As TEXT, one bad cell is
    parsed per row below and reads as null ("not measured"), the same rule `run_service` and
    `notifications` already apply to their JSON columns.
    """
    insights: Any
    if dialect_name(db) == "postgresql":
        # `insights` is a real text[] on PostgreSQL: the driver can only return a list, and
        # casting it to Text would produce array-literal text rather than JSON.
        insights = DailyReport.insights
    else:
        insights = cast(DailyReport.insights, Text)
    return (
        DailyReport.id,
        DailyReport.report_date,
        DailyReport.morning_plan,
        DailyReport.evening_summary,
        DailyReport.tasks_planned,
        DailyReport.tasks_completed,
        DailyReport.tasks_failed,
        cast(DailyReport.metrics_snapshot, Text).label("metrics_snapshot_text"),
        insights.label("insights_value"),
        DailyReport.created_at,
        DailyReport.updated_at,
    )


def _report_from_row(row: Any) -> DailyReport:
    """A detached `DailyReport` with the JSON cells parsed tolerantly (junk -> None)."""
    values = row._mapping
    report = DailyReport(
        id=values["id"],
        report_date=values["report_date"],
        morning_plan=values["morning_plan"],
        evening_summary=values["evening_summary"],
        tasks_planned=values["tasks_planned"],
        tasks_completed=values["tasks_completed"],
        tasks_failed=values["tasks_failed"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
    )
    report.metrics_snapshot = json_or_none(values["metrics_snapshot_text"])
    insights = values["insights_value"]
    if isinstance(insights, str):        # SQLite stores it as JSON text
        insights = json_or_none(insights)
    report.insights = insights if isinstance(insights, list) else None
    return report


async def get_daily_report(db: AsyncSession, report_date: date) -> DailyReport | None:
    """The day's report, or `None`; a driver failure is a named `E_DB` (R32).

    V24/L6: the row is read column-by-column with its JSON cells cast to TEXT, so ONE malformed
    at-rest cell cannot turn this read into an uncaught `JSONDecodeError` 500 — it reads as
    null ("not measured") and the honest columns beside it still serve.  The object returned is
    DETACHED (read-only); the write path uses `_get_report_orm`.
    """
    rows = await fetch_rows(
        db,
        select(*_report_read_columns(db))
        .where(DailyReport.report_date == report_date)
        .limit(1),
        what=f"daily report {report_date}",
    )
    return _report_from_row(rows[0]) if rows else None


async def get_or_create_daily_report(db: AsyncSession, report_date: date) -> DailyReport:
    """Idempotent: the same report_date always yields the same row/id.

    L3-09 (V18): the read-then-insert was a TOCTOU window — two concurrent ticks both saw
    `None` and both inserted, and the loser's `flush()` raised an uncoded IntegrityError out of
    the task.  The DB guarantee is `daily_reports.report_date UNIQUE` (model + migration
    `0001_initial_schema.py:249`); this function now LOSES the race by design: the insert runs
    inside a SAVEPOINT, an IntegrityError rolls the savepoint back (the caller's transaction
    survives, so nothing else in the tick is lost), and the winner's row is re-read and
    returned.  A constraint failure with NO visible row is re-raised — never a fabricated
    report.

    Flushes (so `.id` is populated) but never commits - the caller owns the
    transaction.
    """
    existing = await _get_report_orm(db, report_date)
    if existing is not None:
        return existing

    report = DailyReport(report_date=report_date)
    try:
        async with db.begin_nested():
            db.add(report)
            await db.flush()
    except IntegrityError:
        # Another writer inserted this day's row between our read and our flush.  The savepoint
        # rollback discards OUR insert; theirs is the answer.
        if report in db:
            db.expunge(report)
        winner = await _get_report_orm(db, report_date)
        if winner is None:
            raise
        return winner
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
    class as `get_daily_report` above; this file had exactly those two raw `db.execute` sites).

    V24/L6: same tolerant column read as `get_daily_report` — a malformed JSON cell in the
    newest row must not 500 the dashboard/chat formatting that only wanted its text.
    """
    rows = await fetch_rows(
        db,
        select(*_report_read_columns(db))
        .order_by(DailyReport.report_date.desc())
        .limit(1),
        what="latest daily report",
    )
    return _report_from_row(rows[0]) if rows else None


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

    CURRENCY SCOPE (A2A7-03).  Every money figure here is denominated in `REPORTING_CURRENCY`
    (`finance_service.reporting_currency_filter` — the ONE definition of the rule, shared with
    `get_finance_summary`).  A row in another currency is EXCLUDED, never converted (a converted
    figure needs a stored rate and rate date — an owner decision) and never silently dropped:
    `foreign_currency_rows` counts them, and each affected field is named in `reasons` under
    `<field>.currency` (the `burn_line_items.<category>` precedent).  A NULL currency is
    "unknown", not usd, so it is excluded and counted the same way.

    Null-honesty, stated per field:
    * `cash_on_hand` — the newest revenue snapshot's Stripe balance; none recorded -> null.
    * `out_30d` — summed USD expense records in the window; no rows -> UNMEASURED (null), never 0;
      rows that all sit in another currency -> UNMEASURED (null) with that reason.
    * `in_30d` — summed USD money-in stripe events in the window; no events at all -> UNMEASURED
      (the webhook may simply not be configured); USD events exist but none succeeded -> a measured
      0; money-in events that all sit in another currency -> UNMEASURED (null); money-in events
      whose amount was never stated -> UNMEASURED (null), because a partial sum is not a total
      (A2A7-02's rule, applied to the inflow: a NULL amount cannot be summed into $0.00).
    * `tax_set_aside` — USD tax-categorised expenses in the window; a USD ledger with rows but no
      tax category is a measured 0; an empty (or entirely foreign) ledger is null.

    THE WINDOW IS HALF-OPEN.  `day_window` answers `[start, end)`; both bounds are used.  The
    pre-fix read was an open-ended `date >= start`, so a FUTURE-dated expense (an agent that books
    next month's bill) counted as money already out — the exact unbounded-range defect
    `_month_bounds` records for the month window (measured: one row dated +30 days turned a real
    `out_30d` of 100.00 into 1099.99, `wave-b1/raw/D4/probe_attacks.out.json` §B).
    """
    reasons: dict[str, str] = {}
    today = today_in()
    # The window's two bounds are typed PER COLUMN.  `expense_records.date` is a DATE, so it is
    # bounded by DATES (the `_month_bounds` pattern): comparing a DATE to a timestamp bound renders
    # `'YYYY-MM-DD'` against `'YYYY-MM-DDThh:mm:ss…'` on SQLite, which drops the window's FIRST day
    # and admits the day after its last.  `stripe_events.processed_at` is a timestamp and is
    # bounded by `day_window`'s instants, `[start, end)` with `end` = tomorrow's start.
    window_start = today - timedelta(days=CASH_WINDOW_DAYS - 1)
    start, _ = day_window(db, window_start)
    _, end = day_window(db, today)

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

    # ONE pass over the window's expenses: how many rows exist at all (telemetry), how many are
    # USD (the only ones summed), and how many were EXCLUDED for their currency.
    expense_rows = await fetch_rows(
        db,
        select(
            func.count().label("rows"),
            func.sum(
                case(
                    (
                        reporting_currency_filter(ExpenseRecord.currency),
                        ExpenseRecord.amount_cents,
                    ),
                    else_=None,
                )
            ).label("usd_cents"),
            func.sum(
                case((reporting_currency_filter(ExpenseRecord.currency), 1), else_=0)
            ).label("usd_rows"),
            func.sum(
                case((excluded_currency_filter(ExpenseRecord.currency), 1), else_=0)
            ).label("foreign_rows"),
        ).where(ExpenseRecord.date >= window_start, ExpenseRecord.date <= today),
        what="cash block expenses",
    )
    expense_count, usd_expense_cents, usd_expense_count, foreign_expense_count = (
        expense_rows[0] if expense_rows else (0, None, 0, 0)
    )
    usd_expense_count = int(usd_expense_count or 0)
    foreign_expense_count = int(foreign_expense_count or 0)

    out_30d: float | None = None
    if not expense_count:
        reasons["out_30d"] = (
            f"no expense records in the last {CASH_WINDOW_DAYS} days — outflow is unmeasured"
        )
    elif not usd_expense_count:
        reasons["out_30d"] = (
            f"all {foreign_expense_count} expense record(s) in the last {CASH_WINDOW_DAYS} days "
            f"are in another currency than {REPORTING_CURRENCY} — the USD outflow is unmeasured "
            "(nothing was converted)"
        )
    elif usd_expense_cents is None:
        reasons["out_30d"] = "expense rows in the window carry no measured amounts"
    else:
        out_30d = round(float(usd_expense_cents) / 100, 2)
    if foreign_expense_count and out_30d is not None:
        reasons["out_30d.currency"] = (
            f"{foreign_expense_count} expense record(s) in another currency were EXCLUDED from "
            f"the USD outflow (nothing was converted)"
        )

    # The inflow read: every event in the window is telemetry ("did the webhook deliver?"), while
    # the SUM counts only USD money-in events and the exclusions are counted separately.
    event_rows = await fetch_rows(
        db,
        select(
            func.count().label("rows"),
            func.sum(
                case(
                    (
                        and_(
                            reporting_currency_filter(StripeEvent.currency),
                            StripeEvent.event_type.in_(MONEY_IN_EVENT_TYPES),
                        ),
                        StripeEvent.amount_cents,
                    ),
                    else_=None,
                )
            ).label("usd_in_cents"),
            func.sum(
                case(
                    (
                        and_(
                            reporting_currency_filter(StripeEvent.currency),
                            StripeEvent.event_type.in_(MONEY_IN_EVENT_TYPES),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("usd_in_rows"),
            func.sum(
                case(
                    (
                        and_(
                            excluded_currency_filter(StripeEvent.currency),
                            StripeEvent.event_type.in_(MONEY_IN_EVENT_TYPES),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("foreign_in_rows"),
            # A2A7-02's rule, on the inflow side: `SUM` skips a NULL amount ROW BY ROW, so a
            # money-in delivery Stripe stated no amount for would be summed away and the rest
            # served as the complete inflow.  Counted here so the caller can refuse the partial.
            func.sum(
                case(
                    (
                        and_(
                            reporting_currency_filter(StripeEvent.currency),
                            StripeEvent.event_type.in_(MONEY_IN_EVENT_TYPES),
                            StripeEvent.amount_cents.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("usd_in_unmeasured"),
        ).where(StripeEvent.processed_at >= start, StripeEvent.processed_at < end),
        what="cash block stripe telemetry",
    )
    events_count, usd_in_cents, usd_in_count, foreign_in_count, usd_in_unmeasured = (
        event_rows[0] if event_rows else (0, None, 0, 0, 0)
    )
    usd_in_count = int(usd_in_count or 0)
    foreign_in_count = int(foreign_in_count or 0)
    usd_in_unmeasured = int(usd_in_unmeasured or 0)

    in_30d: float | None = None
    if not events_count:
        reasons["in_30d"] = (
            f"no Stripe events in the last {CASH_WINDOW_DAYS} days — inflow is unmeasured "
            "(check that the webhook is configured)"
        )
    elif not usd_in_count and foreign_in_count:
        reasons["in_30d"] = (
            f"the window's {foreign_in_count} money-in Stripe event(s) are in another currency "
            f"than {REPORTING_CURRENCY} — the USD inflow is unmeasured (nothing was converted)"
        )
    elif usd_in_unmeasured:
        reasons["in_30d"] = (
            f"{usd_in_unmeasured} of the window's {usd_in_count} USD money-in Stripe event(s) "
            "carry no measured amount — the USD inflow cannot be summed (a partial sum is not a "
            "total, and $0.00 would be a measurement nobody made)"
        )
    else:
        # A USD ledger with events and no USD money-in is a MEASURED zero, unlike the
        # no-telemetry case above.
        in_30d = round(float(usd_in_cents or 0) / 100, 2)
        if foreign_in_count:
            reasons["in_30d.currency"] = (
                f"{foreign_in_count} money-in Stripe event(s) in another currency were EXCLUDED "
                f"from the USD inflow (nothing was converted)"
            )

    tax_rows = await fetch_rows(
        db,
        select(
            func.sum(
                case(
                    (
                        reporting_currency_filter(ExpenseRecord.currency),
                        ExpenseRecord.amount_cents,
                    ),
                    else_=None,
                )
            ).label("usd_cents"),
            func.sum(
                case(
                    (
                        and_(
                            excluded_currency_filter(ExpenseRecord.currency),
                            func.lower(ExpenseRecord.category).in_(TAX_EXPENSE_CATEGORIES),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("foreign_rows"),
        ).where(
            ExpenseRecord.date >= window_start,
            ExpenseRecord.date <= today,
            func.lower(ExpenseRecord.category).in_(TAX_EXPENSE_CATEGORIES),
        ),
        what="cash block tax set aside",
    )
    usd_tax_cents, foreign_tax_count = tax_rows[0] if tax_rows else (None, 0)
    foreign_tax_count = int(foreign_tax_count or 0)

    tax_set_aside: float | None = None
    if not expense_count:
        reasons["tax_set_aside"] = (
            f"no expense records in the last {CASH_WINDOW_DAYS} days — the tax set-aside is unmeasured"
        )
    elif not usd_expense_count:
        reasons["tax_set_aside"] = (
            f"the window's expense records are all in another currency than {REPORTING_CURRENCY} "
            "— the USD tax set-aside is unmeasured (nothing was converted)"
        )
    else:
        # A USD ledger with rows but no tax category: a measured 0 (nobody has set anything
        # aside yet).  Foreign tax rows are excluded AND counted below.
        tax_set_aside = round(float(usd_tax_cents or 0) / 100, 2)
        if foreign_tax_count:
            reasons["tax_set_aside.currency"] = (
                f"{foreign_tax_count} tax-categorised expense(s) in another currency were "
                f"EXCLUDED from the USD set-aside (nothing was converted)"
            )

    return {
        "cash_on_hand": cash_on_hand,
        "in_30d": in_30d,
        "out_30d": out_30d,
        "tax_set_aside": tax_set_aside,
        "foreign_currency_rows": foreign_expense_count + foreign_in_count,
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
