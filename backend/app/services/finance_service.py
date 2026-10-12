"""Finance reads/writes (the routes and the finance agent call these).

Three measured problems this module fixes:

* **Round trips.**  The summary was three queries (latest snapshot, month expenses,
  all-time ad spend) plus the caller's own; it is now ONE statement whose scalar
  subqueries read unique-index seeks and one bounded range sum
  (`DATA-PERF.md` §2.5, 4 → 1 round trips).
* **Money as binary floats.**  `sum()` over a `Float` column is order-dependent
  (measured: 30 × 33.33 = 999.9000000000004 against a 999.90 budget), so every
  USD value crossing this service boundary is read into `Decimal` — a spend cap
  compared as a float can trip the wrong way.
* **The webhook race.**  `record_stripe_event` used SELECT-then-INSERT, which two
  concurrent duplicate deliveries lose with `IntegrityError: UNIQUE constraint
  failed` → 5xx → Stripe retries harder (reproduced in lane 06, probe_04 §B).  It
  is now ONE statement: `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING
  RETURNING id`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, literal, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import dialect_name
from app.models.ads import AdCampaign
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.services.query_helpers import db_refusal, fetch_scalars

ZERO = Decimal("0")


def money(value: Any) -> Decimal:
    """A USD value as `Decimal`, never a binary float.

    `Decimal(str(x))` — not `Decimal(x)` — because the float has already lost the
    decimal value by the time it is a float (`Decimal(0.1)` is
    `0.1000000000000000055511151231257827`).
    """
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _month_bounds(month: date | None) -> tuple[date, date]:
    """[first of month, first of next month) — half-open, so the range is bounded.

    The original had no upper bound (`date >= month_start`), which counts a
    future-dated row into "this month" and can never be a bounded index range.
    """
    start = (month or date.today()).replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


async def get_latest_revenue_snapshot(db: AsyncSession) -> RevenueSnapshot | None:
    result = await db.execute(
        select(RevenueSnapshot)
        .order_by(RevenueSnapshot.snapshot_date.desc(), RevenueSnapshot.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_revenue_snapshots(db: AsyncSession, limit: int = 30) -> list[RevenueSnapshot]:
    return await fetch_scalars(
        db,
        select(RevenueSnapshot)
        .order_by(RevenueSnapshot.snapshot_date.desc(), RevenueSnapshot.id.desc())
        .limit(limit),
        what="revenue snapshots",
    )


async def list_expenses(
    db: AsyncSession,
    category: str | None = None,
    limit: int = 100,
) -> list[ExpenseRecord]:
    stmt = select(ExpenseRecord)
    if category is not None:
        stmt = stmt.where(ExpenseRecord.category == category)
    stmt = stmt.order_by(ExpenseRecord.date.desc(), ExpenseRecord.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="expenses")


async def list_stripe_events(db: AsyncSession, limit: int = 100) -> list[StripeEvent]:
    return await fetch_scalars(
        db,
        select(StripeEvent).order_by(StripeEvent.id.desc()).limit(limit),
        what="stripe events",
    )


async def get_finance_summary(db: AsyncSession, month: date | None = None) -> dict[str, Any]:
    """Every headline scalar in ONE statement.  Empty database ⇒ zeros + None date.

    Shape: the seven keys `GET /finance/summary` and `dashboard.summary` consume.
    `total_ad_spend_usd` is a `Decimal` here and becomes the float the wire format
    declares at the response-model boundary.
    """
    month_start, month_end = _month_bounds(month)

    latest = (
        select(RevenueSnapshot)
        .order_by(RevenueSnapshot.snapshot_date.desc(), RevenueSnapshot.id.desc())
        .limit(1)
    ).subquery("snap")
    # A one-row anchor LEFT JOINed to the snapshot keeps the all-zero contract when
    # the table is empty: the JOIN is what the shipped test's "no snapshot yet"
    # case needs, and a Python-side default dict would be an unstated second shape.
    anchor = select(literal(1).label("one")).subquery("one")
    expenses = (
        select(func.coalesce(func.sum(ExpenseRecord.amount_cents), 0))
        .where(ExpenseRecord.date >= month_start, ExpenseRecord.date < month_end)
        .scalar_subquery()
    )
    ad_spend = (
        select(func.coalesce(func.sum(AdCampaign.total_spent_usd), 0.0)).scalar_subquery()
    )
    stmt = (
        select(
            latest.c.mrr_cents,
            latest.c.arr_cents,
            latest.c.active_subscribers,
            latest.c.total_revenue_month_cents,
            latest.c.stripe_balance_cents,
            latest.c.snapshot_date.label("last_snapshot_date"),
            expenses.label("total_expenses_month_cents"),
            ad_spend.label("total_ad_spend_usd"),
        )
        .select_from(anchor.outerjoin(latest, true()))
    )
    try:
        row = (await db.execute(stmt)).one()
    except Exception as exc:  # noqa: BLE001 - translated into a named refusal below
        raise db_refusal("finance summary", exc) from exc

    return {
        "mrr_cents": int(row.mrr_cents or 0),
        "arr_cents": int(row.arr_cents or 0),
        "active_subscribers": int(row.active_subscribers or 0),
        "stripe_balance_cents": int(row.stripe_balance_cents or 0),
        "total_revenue_month_cents": int(row.total_revenue_month_cents or 0),
        "total_expenses_month_cents": int(row.total_expenses_month_cents or 0),
        "total_ad_spend_usd": money(row.total_ad_spend_usd),
        "last_snapshot_date": row.last_snapshot_date,
    }


async def upsert_revenue_snapshot(
    db: AsyncSession,
    snapshot_date: date,
    **values: Any,
) -> RevenueSnapshot:
    """Update-or-insert on the unique `snapshot_date` in ONE statement.

    The finance agent runs this daily; a retried run must update the same row, not
    raise `IntegrityError` on the unique index.
    """
    if not values:
        existing = (
            await db.execute(
                select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == snapshot_date)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        values = {}

    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    base = insert_fn(RevenueSnapshot).values(snapshot_date=snapshot_date, **values)
    if values:
        stmt = base.on_conflict_do_update(
            index_elements=["snapshot_date"], set_={k: insert_fn(RevenueSnapshot).excluded[k] for k in values}
        ).returning(RevenueSnapshot)
    else:
        stmt = base.on_conflict_do_nothing(index_elements=["snapshot_date"]).returning(
            RevenueSnapshot
        )
    # populate_existing: without it, a session that already holds this snapshot in its
    # identity map gets its STALE copy back from `returning()` even though the row was
    # updated (measured: the DB said mrr=250 while the returned object still said 100).
    stmt = stmt.execution_options(synchronize_session=False, populate_existing=True)
    snapshot = (await db.execute(stmt)).scalars().one_or_none()
    if snapshot is None:
        raise PolsiaError(
            E_DB,
            f"revenue snapshot for {snapshot_date} could not be written or re-read",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"snapshot_date": snapshot_date.isoformat()},
        )
    return snapshot


async def record_expense(db: AsyncSession, **values: Any) -> ExpenseRecord:
    amount = values.get("amount_cents")
    if amount is None or int(amount) <= 0:
        raise PolsiaError(
            E_VALIDATION,
            f"expense amount_cents must be a positive integer, got {amount!r}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"amount_cents": amount},
        )
    expense = ExpenseRecord(**values)
    db.add(expense)
    await db.flush()
    return expense


async def record_stripe_event(
    db: AsyncSession,
    stripe_event_id: str,
    event_type: str,
    raw_payload: dict[str, Any] | None = None,
    customer_id: str | None = None,
    amount_cents: int | None = None,
    currency: str | None = None,
    status: str = "processed",
) -> StripeEvent | None:
    """Persist a Stripe event EXACTLY once.  Returns the row (new or existing).

    ONE statement — `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING
    id` — so a concurrent duplicate replay cannot raise `IntegrityError`, which
    would surface as a 5xx and trigger Stripe's retry storm.  When the insert
    conflicts, the existing row is re-read and returned (idempotent success, not a
    fake new row).
    """
    if not stripe_event_id:
        raise PolsiaError(
            E_VALIDATION,
            "stripe_event_id is empty: the event cannot be de-duplicated",
            how_to_fix="reject events without an id at the webhook boundary",
        )
    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(StripeEvent)
        .values(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=(currency.lower() if currency else None),
            status=status,
            raw_payload=raw_payload,
        )
        .on_conflict_do_nothing(index_elements=["stripe_event_id"])
        .returning(StripeEvent)
    )
    created = (await db.execute(stmt)).scalars().one_or_none()
    if created is not None:
        return created
    # Replay: return the row that already exists.  `populate_existing` so a session
    # holding an older copy of that event cannot hand back stale fields.
    return (
        (
            await db.execute(
                select(StripeEvent)
                .where(StripeEvent.stripe_event_id == stripe_event_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    )


__all__ = [
    "get_finance_summary",
    "get_latest_revenue_snapshot",
    "list_expenses",
    "list_revenue_snapshots",
    "list_stripe_events",
    "money",
    "record_expense",
    "record_stripe_event",
    "upsert_revenue_snapshot",
]
