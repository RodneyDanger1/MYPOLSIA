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

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, literal, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.accounting import (
    budget_policy_from_settings,
    burn_rate,
    spend_summary,
    unit_economics,
)
from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import day_bounds, day_window, dialect_name, now_utc, today_in
from app.models.ads import AdCampaign
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.task import AgentRun
from app.services.query_helpers import db_refusal, fetch_scalars


def money(value: Any) -> Decimal | None:
    """A USD value as `Decimal`, never a binary float.  `None` stays `None`.

    `None` means "not measured" (an aggregate over zero rows, an absent column);
    returning `Decimal("0")` for it would collapse it into a measured zero — the
    exact state confusion this module refuses — so absence passes through.

    `Decimal(str(x))` — not `Decimal(x)` — because the float has already lost the
    decimal value by the time it is a float (`Decimal(0.1)` is
    `0.1000000000000000055511151231257827`).
    """
    if value is None:
        return None
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
    """Every headline scalar in ONE statement.  Nothing measured ⇒ `None`, never 0.

    Shape: the seven keys `GET /finance/summary` and `dashboard.summary` consume.
    Every money field is `int`/`Decimal` when measured and `None` when not — no
    `or 0` and no `coalesce(..., 0)` may collapse "not measured" into a fake zero on
    this path (the response model and the frontend both keep the two states apart).
    `total_ad_spend_usd` is a `Decimal` (or `None`) here and becomes the float the
    wire format declares at the response-model boundary.
    """
    month_start, month_end = _month_bounds(month)

    latest = (
        select(RevenueSnapshot)
        .order_by(RevenueSnapshot.snapshot_date.desc(), RevenueSnapshot.id.desc())
        .limit(1)
    ).subquery("snap")
    # A one-row anchor LEFT JOINed to the snapshot keeps the one-row contract when the
    # table is empty: the row's money columns come back NULL ("not measured"), which is
    # what the "no snapshot yet" case needs, and a Python-side default dict would be an
    # unstated second shape.
    anchor = select(literal(1).label("one")).subquery("one")
    expenses = (
        # No coalesce here: SUM over zero rows is NULL ("nothing measured"), and a 0
        # would be an invented reading — the same collapse the `or 0` on the return
        # dict below used to ship.
        select(func.sum(ExpenseRecord.amount_cents))
        .where(ExpenseRecord.date >= month_start, ExpenseRecord.date < month_end)
        .scalar_subquery()
    )
    ad_spend = select(func.sum(AdCampaign.total_spent_usd)).scalar_subquery()
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

    # Explicit `is not None` per field: `value or 0` cannot tell a measured 0 from an
    # absent measurement — the exact collapse this path used to ship.
    return {
        "mrr_cents": int(row.mrr_cents) if row.mrr_cents is not None else None,
        "arr_cents": int(row.arr_cents) if row.arr_cents is not None else None,
        "active_subscribers": (
            int(row.active_subscribers) if row.active_subscribers is not None else None
        ),
        "stripe_balance_cents": (
            int(row.stripe_balance_cents) if row.stripe_balance_cents is not None else None
        ),
        "total_revenue_month_cents": (
            int(row.total_revenue_month_cents) if row.total_revenue_month_cents is not None else None
        ),
        "total_expenses_month_cents": (
            int(row.total_expenses_month_cents) if row.total_expenses_month_cents is not None else None
        ),
        "total_ad_spend_usd": money(row.total_ad_spend_usd),
        "last_snapshot_date": row.last_snapshot_date,
    }


async def get_ai_spend_summary(db: AsyncSession) -> dict[str, Any]:
    """The additive `ai_spend` block of `GET /finance/summary`: the day's AI spend, measured.

    CONSUMER (R24).  `app/agents/accounting.py` carries three public analytics functions that had
    no production caller at all — `spend_summary` (:276), `burn_rate` (:292) and `unit_economics`
    (:310); the deleted `base_agent` budget block (base_agent.py:193-195) is their whole history.
    This is the wire: the cap comes from `budget_policy_from_settings` (accounting.py:212-221) —
    the SAME call the dispatch gate makes (`celery_app/tasks/_common.py:258`) — and the spend is
    the measured quantity the gate gates on (`agent_runs.cost_usd`, company-local day).

    MEASURED-SPEND DISCIPLINE (D3; `_common.day_spend_by_agent` + test_budget_gate):

    * ABSENT day (no `agent_runs` rows in the window) -> a measured `0.0`: the ledger genuinely has
      nothing, the same state the gate calls a fresh day;
    * rows exist AND at least one `cost_usd` was reported -> the measured sum (a reported `0` is a
      measured zero and stays `0.0`);
    * rows exist with NO measured cost anywhere (every column NULL: a run whose CLI never reported
      one, or one still running — the column staying NULL is documented at accounting.py:20-23) ->
      UNMEASURED: `spent`/`remaining` are None, so the wire can never render `$0.00` for a day
      nobody measured (F-08/F-10).

    The window is `timeutil.day_window` — half-open `[start, end)` and dialect-bound.  That is
    STRICTER than the gate's (`>= start` only): a future-dated `started_at` must not be counted
    into today, the same correction `_month_bounds` records for the month window.

    `unit_economics` is fed ONLY the agents whose day sum is measured.  It collapses a missing
    spend entry into `0.0` (accounting.py:320), so an agent with completions but an unmeasured
    spend would otherwise be priced at a fabricated `$0.00` per task; instead it is NAMED in
    `unmeasured_spend_agents` — outside the priced set, never silently dropped.  `burn_rate` is
    None when `spent` is None: a projection from an unmeasured spend is fabricated precision.

    Exactness: the cap and the spend reach `spend_summary` as `Decimal` (this module's money rule,
    see the module docstring), so `remaining` is `0.3`, not `0.3000000000000007`; the response
    model floats the values at the wire boundary only.  `unit_economics` takes `float`s (its own
    documented `dict[str, float]` contract).
    """
    start, end = day_window(db)

    # ONE grouped statement: the day's spend AND the completed-run count per agent, from the same
    # rows — a run's cost and its status live on the row that carries both, so they cannot drift
    # apart.  `sum(case(...))` (not `count(*) filter`) is spelled the same way on both engines.
    grouped = (
        select(
            AgentRun.agent_type,
            func.sum(AgentRun.cost_usd).label("spent"),
            func.sum(case((AgentRun.status == "completed", 1), else_=0)).label("completed"),
        )
        .where(AgentRun.started_at >= start, AgentRun.started_at < end)
        .group_by(AgentRun.agent_type)
    )
    try:
        rows = (await db.execute(grouped)).all()
    except SQLAlchemyError as exc:
        raise db_refusal("ai spend analytics", exc) from exc

    cap = money(budget_policy_from_settings().day_limit_usd)
    assert cap is not None  # `money` only passes a None through; the settings cap is a float
    spend_by_agent: dict[str, Decimal] = {}
    completed_by_agent: dict[str, int] = {}
    unmeasured_spend_agents: list[str] = []
    for agent, spent, completed in rows:
        name = str(agent)
        if spent is None:  # rows, no reported cost: NOT a measured 0 (F-08/F-10)
            unmeasured_spend_agents.append(name)
            continue
        measured = money(spent)
        assert measured is not None  # guarded above: `money` only passes a None through
        spend_by_agent[name] = measured
        completed_by_agent[name] = int(completed)

    if not rows:                                    # absent day: measured zero (D3)
        day_spent: Decimal | None = Decimal("0")
    elif spend_by_agent:                            # at least one run reported its cost
        day_spent = sum(spend_by_agent.values(), Decimal("0"))
    else:                                           # runs exist, none reported a cost
        day_spent = None

    # `spend_summary` is `float`-typed but arithmetically exact on Decimals (is-None guard, then
    # `cap - spent`): passing Decimal here is what keeps `remaining` exact, hence the one ignore.
    view = spend_summary(day_spent, cap)  # type: ignore[arg-type]

    # Elapsed fraction from the REAL local-day bounds — a DST day is 23/25h, so
    # `seconds/86400` would mis-project it.
    local_start, local_end = day_bounds(today_in())
    elapsed_fraction = (
        (now_utc() - local_start).total_seconds() / (local_end - local_start).total_seconds()
    )

    block: dict[str, Any] = dict(view)
    block["burn_rate"] = (
        None
        if day_spent is None
        else burn_rate(
            spilled_usd=float(day_spent),
            day_limit_usd=float(cap),
            elapsed_fraction=elapsed_fraction,
        )
    )
    economics: dict[str, Any] = unit_economics(
        {name: float(total) for name, total in spend_by_agent.items()}, completed_by_agent
    )
    # Additive honesty marker, not part of `unit_economics` itself: the totals above cover exactly
    # the agents in `per_agent`, and this names the agents that are outside them because their
    # day spend was never measured (they are NOT $0.00/task — they are unpriced).
    economics["unmeasured_spend_agents"] = sorted(unmeasured_spend_agents)
    block["unit_economics"] = economics
    return block


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


#: `revenue_snapshots` columns `persist_finance_run` may write — any other key in the
#: payload's snapshot dict is not a column and must not reach the insert.
_SNAPSHOT_COLUMNS = (
    "mrr_cents",
    "arr_cents",
    "active_subscribers",
    "churned_today",
    "new_today",
    "total_revenue_month_cents",
    "stripe_balance_cents",
)
#: `expense_records` columns a payload expense entry may supply (`date` handled separately).
_EXPENSE_COLUMNS = ("category", "vendor", "amount_cents", "currency", "description", "external_ref")


def _iso_date(value: Any) -> date | None:
    """An ISO date string (or `date`) as `date`; `None` when it is neither.

    Payloads carry ISO strings; a full datetime string maps to its date.  There is NO
    "today()" fallback: a guessed date on the UNIQUE-indexed snapshot row (or on an
    expense) is a fabricated measurement, so a missing date stays missing and the
    caller decides — skip the expense, refuse the snapshot.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _expense_kwargs(expense: Any) -> dict[str, Any]:
    """Map one `FinanceOutput.expenses[]` entry onto `ExpenseRecord` kwargs.

    Raises the `E_VALIDATION` refusal when the entry cannot become an honest row: it
    is not an object, or a NOT NULL column (category/vendor) or a usable date is
    missing.  `amount_cents` is left for `record_expense` to adjudicate by name.
    """
    if not isinstance(expense, dict):
        raise PolsiaError(
            E_VALIDATION,
            f"expense entry is not an object: {type(expense).__name__}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"expense": repr(expense)[:200]},
        )
    values = {key: expense[key] for key in _EXPENSE_COLUMNS if expense.get(key) is not None}
    missing = [key for key in ("category", "vendor") if not values.get(key)]
    parsed = _iso_date(expense.get("date"))
    if missing or parsed is None:
        detail = f"missing {missing}" if missing else f"unusable date {expense.get('date')!r}"
        raise PolsiaError(
            E_VALIDATION,
            f"expense entry cannot be recorded: {detail}",
            how_to_fix="each expense needs a category, a vendor and an ISO date; fix the agent output",
            context={"expense": {k: expense.get(k) for k in (*_EXPENSE_COLUMNS, "date")}},
        )
    values["date"] = parsed
    return values


async def persist_finance_run(db: AsyncSession, result: dict[str, Any]) -> dict[str, Any]:
    """Persist what a finished finance run ACTUALLY measured: the snapshot row + the expenses.

    This is the seam the queue worker calls after a finance run
    (`celery_app.tasks.agent_tasks._persist_run_artifacts`); before it existed,
    `revenue_snapshots` / `expense_records` had no writer at all and the summary could
    only ever read empty tables.  Idempotent by date: `upsert_revenue_snapshot` is an
    upsert on the UNIQUE `snapshot_date`, so a retried run updates the same row instead
    of duplicating it.

    Contract: NEVER raises — every failure is returned as the `error` field (a named
    `{"code", "message", ...}` refusal).  The worker owns the transaction, so this
    flushes only and never commits.  An expense the service refuses (`amount_cents`
    missing or <= 0, or a row that cannot be formed at all) is counted in
    `skipped_invalid` — never silently dropped, never fake-written.

    Returns `{"ok": True, "snapshot_written": bool, "expenses_written": int,
    "skipped_invalid": int}` — `snapshot_written` is True only when the upsert really
    returned a row, which is the fact the worker reports as the run's `snapshot_written`.
    """
    snapshot_written = False
    expenses_written = 0
    skipped_invalid = 0
    try:
        snapshot = result.get("snapshot")
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                raise PolsiaError(
                    E_VALIDATION,
                    f"snapshot is not an object: {type(snapshot).__name__}",
                    how_to_fix="the finance agent must provide a snapshot object or null",
                    context={"snapshot": repr(snapshot)[:200]},
                )
            snapshot_date = _iso_date(snapshot.get("snapshot_date"))
            if snapshot_date is None:
                raise PolsiaError(
                    E_VALIDATION,
                    f"snapshot has no usable snapshot_date: {snapshot.get('snapshot_date')!r}",
                    how_to_fix=(
                        "the agent must provide snapshot_date = YYYY-MM-DD (UTC); a guessed "
                        "date would mis-key the UNIQUE row"
                    ),
                    context={"snapshot_date": snapshot.get("snapshot_date")},
                )
            values = {
                key: snapshot[key] for key in _SNAPSHOT_COLUMNS if snapshot.get(key) is not None
            }
            row = await upsert_revenue_snapshot(db, snapshot_date=snapshot_date, **values)
            snapshot_written = row is not None
        for expense in result.get("expenses") or []:
            try:
                await record_expense(db, **_expense_kwargs(expense))
                expenses_written += 1
            except PolsiaError as exc:
                if exc.code != E_VALIDATION:
                    raise
                # `record_expense` refuses `amount_cents<=0` by name (and an entry that
                # cannot form a row refuses the same way): one unusable draft must not
                # sink the run's good rows — and must not be invented into a write either.
                skipped_invalid += 1
        await db.flush()  # flush only: the worker owns the commit
        return {
            "ok": True,
            "snapshot_written": snapshot_written,
            "expenses_written": expenses_written,
            "skipped_invalid": skipped_invalid,
        }
    except Exception as exc:  # noqa: BLE001 - the documented contract is "never raises"
        if isinstance(exc, PolsiaError):
            error: dict[str, Any] = {
                "code": exc.code,
                "message": exc.message,
                "how_to_fix": exc.how_to_fix,
            }
        elif isinstance(exc, SQLAlchemyError):
            error = {"code": E_DB, "message": f"finance persist hit a database error: {exc}"}
        else:
            error = {
                "code": E_VALIDATION,
                "message": f"finance persist rejected the run result: {type(exc).__name__}: {exc}",
            }
        return {
            "ok": False,
            "snapshot_written": snapshot_written,
            "expenses_written": expenses_written,
            "skipped_invalid": skipped_invalid,
            "error": error,
        }


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
    "get_ai_spend_summary",
    "get_finance_summary",
    "get_latest_revenue_snapshot",
    "list_expenses",
    "list_revenue_snapshots",
    "list_stripe_events",
    "money",
    "persist_finance_run",
    "record_expense",
    "record_stripe_event",
    "upsert_revenue_snapshot",
]
