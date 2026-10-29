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

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import PolsiaError
from app.core.timeutil import day_window, dialect_name, today_in
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.report import ActivityLog, DailyReport
from app.models.task import Task
from app.services import pricing_service
from app.services.finance_service import (
    REPORTING_CURRENCY,
    excluded_currency_filter,
    persisted_payment_intent_id,
    reporting_currency_filter,
)
from app.services.query_helpers import fetch_rows, fetch_scalar, fetch_scalars, json_or_none


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


# ── F-43: the day digest (what the evening report could not see) ─────────────────────────────

#: How many completed/failed rows ONE digest carries (bounded like every other feed payload).
DAY_DIGEST_MAX_ROWS = 25
#: Characters of ONE task's recorded result/error inside the digest (truncation is STATED).
DAY_DIGEST_TEXT_CHARS = 400


@dataclass(frozen=True)
class DayDigestTask:
    """ONE task that reached an outcome today — the fact the evening report was missing."""

    task_id: int
    title: str
    agent_type: str
    status: str
    text: str | None
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "agent_type": self.agent_type,
            "status": self.status,
            "text": self.text,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class DayDigest:
    """The company-local day's outcomes, as the evening report records them (F-43).

    `status` is part of the contract: `measured` means the rows below are the day's real
    outcomes, `empty` means the day really had none (that is a fact, and the report says it),
    `unavailable` means the read FAILED — and then `reason` carries the error code, because a
    report that cannot see its data must say so instead of printing zeros that look measured.
    """

    day: date
    status: str                       # measured | empty | unavailable
    counts_created: dict[str, int]
    completed: list[DayDigestTask]
    failed: list[DayDigestTask]
    cancelled: int
    requeued: int
    reaped_approvals: int
    ended: int = 0
    reason: str | None = None

    def headline(self) -> str:
        """ONE honest sentence about the day, built from what was actually read."""
        if self.status == "unavailable":
            return (
                f"the day's task data could not be read ({self.reason}); this report carries no "
                "task counts"
            )
        if self.status == "empty":
            return "no task reached an outcome today (nothing completed, failed or was cancelled)"
        parts = [
            f"{len(self.completed)} completed",
            f"{len(self.failed)} failed",
            f"{self.cancelled} cancelled",
        ]
        if self.requeued:
            parts.append(f"{self.requeued} stalled claim(s) requeued")
        if self.ended:
            parts.append(f"{self.ended} stalled claim(s) ended at the attempt bound")
        if self.reaped_approvals:
            parts.append(f"{self.reaped_approvals} parked approval(s) reaped")
        return ", ".join(parts) + " today"

    def as_metrics(self) -> dict[str, Any]:
        """The JSON shape stored in `daily_reports.metrics_snapshot['day_digest']`."""
        return {
            "day": self.day.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "counts_created": self.counts_created,
            "completed": [row.as_dict() for row in self.completed],
            "failed": [row.as_dict() for row in self.failed],
            "cancelled": self.cancelled,
            "requeued": self.requeued,
            "ended": self.ended,
            "reaped_approvals": self.reaped_approvals,
            "headline": self.headline(),
        }

    def prompt_block(self) -> str:
        """The digest as text an agent reads (the evening run's brief carries this, F-26/F-43)."""
        lines = [f"DAY OUTCOMES ({self.day.isoformat()}): {self.headline()}"]
        for label, rows in (("COMPLETED", self.completed), ("FAILED", self.failed)):
            for row in rows:
                text = (row.text or "").strip()
                if text:
                    lines.append(f"- [{label}] #{row.task_id} {row.title} (@{row.agent_type}): {text}")
                else:
                    lines.append(
                        f"- [{label}] #{row.task_id} {row.title} (@{row.agent_type}): "
                        "no result was recorded"
                    )
        return "\n".join(lines)


def _digest_text(value: Any) -> tuple[str | None, bool]:
    """ONE recorded result/error as digest text: bounded, with the cut STATED."""
    if value is None:
        return None, False
    text = str(value).strip()
    if not text:
        return None, False
    if len(text) > DAY_DIGEST_TEXT_CHARS:
        return text[: DAY_DIGEST_TEXT_CHARS - 1] + "…", True
    return text, False


async def build_day_digest(
    db: AsyncSession, day: date, *, tz_name: str | None = None
) -> DayDigest:
    """What actually happened on `day` — the evening report's missing half (F-43).

    The report counted only tasks CREATED that day (`daily_cycle._evening_cycle`), so a task
    created yesterday and finished today never appeared in today's report, and nothing in the
    report said what the agents PRODUCED.  This reads the rows that reached an outcome in the
    day's window (`updated_at`, company-local half-open bounds — the same window every other
    "today" uses), one bounded query, and returns the typed digest.

    Outcomes only: `completed`/`failed`/`cancelled`.  `pending`/`in_progress` rows are still
    running and `awaiting_approval` rows are waiting on the owner — neither is an outcome, and
    counting them as "today's work" is the thinness this replaces.

    A failed read is `status="unavailable"` with the error code in `reason` — never zeros.
    """
    start, end = day_window(db, day=day, tz_name=tz_name)
    digest_rows: list[DayDigestTask] = []
    cancelled = 0
    try:
        rows = await fetch_rows(
            db,
            select(
                Task.id,
                Task.title,
                Task.agent_type,
                Task.status,
                Task.result_summary,
                Task.error_message,
                Task.updated_at,
            )
            .where(
                Task.updated_at >= start,
                Task.updated_at < end,
                Task.status.in_(("completed", "failed", "cancelled")),
            )
            .order_by(Task.updated_at.desc(), Task.id.desc())
            .limit(DAY_DIGEST_MAX_ROWS),
            what=f"day digest {day}",
        )
        counts = await fetch_rows(
            db,
            select(Task.status, func.count())
            .where(Task.created_at >= start, Task.created_at < end)
            .group_by(Task.status),
            what=f"day digest created counts {day}",
        )
        # W9/R1: `stale_claims_requeued` is written for BOTH outcomes (requeued and terminal
        # ended).  Grouping on the action alone made the headline claim "N requeued" for rows the
        # ladder ENDED.  Read the detail and split the two; a legacy row with no outcome stays in
        # the requeued count (the pre-W9 writer's only meaning) and is named in the comment.
        activity = await fetch_rows(
            db,
            select(ActivityLog.action, ActivityLog.detail)
            .where(
                ActivityLog.created_at >= start,
                ActivityLog.created_at < end,
                ActivityLog.action.in_(("stale_claims_requeued", "stale_approvals_reaped")),
            ),
            what=f"day digest recovery counts {day}",
        )
    except PolsiaError as exc:
        # "A report that cannot see its data says so": the code travels, the counts do not.
        return DayDigest(
            day=day,
            status="unavailable",
            counts_created={},
            completed=[],
            failed=[],
            cancelled=0,
            requeued=0,
            reaped_approvals=0,
            reason=exc.code,
        )

    for task_id, title, agent_type, status, result_summary, error_message, _at in rows:
        state = str(status)
        if state == "cancelled":
            cancelled += 1
            continue
        text, truncated = _digest_text(result_summary if state == "completed" else error_message)
        digest_rows.append(
            DayDigestTask(
                task_id=int(task_id),
                title=str(title or ""),
                agent_type=str(agent_type or ""),
                status=state,
                text=text,
                truncated=truncated,
            )
        )
    completed = [row for row in digest_rows if row.status == "completed"]
    failed = [row for row in digest_rows if row.status == "failed"]
    recovery = {"stale_approvals_reaped": 0}
    requeued_count = 0
    ended_count = 0
    for action, detail in activity:
        if str(action) == "stale_approvals_reaped":
            recovery["stale_approvals_reaped"] += 1
            continue
        outcome = (detail or {}).get("outcome") if isinstance(detail, dict) else None
        if outcome == "failed":
            ended_count += 1
        else:
            # "requeued" and legacy rows without an outcome (the pre-W9 writer had only this meaning)
            requeued_count += 1
    return DayDigest(
        day=day,
        status="measured" if (digest_rows or cancelled) else "empty",
        counts_created={str(status): int(count) for status, count in counts},
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        requeued=requeued_count,
        ended=ended_count,
        reaped_approvals=recovery.get("stale_approvals_reaped", 0),
    )


async def get_evening_summary_for(db: AsyncSession, report_date: date) -> str | None:
    """THAT day's evening summary, or `None` — never another day's (F-43's `LIMIT 1` defect).

    `company_service.get_full_context` read "the newest `evening_summary` in the table" and
    labelled it `yesterday_summary`, so every prompt for weeks was fed the summary of the last
    day the cycle happened to run — with no hint of its date.  This answers the question the key
    actually asks ("yesterday's"), and the caller decides what to do when it is `None`.
    """
    report = await get_daily_report(db, report_date)
    if report is None:
        return None
    summary = report.evening_summary
    return summary if isinstance(summary, str) and summary.strip() else None


# ── the daily report's cash/tax block (V9 B2, plan §9 lines 454-456) ──────────────────────────
#: Stripe event types that are money IN (a payment that succeeded).
MONEY_IN_EVENT_TYPES = (
    "charge.succeeded",
    "invoice.paid",
    "invoice.payment_succeeded",
    "payment_intent.succeeded",
)
#: DT-03: when one payment is reported by several families, which family states its AMOUNT most
#: authoritatively (best first).  A settled `charge` is the money that actually moved; the
#: PaymentIntent and the invoice agree with it in the normal case, and when they disagree the
#: disagreement is NAMED in `reasons["in_30d.amount_conflict"]` rather than summed.
MONEY_IN_AMOUNT_PRECEDENCE = (
    "charge.succeeded",
    "payment_intent.succeeded",
    "invoice.paid",
    "invoice.payment_succeeded",
)
_MONEY_IN_PRECEDENCE_RANK = {
    event_type: rank for rank, event_type in enumerate(MONEY_IN_AMOUNT_PRECEDENCE)
}
#: Expense categories that mean "money set aside for tax".
TAX_EXPENSE_CATEGORIES = ("tax", "taxes", "tax_set_aside")
#: The rolling window the in/out figures cover, in company-local days (today inclusive).
CASH_WINDOW_DAYS = 30


@dataclass(frozen=True)
class _PaymentGroup:
    """One PAYMENT in the window: the amounts the event families reported for it.

    `amount_cents` is the authoritative measured amount (the highest-precedence family that
    states one), or `None` when no family stated one.  `amount_conflict` records that two
    families stated DIFFERENT amounts for the same payment — the block counts the payment once
    and names the disagreement instead of adding both up.
    """

    amount_cents: int | None
    event_count: int
    amount_conflict: bool


def _is_reporting_currency(currency: str | None) -> bool:
    """The read-side half of `reporting_currency_filter`, for a row already in Python.

    Same predicate as the SQL (`lower(coalesce(currency, '')) == 'usd'`): a NULL/blank currency is
    NOT usd, and nothing is converted.
    """
    return (currency or "").lower() == REPORTING_CURRENCY


def _payment_groups(events: list[StripeEvent]) -> list[_PaymentGroup]:
    """Group the window's USD money-in EVENTS into PAYMENTS (DT-03).

    One card payment produces up to four Stripe deliveries — `charge.succeeded`,
    `payment_intent.succeeded`, `invoice.paid`, `invoice.payment_succeeded` — and the block used to
    sum every one of them: one $100.00 payment read as $400.00 of money received.  The stored
    identity is the PaymentIntent id the webhook persisted on the event row
    (`finance_service.persisted_payment_intent_id`), so events that share it are one payment.

    An event with NO PaymentIntent cannot be PROVEN to be the same payment as any other; it is its
    own group and is counted exactly once (never dropped, never guessed into a family pair).
    """
    grouped: dict[str, list[StripeEvent]] = {}
    for event in events:
        payment_intent = persisted_payment_intent_id(event)
        key = f"pi:{payment_intent}" if payment_intent else f"event:{event.stripe_event_id}"
        grouped.setdefault(key, []).append(event)
    groups: list[_PaymentGroup] = []
    for rows in grouped.values():
        ordered = sorted(
            rows,
            key=lambda event: (
                _MONEY_IN_PRECEDENCE_RANK.get(event.event_type, len(MONEY_IN_AMOUNT_PRECEDENCE)),
                int(event.id or 0),
            ),
        )
        amounts = [
            int(event.amount_cents) for event in ordered if event.amount_cents is not None
        ]
        groups.append(
            _PaymentGroup(
                amount_cents=amounts[0] if amounts else None,
                event_count=len(ordered),
                amount_conflict=len(set(amounts)) > 1,
            )
        )
    return groups


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
    * `in_30d` — the window's USD money IN, counted ONCE PER PAYMENT (DT-03): one card payment
      arrives as up to four Stripe deliveries (`charge.succeeded`, `payment_intent.succeeded`,
      `invoice.paid`, `invoice.payment_succeeded`) and the block groups them by the stored
      PaymentIntent id before summing, so $100.00 is $100.00 and not $200–$400.  An event with no
      PaymentIntent cannot be proven to repeat another, so it is counted exactly once on its own.
      No events at all -> UNMEASURED (the webhook may simply not be configured); USD events exist
      but none succeeded -> a measured 0; money-in events that all sit in another currency ->
      UNMEASURED (null); a USD payment whose amount no family stated -> UNMEASURED (null), because
      a partial sum is not a total (A2A7-02's rule, applied to the inflow).  A payment whose
      families state DIFFERENT amounts is counted once at the highest-precedence family's amount
      and the disagreement is named in `reasons["in_30d.amount_conflict"]`.
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

    # The inflow read (DT-03): every event in the window is telemetry ("did the webhook deliver?"),
    # but the MONEY is counted per PAYMENT, not per event family.  The rows are read and grouped by
    # the stored PaymentIntent id before summing (`_payment_groups`); exclusions are still counted
    # separately so a scoped total can never present itself as the whole.
    inflow_events = await fetch_scalars(
        db,
        select(StripeEvent)
        .where(StripeEvent.processed_at >= start, StripeEvent.processed_at < end)
        .order_by(StripeEvent.id),
        what="cash block stripe inflow",
    )
    events_count = len(inflow_events)
    money_in_events = [
        event for event in inflow_events if event.event_type in MONEY_IN_EVENT_TYPES
    ]
    usd_money_in = [
        event for event in money_in_events if _is_reporting_currency(event.currency)
    ]
    foreign_in_count = len(money_in_events) - len(usd_money_in)
    groups = _payment_groups(usd_money_in)
    unmeasured_payments = sum(1 for group in groups if group.amount_cents is None)
    conflicting_payments = sum(1 for group in groups if group.amount_conflict)

    in_30d: float | None = None
    if not events_count:
        reasons["in_30d"] = (
            f"no Stripe events in the last {CASH_WINDOW_DAYS} days — inflow is unmeasured "
            "(check that the webhook is configured)"
        )
    elif not groups and foreign_in_count:
        reasons["in_30d"] = (
            f"the window's {foreign_in_count} money-in Stripe event(s) are in another currency "
            f"than {REPORTING_CURRENCY} — the USD inflow is unmeasured (nothing was converted)"
        )
    elif unmeasured_payments:
        reasons["in_30d"] = (
            f"{unmeasured_payments} of the window's {len(groups)} USD money-in payment(s) "
            "carry no measured amount — the USD inflow cannot be summed (a partial sum is not a "
            "total, and $0.00 would be a measurement nobody made)"
        )
    else:
        # A USD ledger with events and no USD money-in is a MEASURED zero, unlike the
        # no-telemetry case above.  `unmeasured_payments == 0` here, so every group states one.
        total_cents = 0
        for group in groups:
            if group.amount_cents is None:  # pragma: no cover - excluded by the branch above
                continue
            total_cents += group.amount_cents
        in_30d = round(total_cents / 100, 2)
        if foreign_in_count:
            reasons["in_30d.currency"] = (
                f"{foreign_in_count} money-in Stripe event(s) in another currency were EXCLUDED "
                f"from the USD inflow (nothing was converted)"
            )
        if conflicting_payments:
            reasons["in_30d.amount_conflict"] = (
                f"{conflicting_payments} payment(s) were reported with different amounts by "
                f"different Stripe event families; each is counted ONCE at the amount stated by "
                f"the first family in {', '.join(MONEY_IN_AMOUNT_PRECEDENCE)} — the disagreement "
                f"is named here, never added up"
            )
        collapsed_events = len(usd_money_in) - len(groups)
        if collapsed_events:
            reasons["in_30d.dedupe"] = (
                f"{collapsed_events} of the window's {len(usd_money_in)} USD money-in Stripe "
                f"event(s) repeat another event's payment (same payment_intent) and were counted "
                f"ONCE: the inflow is {len(groups)} payment(s), not {len(usd_money_in)} event(s)"
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


# ── R-W10-1: the report cycle's real state (rows in, one named state out) ─────────────────────
# The two ledger actions the daily cycle writes (`celery_app/tasks/daily_cycle.py`, module
# constants `CYCLE_CLAIM_ACTION` / `CYCLE_SKIP_ACTION`).  Repeated here as literals on purpose:
# importing that module from the API process would pull Celery in.  `test_w3_report_cycle_state`
# pins these against the writer's own constants so the two can never drift silently.
REPORT_CYCLE_CLAIM_ACTION = "cycle_claimed"
REPORT_CYCLE_SKIP_ACTION = "cycle_tick_skipped"
#: The cycle that WRITES the report (`_evening_cycle` → `save_evening_summary`).  The morning
#: cycle creates the row via `save_morning_plan`, so its claim cannot prove a report exists.
REPORT_EVENING_CYCLE = "evening"

#: The closed vocabulary `DashboardReportCycle.state` may carry.  `unknown` is a real answer,
#: never a rolled-up guess (see `derive_report_cycle`).
REPORT_CYCLE_STATES: tuple[str, ...] = (
    "unknown",
    "never_triggered",
    "not_run_today",
    "report_running",
    "report_written_today",
    "report_missing",
)

#: Why a next-run time cannot be served: the schedule lives in a beat process the API cannot
#: see.  Served verbatim as `report_cycle.scheduler_reason`.
REPORT_CYCLE_SCHEDULER_REASON = (
    "the report cycle is a Celery beat entry in a separate process; the API cannot observe beat "
    "(it may be stopped, profile-gated or crashing), so no next-run time is claimed. 'cycle "
    "started' is a committed activity_log cycle_claimed row and 'report written' is a "
    "daily_reports row carrying an evening_summary — the state is derived from those rows only."
)


def _claim_day(value: str | None) -> date | None:
    """Parse a claim's `detail.day` (company-local ISO date); junk is None, never an exception."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def derive_report_cycle(
    *,
    today: date,
    last_claim_at: datetime | None = None,
    last_claim_cycle: str | None = None,
    last_claim_day: str | None = None,
    last_evening_claim_day: str | None = None,
    last_completed_report_date: date | None = None,
    last_skip_reason: str | None = None,
    claims_retained_since: datetime | None = None,
) -> dict[str, Any]:
    """Derive the report cycle's state from the two ledgers; never from a schedule or a guess.

    Returns the exact kwargs of `schemas.report.DashboardReportCycle`.  The inputs are rows the
    dashboard statement already reads (`activity_log` claim/skip rows and `daily_reports` rows
    carrying an evening summary); this function is pure, so every state is testable without a
    database.

    Every branch names its evidence in `state_reason`; an input that cannot decide a state yields
    `unknown` with the reason, NEVER a guess.  In particular a future-dated claim (clock or
    day-attribution fault) is `unknown`, not `report_running`.
    """
    evening_day = _claim_day(last_evening_claim_day)
    completed = last_completed_report_date
    state, reason = _report_cycle_state(
        today=today,
        evening_raw=last_evening_claim_day,
        evening_day=evening_day,
        completed=completed,
        any_claim=last_claim_at is not None,
    )
    return {
        "state": state,
        "state_reason": reason,
        "last_claim_at": last_claim_at,
        "last_claim_cycle": last_claim_cycle,
        "last_claim_day": last_claim_day,
        "last_evening_claim_day": last_evening_claim_day,
        "last_completed_report_date": completed,
        "last_skip_reason": last_skip_reason,
        "claims_retained_since": claims_retained_since,
        "scheduler_observable": False,
        "scheduler_reason": REPORT_CYCLE_SCHEDULER_REASON,
    }


def _report_cycle_state(
    *,
    today: date,
    evening_raw: str | None,
    evening_day: date | None,
    completed: date | None,
    any_claim: bool,
) -> tuple[str, str]:
    """`(state, reason)` — the comparison in one place, so no route can invent its own state."""
    if evening_day is None:
        if evening_raw:
            return (
                "unknown",
                f"the newest report-cycle claim carries an unparsable day {evening_raw!r} — "
                "the state cannot be decided from it",
            )
        if completed is None and not any_claim:
            return (
                "never_triggered",
                "no cycle claim is retained and no daily report has ever carried an evening "
                "summary — nothing has run on this deployment",
            )
        if completed is None:
            return (
                "unknown",
                "a cycle has claimed but the report (evening) cycle never has in the retained "
                "activity window, and no daily report carries an evening summary",
            )
        return (
            "unknown",
            "daily reports carry an evening summary but no report-cycle claim is retained — "
            "the claim ledger cannot explain them",
        )
    if evening_day > today:
        return (
            "unknown",
            f"the newest report-cycle claim is dated {evening_day.isoformat()}, after today "
            f"({today.isoformat()}) — a clock or day-attribution fault, not a cycle state",
        )
    if evening_day == today:
        if completed == today:
            return (
                "report_written_today",
                f"the report cycle claimed {today.isoformat()} and that day's report carries "
                "an evening summary",
            )
        return (
            "report_running",
            f"the report cycle claimed {today.isoformat()} and no evening summary exists yet",
        )
    if completed is None or completed < evening_day:
        return (
            "report_missing",
            f"the report cycle claimed {evening_day.isoformat()} and no daily report for that "
            "day carries an evening summary — it started and did not finish",
        )
    return (
        "not_run_today",
        f"the newest report-cycle claim is {evening_day.isoformat()} and its report was "
        "written; no cycle has started since",
    )


__all__ = [
    "REPORT_CYCLE_CLAIM_ACTION",
    "REPORT_CYCLE_SCHEDULER_REASON",
    "REPORT_CYCLE_SKIP_ACTION",
    "REPORT_CYCLE_STATES",
    "REPORT_EVENING_CYCLE",
    "build_cash_block",
    "derive_report_cycle",
    "get_daily_report",
    "get_latest_daily_report",
    "get_or_create_daily_report",
    "save_evening_summary",
    "save_morning_plan",
]
