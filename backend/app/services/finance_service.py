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

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, literal, select, true
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
from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, MeridianError
from app.core.timeutil import day_bounds, day_window, dialect_name, now_utc, today_in, today_utc
from app.models.ads import AdCampaign
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.invoice import Invoice, InvoiceStatus
from app.models.task import AgentRun
from app.services.activity_service import log_activity
from app.services.query_helpers import db_refusal, fetch_scalars

logger = logging.getLogger(__name__)


def money(value: Any) -> Decimal | None:
    """A USD value as `Decimal`, never a binary float.  `None` stays `None`.

    `None` means "not measured" (an aggregate over zero rows, an absent column);
    returning `Decimal("0")` for it would collapse it into a measured zero — the
    exact state confusion this module refuses — so absence passes through.

    `Decimal(str(x))` — not `Decimal(x)` — because the float has already lost the
    decimal value by the time it is a float (`Decimal(0.1)` is
    `0.1000000000000000055511151231257827`).

    A NON-FINITE value (`NaN` / `±Infinity`, however spelled) is not a measurement and is
    refused BY NAME (S4-F1, wave-v13/s4-errors F1): `Decimal(str(float("nan")))` is
    `Decimal("NaN")`, which the invoice create route used to PERSIST and then 500 on at
    response render — poisoning every later collection read.  This is the service-layer
    belt; the wire models also reject non-finite floats (`allow_inf_nan=False`) before
    they get here.
    """
    if value is None:
        return None
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise MeridianError(
            E_VALIDATION,
            f"money value {value!r} is not finite (NaN/Infinity are not measurements)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"value": repr(value)},
        )
    return result


#: Stripe event types whose `data.object` is a failed payment.  The finance agent's
#: failed-payment reader (`recent_failed_payments`) looks at exactly these; a delivery of any
#: other type is still recorded, it just is not a "failed payment" record.
FAILED_PAYMENT_EVENT_TYPES: tuple[str, ...] = (
    "charge.failed",
    "invoice.payment_failed",
    "payment_intent.payment_failed",
)


def extract_payment_intent_id(event: Any) -> str | None:
    """The Stripe PaymentIntent id (`pi_…`) a refund for this delivery would need — or `None`.

    Shape-driven, because that is all the delivery carries (Stripe's own object reference,
    fetched 2026-10-13):

    * a `charge` object carries *"payment_intent … ID of the PaymentIntent associated with this
      charge, **if one exists**"* — so its absence is legal and answers `None` (never the charge
      id: `ch_…` is not a refund target for `integrations.refund_payment`, which refuses any
      non-`pi_` id BY NAME);
    * an `invoice` object carries *"payment_intent … The PaymentIntent associated with this
      invoice"* (null until the invoice is finalised) — same rule;
    * a `payment_intent` object IS the PaymentIntent: its own `id` is the answer (detected first
      by `object == "payment_intent"`, and by the `pi_` prefix as a secondary signal).

    Anything else — a non-dict payload, a missing `data.object`, an unrelated object — is `None`.
    Nothing is fabricated or coerced from another id type; `None` is the honest "not measured"
    the reader and the agent's refusal both expect.
    """
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    data_object = data.get("object") if isinstance(data, dict) else None
    if not isinstance(data_object, dict):
        return None
    candidate = data_object.get("payment_intent")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    if data_object.get("object") == "payment_intent":
        own_id = data_object.get("id")
        if isinstance(own_id, str) and own_id.strip():
            return own_id.strip()
    return None


def _month_bounds(month: date | None) -> tuple[date, date]:
    """[first of month, first of next month) — half-open, so the range is bounded.

    The original had no upper bound (`date >= month_start`), which counts a
    future-dated row into "this month" and can never be a bounded index range.

    The DEFAULT month is the COMPANY-local one (`today_in`), never the process clock:
    for a non-UTC `COMPANY_TIMEZONE` the process date can sit in a different calendar
    month for |UTC offset| hours around every boundary, so reading the process-local
    date made the "Expenses (Month)" total count the wrong month — the same company-local
    rule `get_ai_spend_summary` applies to its day window below (L2-01; measured RED with
    the process clock at 2026-10-31 and `Asia/Tokyo`).
    """
    start = (month or today_in()).replace(day=1)
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


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


#: The one currency every reported total is denominated in (A2A7-03 / A8-04).  A row in another
#: currency is NOT converted here: `fx_service` can convert, but a converted total needs a stored
#: rate and a rate date, which is an owner decision.  Until then the row is EXCLUDED and COUNTED,
#: so no payload can present a mixed sum as exact dollars.
REPORTING_CURRENCY = "usd"


def reporting_currency_filter(column):
    """The single currency predicate for every money sum that reaches the wire (A2A7-03).

    `finance_service._normalize_currency` is the one normaliser on the WRITE side; this is its
    read-side counterpart — every sum over `expense_records.amount_cents` or
    `stripe_events.amount_cents` goes through this predicate instead of re-spelling the rule.

    A NULL/blank currency is NOT usd: it is what a raw/ops writer leaves behind, i.e. "unknown",
    and summing it as dollars would present an unstated unit as a measurement.  It is excluded
    here and named by the caller's count (`foreign_*_rows`).
    """
    return func.lower(func.coalesce(column, "")) == REPORTING_CURRENCY


def excluded_currency_filter(column):
    """The complement reader: the rows a total LEFT OUT because their currency is not the reporting one."""
    return func.lower(func.coalesce(column, "")) != REPORTING_CURRENCY


async def get_finance_summary(db: AsyncSession, month: date | None = None) -> dict[str, Any]:
    """Every headline scalar in ONE statement.  Nothing measured ⇒ `None`, never 0.

    Shape: the seven pre-A2A7-03 keys `GET /finance/summary` and `dashboard.summary` consume,
    plus `foreign_expenses_month_count` — the number of expense rows in the month that were
    EXCLUDED from `total_expenses_month_cents` because their currency is not
    `REPORTING_CURRENCY` (A2A7-03).  The exclusion is counted, never silent, and nothing is
    converted: a converted total needs a stored rate and rate date (an owner decision).

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
        # dict below used to ship.  A2A7-03: USD-only, and the excluded rows are counted
        # beside it so the payload says the total is scoped.
        select(func.sum(ExpenseRecord.amount_cents))
        .where(
            ExpenseRecord.date >= month_start,
            ExpenseRecord.date < month_end,
            reporting_currency_filter(ExpenseRecord.currency),
        )
        .scalar_subquery()
    )
    foreign_expenses = (
        select(func.count())
        .where(
            ExpenseRecord.date >= month_start,
            ExpenseRecord.date < month_end,
            excluded_currency_filter(ExpenseRecord.currency),
        )
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
            foreign_expenses.label("foreign_expenses_month_count"),
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
        "foreign_expenses_month_count": int(row.foreign_expenses_month_count or 0),
        "total_ad_spend_usd": money(row.total_ad_spend_usd),
        "last_snapshot_date": row.last_snapshot_date,
    }


@dataclass(frozen=True)
class SpendTruth:
    """What the day's ledger can say, with the three states kept apart (A1-03 / A8-02 / F-19).

    `total is None` = UNKNOWN: at least one run that ENDED never wrote a cost, so no cap can be
    enforced from this number (never a measured `$0.00`).  `finished_unpriced` is that count;
    `in_flight` is the runs still open — their cost is bounded by the per-run cap, not guessed
    into the total — and an ABSENT day is a measured `Decimal("0")`.
    """

    total: Decimal | None
    per_agent: dict[str, Decimal]
    finished_unpriced: int
    in_flight: int
    measured_runs: int


async def day_spend_truth(db: AsyncSession) -> SpendTruth:
    """MEASURED AI spend for the company-local day, with the unmeasured rows COUNTED (A1-03).

    The defect this replaces: `coalesce(sum(agent_runs.cost_usd), 0)` (`_common.day_spend_by_agent`)
    cannot tell an ABSENT day from a day whose finished runs never wrote a cost, and `SUM` ignores
    the NULLs — so an unpriced served-model alias (F-19) or a failed attempt whose measurement was
    dropped (F-20) read as `$0.00` and the day never filled up.

    Three states, stated:
    * `total` is a MEASURED `Decimal` when every finished run wrote a cost (a reported `0` is a
      measured zero and counts);
    * `total is None` when at least one finished run has no cost — `finished_unpriced` says how
      many, and the caller REFUSES rather than spending against an invented zero;
    * an open run (`ended_at IS NULL`) is NOT lost accounting: it is counted in `in_flight` and
      its cost is bounded by the policy's per-run cap, so it neither refuses a gate nor adds a
      fabricated number to `total`.

    The window is `timeutil.day_window` — half-open `[start, end)` and dialect-bound, the same
    window `get_ai_spend_summary` reads (a future-dated `started_at` cannot inflate today).  An
    agent with NO measured cost is ABSENT from `per_agent`: a `Decimal("0")` entry would be the
    same fabricated zero one level down (`unmeasured_spend_agents` below is the precedent).
    """
    start, end = day_window(db)
    finished_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_not(None))
    open_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_(None))
    grouped = (
        select(
            AgentRun.agent_type,
            func.sum(AgentRun.cost_usd).label("measured"),
            func.count().label("runs"),
            func.sum(case((finished_unpriced, 1), else_=0)).label("finished_unpriced"),
            func.sum(case((open_unpriced, 1), else_=0)).label("in_flight"),
        )
        .where(AgentRun.started_at >= start, AgentRun.started_at < end)
        .group_by(AgentRun.agent_type)
    )
    try:
        rows = (await db.execute(grouped)).all()
    except SQLAlchemyError as exc:
        raise db_refusal("day spend truth", exc) from exc

    per_agent: dict[str, Decimal] = {}
    unpriced_total = in_flight_total = measured_runs = 0
    for agent_type, measured, runs, unpriced_runs, open_runs in rows:
        unpriced_total += int(unpriced_runs or 0)
        in_flight_total += int(open_runs or 0)
        measured_runs += int(runs) - int(unpriced_runs or 0) - int(open_runs or 0)
        converted = money(measured)
        if converted is not None:
            per_agent[str(agent_type)] = converted
    return SpendTruth(
        total=None if unpriced_total else sum(per_agent.values(), Decimal("0")),
        per_agent=per_agent,
        finished_unpriced=unpriced_total,
        in_flight=in_flight_total,
        measured_runs=measured_runs,
    )


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

    F-W5-TS-1 (wave 5 finding, fixed in wave 6) — A FLOOR IS NOT A TOTAL.  On the mixed live day
    (2026-10-28/29) one agent's run had a measured cost and another's finished run had none, and
    this block served `spent: 0.0032` with `remaining`/`burn_rate` derived from it while
    `day_spend_truth.total` for the SAME day was UNKNOWN.  The day total is now decided by the same
    rule the cap gate uses: a FINISHED run with no cost (`ended_at IS NOT NULL AND cost_usd IS
    NULL`) makes the total unknown — `spent`/`remaining`/`burn_rate` are None — and the measured
    portion is published SEPARATELY as `measured_floor_usd` (named a floor, never summed into a
    total) with `spent_status = "partial"` and the count of unpriced finished runs.  An OPEN run
    (`ended_at IS NULL`) is NOT a reason to refuse a total: it is counted in `in_flight_runs` and
    its cost is bounded by the per-run cap — the same distinction `day_spend_truth` records.  The
    three states stay distinguishable on the wire: `measured` (total known, absent day = 0.0),
    `partial` (some of the day measured, total unknown), `unmeasured` (no priced run at all).

    Exactness: the cap and the spend reach `spend_summary` as `Decimal` (this module's money rule,
    see the module docstring), so `remaining` is `0.3`, not `0.3000000000000007`; the response
    model floats the values at the wire boundary only.  `unit_economics` takes `float`s (its own
    documented `dict[str, float]` contract).
    """
    start, end = day_window(db)

    # ONE grouped statement: the day's spend AND the completed-run count per agent, from the same
    # rows — a run's cost and its status live on the row that carries both, so they cannot drift
    # apart.  `sum(case(...))` (not `count(*) filter`) is spelled the same way on both engines.
    # F-W5-TS-1: the same statement also separates a FINISHED run with no cost (which makes the day
    # total unknown) from an OPEN one (bounded by the per-run cap, and not a reason to refuse).
    finished_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_not(None))
    open_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_(None))
    grouped = (
        select(
            AgentRun.agent_type,
            func.sum(AgentRun.cost_usd).label("spent"),
            func.sum(case((AgentRun.status == "completed", 1), else_=0)).label("completed"),
            func.sum(case((finished_unpriced, 1), else_=0)).label("finished_unpriced"),
            func.sum(case((open_unpriced, 1), else_=0)).label("in_flight"),
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
    finished_unpriced_runs = in_flight_runs = 0
    for agent, spent, completed, finished_unpriced_count, in_flight_count in rows:
        name = str(agent)
        finished_unpriced_runs += int(finished_unpriced_count or 0)
        in_flight_runs += int(in_flight_count or 0)
        if spent is None:  # rows, no reported cost: NOT a measured 0 (F-08/F-10)
            unmeasured_spend_agents.append(name)
            continue
        measured = money(spent)
        assert measured is not None  # guarded above: `money` only passes a None through
        spend_by_agent[name] = measured
        completed_by_agent[name] = int(completed)

    measured_total = sum(spend_by_agent.values(), Decimal("0"))
    #: The measured portion of a day whose TOTAL is unknown — published as a floor, never as the
    #: day's spend.  `None` when the day is fully measured, and `None` when NO run was priced (a
    #: floor of 0.0 with nothing measured would be a fabricated zero).
    measured_floor: Decimal | None = None
    if not rows:                                    # absent day: measured zero (D3)
        day_spent: Decimal | None = Decimal("0")
        spent_status = "measured"
    elif not spend_by_agent:                        # runs exist, none reported a cost
        day_spent = None
        spent_status = "unmeasured"
    elif finished_unpriced_runs:                    # F-W5-TS-1: part of the day is unmeasured
        day_spent = None
        spent_status = "partial"
        measured_floor = measured_total
    else:                                           # every finished run reported its cost
        day_spent = measured_total
        spent_status = "measured"

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
    # F-W5-TS-1: the day's measurement state, and — when the total is unknown — the measured
    # portion published under its own name, so no reader can add it up as "the day's spend".
    block["spent_status"] = spent_status
    block["measured_floor_usd"] = None if measured_floor is None else float(measured_floor)
    block["unmeasured_finished_runs"] = finished_unpriced_runs
    block["in_flight_runs"] = in_flight_runs
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

    DT-01 — "omitted" and "measured 0" are different states.  The INSERT used to pass only
    the keys a caller supplied, and the DDL's `DEFAULT 0` (migration 0001) then wrote a
    fabricated zero into every omitted money column, which `GET /finance/summary` served as a
    measurement.  Now every `_SNAPSHOT_COLUMNS` key this call did NOT supply is written as an
    explicit NULL (migration 0017 drops the server defaults; the explicit NULL keeps the
    statement honest even against a database that still carries them).  A supplied `0` is a
    measured zero and is written as `0`.

    The UPDATE half only moves the columns THIS call supplied: a retry that omits a column must
    not erase a value an earlier same-day run did measure (the row is the DAY's snapshot, not
    the last attempt's).

    TS-3 — NO MEASUREMENT, NO ROW.  A call that supplies a date and no measured column used to be
    an accepted primitive: it wrote a seven-NULL row (or, before 0017, a seven-ZERO row), and row
    EXISTENCE is the provenance signal `get_finance_summary` (`last_snapshot_date`),
    `build_cash_block` and the finance page's idle state all read.  It now refuses `E_VALIDATION`
    BY NAME.  A supplied `0` IS a measurement (`is not None`, never truthiness) and still writes.

    TS-2 — A SNAPSHOT RECORDS A DAY THAT HAPPENED.  `snapshot_date` is model-controlled (the
    finance agent supplies it inside its JSON output), and `get_finance_summary` picks the
    newest-by-date row: one future-dated snapshot would hijack the summary — and the dashboard's
    measured-finance block — until that date arrived.  Live evidence of the quirk class: run 5
    (started 2026-10-14) stamped `snapshot_date = 2026-02-05` from the model.  A date AFTER both
    the company-local today and the UTC today is therefore refused `E_VALIDATION`; the maximum is
    used because the agent is told to emit a UTC date while the rest of the app thinks in the
    company's local day, and refusing a legitimate "today" at the UTC/local boundary would be a
    false refusal, not a guard.
    """
    unknown = sorted(set(values) - set(_SNAPSHOT_COLUMNS))
    if unknown:
        # A key with no column used to reach the INSERT and die as an uncoded CompileError.
        raise MeridianError(
            E_VALIDATION,
            f"revenue snapshot has no column(s) {unknown}",
            how_to_fix=f"supply only these snapshot columns: {', '.join(_SNAPSHOT_COLUMNS)}",
            context={"unknown": unknown},
        )

    # TS-2 (input validation at the boundary): a datetime is a `date` SUBCLASS, and a string would
    # die as an uncoded TypeError in the future-date comparison before the database ever sees it.
    if isinstance(snapshot_date, datetime) or not isinstance(snapshot_date, date):
        raise MeridianError(
            E_VALIDATION,
            f"revenue snapshot date must be a date, not {type(snapshot_date).__name__}",
            how_to_fix="pass snapshot_date as a datetime.date (YYYY-MM-DD); never a datetime or a string",
            context={"snapshot_date": repr(snapshot_date)[:120]},
        )
    company_today = today_in()
    utc_today = today_utc()
    latest_plausible_day = max(company_today, utc_today)
    if snapshot_date > latest_plausible_day:
        raise MeridianError(
            E_VALIDATION,
            f"revenue snapshot date {snapshot_date.isoformat()} is in the future — a snapshot "
            "records a day that happened",
            how_to_fix=(
                "supply snapshot_date = the day the run actually measured (YYYY-MM-DD), not later "
                "than today; a future-dated row becomes the newest snapshot and would hijack every "
                "summary read until that date arrives"
            ),
            context={
                "snapshot_date": snapshot_date.isoformat(),
                "latest_plausible_day": latest_plausible_day.isoformat(),
                "company_local_today": company_today.isoformat(),
                "utc_today": utc_today.isoformat(),
            },
        )

    # TS-3: a row's existence is a provenance claim; only a call that measured something may make
    # it.  `values.get(...) is not None` keeps a supplied 0 (a measured zero) and refuses only a
    # call whose every optional value is absent/None.
    measured_values = {
        column: values.get(column)
        for column in _SNAPSHOT_COLUMNS
        if values.get(column) is not None
    }
    if not measured_values:
        raise MeridianError(
            E_VALIDATION,
            "revenue snapshot measured no column — refusing to write a date-only row",
            how_to_fix=(
                "supply at least one measured snapshot column ("
                + ", ".join(_SNAPSHOT_COLUMNS)
                + "); a row with a date and no values claims a day was measured"
            ),
            context={"snapshot_date": snapshot_date.isoformat()},
        )

    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    insert_values = {column: values.get(column) for column in _SNAPSHOT_COLUMNS}
    base = insert_fn(RevenueSnapshot).values(snapshot_date=snapshot_date, **insert_values)
    supplied = [column for column in _SNAPSHOT_COLUMNS if column in values]
    if supplied:
        stmt = base.on_conflict_do_update(
            index_elements=["snapshot_date"],
            set_={column: base.excluded[column] for column in supplied},
        ).returning(RevenueSnapshot)
    else:
        # Nothing to update: a conflicting row is the answer, not an error (see the read-back).
        stmt = base.on_conflict_do_nothing(index_elements=["snapshot_date"]).returning(
            RevenueSnapshot
        )
    # populate_existing: without it, a session that already holds this snapshot in its
    # identity map gets its STALE copy back from `returning()` even though the row was
    # updated (measured: the DB said mrr=250 while the returned object still said 100).
    stmt = stmt.execution_options(synchronize_session=False, populate_existing=True)
    snapshot = (await db.execute(stmt)).scalars().one_or_none()
    if snapshot is None:
        # `on_conflict_do_nothing` returns no row when the date already exists — re-read it.
        snapshot = (
            await db.execute(
                select(RevenueSnapshot)
                .where(RevenueSnapshot.snapshot_date == snapshot_date)
                .execution_options(populate_existing=True)
            )
        ).scalars().one_or_none()
    if snapshot is None:
        raise MeridianError(
            E_DB,
            f"revenue snapshot for {snapshot_date} could not be written or re-read",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"snapshot_date": snapshot_date.isoformat()},
        )
    return snapshot


def _normalize_currency(currency: Any) -> str | None:
    """One normalizer for every persisted currency value (F7/V12-W-C): blank -> None, else
    stripped lower — the exact shape both writers' CHECK families require on PostgreSQL."""
    if currency is None:
        return None
    text = str(currency).strip().lower()
    return text or None


async def record_expense(db: AsyncSession, **values: Any) -> ExpenseRecord:
    """Validate + insert one expense row.

    `amount_cents` must be a positive integer (a named `E_VALIDATION` otherwise — the
    payload path counts the skip, never writes a fake row).  The CURRENCY rule is the same
    one `record_stripe_event` applies, because the two writers share a CHECK family
    (`ck_expense_records_currency` / `ck_stripe_events_currency`, 0002:107-108:
    `currency = lower(currency)`, enforced on PostgreSQL): a text currency is normalised
    (`strip().lower()`, blank -> `None`) HERE, the single crossing point for both the
    payload path (`persist_finance_run` -> `_expense_kwargs`) and direct callers — before
    this, a model emitting `"USD"` stored `"USD"` on SQLite (no CHECK there; L2-09 probe)
    and on PostgreSQL would have aborted the whole finance persist batch as an
    `IntegrityError` blamed on the database.  A non-text currency is refused by NAME
    instead of dying in `.lower()`.
    """
    amount = values.get("amount_cents")
    if amount is None or int(amount) <= 0:
        raise MeridianError(
            E_VALIDATION,
            f"expense amount_cents must be a positive integer, got {amount!r}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"amount_cents": amount},
        )
    currency = values.get("currency")
    if currency is not None:
        if not isinstance(currency, str):
            raise MeridianError(
                E_VALIDATION,
                f"expense currency must be text, got {type(currency).__name__}",
                how_to_fix=ERROR_HINTS[E_VALIDATION],
                context={"currency": repr(currency)[:100]},
            )
        # `or None` mirrors `record_stripe_event`'s `currency.lower() if currency else None`:
        # a blank is "not recorded", never an empty-string currency.
        values["currency"] = _normalize_currency(currency)
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
#: TS-1 — THE measurement vocabulary of a snapshot row, in the row's own column order.  Public so
#: the wire marker (`RevenueSnapshotResponse.measured` / `.unmeasured` / `.provenance`) and the
#: dashboard's measured-finance block read the SAME list: a column the marker calls measured is a
#: column the writer could have supplied, and the two surfaces cannot drift.
SNAPSHOT_MEASUREMENT_COLUMNS: tuple[str, ...] = _SNAPSHOT_COLUMNS
#: `expense_records` columns a payload expense entry may supply (`date` handled separately).
_EXPENSE_COLUMNS = ("category", "vendor", "amount_cents", "currency", "description", "external_ref")


def snapshot_provenance(snapshot: RevenueSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    """TS-1 — what a snapshot row actually MEASURED, derived from the row itself.

    THE DEFECT.  `RevenueSnapshotResponse` served seven nullable money/count columns with no
    marker for which of them were measured.  A legacy row (written before migration 0017 dropped
    `DEFAULT 0`) carries non-NULL zeros the run never supplied; a repaired row carries NULLs where
    the zeros were fabricated; a partial row (a run that measured only balance + month revenue)
    carries a mix.  Every reader had to infer the state from nulls alone, and a row with 4 measured
    values looked identical in SHAPE to a row with 4 unmeasured ones.

    THE MARKER IS DERIVED AND THEREFORE NOT FABRICABLE.  `measured` is exactly the columns whose
    value is not NULL (a supplied `0` is a measured zero and IS included — `is not None`, never
    truthiness), `unmeasured` is its exact complement, and `provenance` names the coverage:
    `"complete"` (all seven carry a value), `"partial"` (some do), `"unmeasured"` (none do).

    WHAT THIS CANNOT PROVE, stated honestly.  A database that still predates 0017's writer/DDL
    halves can hold an all-zeros row that was never measured: the reader sees seven non-NULL values
    and cannot distinguish a complete measurement from a fabricated one without the run ledger.
    That state is uncreatable on the current schema (the server defaults are gone and
    `upsert_revenue_snapshot` writes explicit NULLs), and this marker's job is to make the
    PARTIAL/unmeasured states — the ones the live repair produced — visible on the wire instead of
    leaving them to be inferred differently by every client.

    `snapshot` is an ORM row or a mapping of the same column names (the dashboard composes the
    latest row from one SELECT and reuses this helper).
    """
    def value(column: str) -> Any:
        if isinstance(snapshot, Mapping):
            return snapshot.get(column)
        return getattr(snapshot, column)

    measured = [column for column in SNAPSHOT_MEASUREMENT_COLUMNS if value(column) is not None]
    unmeasured = [column for column in SNAPSHOT_MEASUREMENT_COLUMNS if value(column) is None]
    if not measured:
        provenance = "unmeasured"
    elif unmeasured:
        provenance = "partial"
    else:
        provenance = "complete"
    return {"measured": measured, "unmeasured": unmeasured, "provenance": provenance}


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
        raise MeridianError(
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
        raise MeridianError(
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
    flushes only and never commits; as of V12/W-C (F22) all writes run inside ONE savepoint — a
    mid-batch failure rolls THIS persist's rows back, so a caller's later commit can never
    persist a partial batch (the report says ok=False and reports `rolled_back`).  An expense the service refuses (`amount_cents`
    missing or <= 0, or a row that cannot be formed at all) is counted in
    `skipped_invalid` — never silently dropped, never fake-written.

    Returns `{"ok": True, "snapshot_written": bool, "expenses_written": int,
    "skipped_invalid": int, "snapshot_skipped": str | None}` — `snapshot_written` is True only when
    the upsert really returned a row, which is the fact the worker reports as the run's
    `snapshot_written`.

    LIVE-3 — a snapshot row is a PROVENANCE claim, so a snapshot that measured nothing writes
    nothing.  Before this guard, a payload naming a date with no measured column
    (`{"snapshot": {"snapshot_date": …}}`) created a `revenue_snapshots` row whose seven columns
    are all NULL and reported `snapshot_written: True`.  Nothing numeric was invented, but ROW
    EXISTENCE is the provenance signal the read paths trust: `get_finance_summary` serves
    `last_snapshot_date` from the newest row, `build_cash_block`'s "no revenue snapshot has been
    recorded yet" reason stops firing, and the finance page leaves its "not started" idle state —
    i.e. a run that measured nothing presented itself as a measured day.  The skip is NAMED in
    `snapshot_skipped: "unmeasured"` (reported, never silent) and it is a SKIP, not an error: the
    run succeeded and genuinely measured nothing.  A supplied `0` is still a measured zero and
    still writes (see the measured-zero test), and a measurement-less same-day retry therefore
    cannot erase an earlier same-day reading.
    """
    snapshot_written = False
    snapshot_skipped: str | None = None
    expenses_written = 0
    skipped_invalid = 0
    try:
        # F22 (V12/W-C): ONE savepoint for this call's writes — a mid-batch failure
        # rolls THIS persist back WITHOUT aborting the caller's transaction (a later
        # caller commit after a partially-failed persist was the F22 failure class).
        async with db.begin_nested():
            snapshot = result.get("snapshot")
            if snapshot is not None:
                if not isinstance(snapshot, dict):
                    raise MeridianError(
                        E_VALIDATION,
                        f"snapshot is not an object: {type(snapshot).__name__}",
                        how_to_fix="the finance agent must provide a snapshot object or null",
                        context={"snapshot": repr(snapshot)[:200]},
                    )
                snapshot_date = _iso_date(snapshot.get("snapshot_date"))
                if snapshot_date is None:
                    raise MeridianError(
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
                # LIVE-3: no measured column == no measurement.  `values` keeps a supplied 0 (the
                # `is not None` test), so only a snapshot that supplied NOTHING reaches this skip.
                # The row is NOT written: an all-NULL row is not a measurement, it is a provenance
                # claim that the summary/cash block/idle-state all read as "a day was measured".
                if not values:
                    snapshot_skipped = "unmeasured"
                else:
                    row = await upsert_revenue_snapshot(db, snapshot_date=snapshot_date, **values)
                    snapshot_written = row is not None
            for expense in result.get("expenses") or []:
                try:
                    await record_expense(db, **_expense_kwargs(expense))
                    expenses_written += 1
                except MeridianError as exc:
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
                "snapshot_skipped": snapshot_skipped,
                "expenses_written": expenses_written,
                "skipped_invalid": skipped_invalid,
            }
    except Exception as exc:  # noqa: BLE001 - the documented contract is "never raises"
        if isinstance(exc, MeridianError):
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
            "snapshot_written": False,  # F22: the savepoint rolled THIS persist's writes back
            "snapshot_skipped": None,
            "expenses_written": 0,
            "skipped_invalid": skipped_invalid,
            "rolled_back": True,
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
    payment_intent_id: str | None = None,
) -> StripeEvent | None:
    """Persist a Stripe event EXACTLY once.  Returns the row (new or existing).

    ONE statement — `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING
    id` — so a concurrent duplicate replay cannot raise `IntegrityError`, which
    would surface as a 5xx and trigger Stripe's retry storm.  When the insert
    conflicts, the existing row is re-read and returned (idempotent success, not a
    fake new row).

    `payment_intent_id` (T14/D21) is the refundable id the webhook extracted from the
    delivery's shape (`extract_payment_intent_id`).  When it is present it is persisted as ONE
    Meridian-owned sibling key on the stored payload — `raw_payload["_meridian"]["payment_intent_id"]`
    — because `stripe_events` carries no dedicated column in this lane's ownership (models/alembic
    are another seat's files) and the verbatim Stripe event must stay byte-for-byte under its own
    keys.  A shape that carries NO PaymentIntent writes no annotation at all: the verbatim event
    says so (its `payment_intent` field is null/absent) and the reader re-derives the answer with
    the same extractor — writing a `null` there would claim an extraction for every row whose
    caller never ran one.  Rows written by other paths (raw SQL, older writers) behave the same:
    `recent_failed_payments` extracts from the verbatim event.
    """
    if not stripe_event_id:
        raise MeridianError(
            E_VALIDATION,
            "stripe_event_id is empty: the event cannot be de-duplicated",
            how_to_fix="reject events without an id at the webhook boundary",
        )
    stored_payload: dict[str, Any] | None = raw_payload
    if isinstance(raw_payload, dict) and payment_intent_id is not None:
        # Copy-on-write: the caller's event object is never mutated, and the verbatim keys stay
        # untouched — the annotation rides beside them under a key Stripe does not use.
        annotation = raw_payload.get("_meridian")
        stored_payload = {
            **raw_payload,
            "_meridian": {
                **(annotation if isinstance(annotation, dict) else {}),
                "payment_intent_id": payment_intent_id,
            },
        }
    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(StripeEvent)
        .values(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=_normalize_currency(currency),
            status=status,
            raw_payload=stored_payload,
        )
        .on_conflict_do_nothing(index_elements=["stripe_event_id"])
        .returning(StripeEvent)
    )
    created = (await db.execute(stmt)).scalars().one_or_none()
    if created is not None:
        # V9/B4 additive hook — a NEWLY INGESTED failed-payment delivery also flips OUR invoice
        # rows by `payment_link` match (plan §9 B4).  On the create leg ONLY: a replay of the
        # same delivery re-reads the existing row and flips nothing twice.  Never raises (see
        # `_flip_invoices_safely`): the webhook's 200 contract and Stripe's retry storm survive
        # an additive-path failure, while the failure itself is still NAMED in the activity feed.
        if event_type in FAILED_PAYMENT_EVENT_TYPES:
            await _flip_invoices_safely(
                db, event_id=stripe_event_id, event_type=event_type, payload=stored_payload
            )
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


def persisted_payment_intent_id(row: StripeEvent) -> str | None:
    """The refundable id recorded on a `stripe_events` row — annotation first, event fallback.

    The annotation (`raw_payload["_meridian"]["payment_intent_id"]`, written by the webhook when it
    really extracted one) is preferred because it was taken at INGEST from a delivery we saw
    whole.  Everything else — a shape that carried none (no annotation by design), a row written
    by another path (raw SQL/ops, a pre-T14 writer) — is answered by re-reading the verbatim event
    through the same extractor: the id is durably there (fix-5 §8) and this is the code path that
    finally reads it.  Honest `None` when neither answers.

    Public because TWO readers now need the same answer (DT-03): `recent_failed_payments` (the
    refund producer) and `report_service.build_cash_block`, which groups the 30-day inflow by this
    id so one card payment (charge + payment_intent + invoice.paid + invoice.payment_succeeded)
    is counted once instead of once per event family.
    """
    payload = row.raw_payload
    if not isinstance(payload, dict):
        return None
    annotation = payload.get("_meridian")
    if isinstance(annotation, dict):
        recorded = annotation.get("payment_intent_id")
        if isinstance(recorded, str) and recorded.strip():
            return recorded.strip()
    return extract_payment_intent_id(payload)


async def recent_failed_payments(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    """The failed-payment records the FINANCE AGENT consumes — newest first.

    WHY this exists (T14/D21, fix-5 §8): `stripe_events` had a producer (the webhook) and no
    consumer — the refundable `pi_…` id sat in `raw_payload` with *"NO code path that reads it
    into the finance agent's context"*, so a model could never emit a real `pi_` id and the
    refund adapter's fail-closed guard was the only thing the refund path could ever reach.  This
    is the read half: each record carries the id a refund needs, or an honest `None` (the shape
    carried none / it is not a `pi_` id), which the agent turns into a NAMED refusal instead of a
    fabricated one.

    Shape (one dict per row, JSON-ready so it can ride a task's `metadata`):
    `{event_id, event_type, payment_intent_id, amount_cents, currency, customer_id, occurred_at}`.
    Only `FAILED_PAYMENT_EVENT_TYPES` are read; a dead read is a named `E_DB`.
    """
    rows = await fetch_scalars(
        db,
        select(StripeEvent)
        .where(StripeEvent.event_type.in_(FAILED_PAYMENT_EVENT_TYPES))
        .order_by(StripeEvent.id.desc())
        .limit(limit),
        what="failed stripe events",
    )
    return [
        {
            "event_id": row.stripe_event_id,
            "event_type": row.event_type,
            "payment_intent_id": persisted_payment_intent_id(row),
            "amount_cents": row.amount_cents,
            "currency": row.currency,
            "customer_id": row.customer_id,
            "occurred_at": row.processed_at.isoformat() if row.processed_at is not None else None,
        }
        for row in rows
    ]


# ── V9/B4 — the invoice webhook flip (ADDITIVE) ──────────────────────────────────────────────
#: Length under which a string is not trusted as a real payment-link identifier/URL for the
#: containment match (`plink_…` ids and `https://…` urls are both comfortably longer).
_PAYMENT_LINK_MIN_TOKEN = 8


def _payment_link_candidates(payload: Any) -> list[str]:
    """Every payment-link identifier a failed-payment delivery names, in order, deduped.

    Shape-driven, like `extract_payment_intent_id`: `data.object.payment_link` is Stripe's
    reference to the link that produced the failed payment — a string id/url in the common
    case, or an expanded object (then its `url`/`id`).  Anything else answers `[]` — there is
    no candidate to fabricate one from.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    data_object = data.get("object") if isinstance(data, dict) else None
    if not isinstance(data_object, dict):
        return []
    raw = data_object.get("payment_link")
    candidates: list[str] = []
    if isinstance(raw, str) and raw.strip():
        candidates.append(raw.strip())
    elif isinstance(raw, dict):
        for key in ("url", "id"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    return list(dict.fromkeys(candidates))


def _link_matches(stored: str, candidate: str) -> bool:
    """Equality, or containment when BOTH sides are long enough to be a real token.

    The same link may be stored as its `buy.stripe.com` URL while a delivery names the
    `plink_…` id (or the reverse); a containment check bridges that without ever matching on
    short, accidental substrings.
    """
    if stored == candidate:
        return True
    if len(stored) >= _PAYMENT_LINK_MIN_TOKEN and len(candidate) >= _PAYMENT_LINK_MIN_TOKEN:
        return candidate in stored or stored in candidate
    return False


async def apply_failed_payment_to_invoices(
    db: AsyncSession, *, event_id: str, event_type: str, payload: Any
) -> dict[str, Any]:
    """Flip OUR invoice rows to `overdue` for a failed-payment delivery — or name the miss.

    ADDITIVE consumer of the failed-payment deliveries (`FAILED_PAYMENT_EVENT_TYPES`): the
    `invoice.payment_failed` webhook path already persisted the event; when the delivery names
    a `payment_link`, every invoice row whose stored `payment_link` matches flips
    `sent -> overdue` — but ONLY from `sent` (draft/paid/void/overdue rows are left alone and
    the no-flip is recorded too, never silent).  A delivery that matches NOTHING writes an
    `invoice_payment_failed_unmatched` activity row naming the candidates — the plan's rule:
    "no match possible becomes an activity row noting the miss — never silent".
    """
    candidates = _payment_link_candidates(payload)
    rows = await fetch_scalars(
        db, select(Invoice).where(Invoice.payment_link.is_not(None)), what="invoices with links"
    )
    matched: list[tuple[Invoice, str]] = []
    for row in rows:
        stored = str(row.payment_link or "")
        hit = next((cand for cand in candidates if _link_matches(stored, cand)), None)
        if hit is not None:
            matched.append((row, hit))
    if not matched:
        named = ", ".join(candidates) if candidates else "none named by the delivery"
        await log_activity(
            db,
            "finance",
            "invoice_payment_failed_unmatched",
            summary=(
                f"failed payment {event_id} matched no invoice "
                f"(payment_link candidates: {named})"
            ),
            level="warning",
            detail={
                "event_id": event_id,
                "event_type": event_type,
                "candidates": candidates,
                "matched": 0,
            },
        )
        return {"matched": 0, "flipped": []}
    flipped: list[int] = []
    for row, hit in matched:
        from_status = row.status.value if isinstance(row.status, InvoiceStatus) else str(row.status)
        if from_status == InvoiceStatus.SENT.value:
            # The transition matrix's one enforcement point (lazy import: keeps this module's
            # existing import graph untouched — the additive rule).
            from app.services.invoice_service import transition_invoice

            await transition_invoice(db, row, InvoiceStatus.OVERDUE)
            await log_activity(
                db,
                "finance",
                "invoice_payment_failed",
                summary=f"Invoice {row.number} marked overdue: payment failed ({event_id})",
                level="warning",
                detail={
                    "invoice_id": row.id,
                    "number": row.number,
                    "event_id": event_id,
                    "event_type": event_type,
                    "from_status": from_status,
                    "matched_link": hit,
                },
            )
            flipped.append(int(row.id))
        else:
            await log_activity(
                db,
                "finance",
                "invoice_payment_failed_noop",
                summary=(
                    f"failed payment {event_id} matched invoice {row.number} but it is "
                    f"{from_status}: no flip"
                ),
                detail={
                    "invoice_id": row.id,
                    "number": row.number,
                    "event_id": event_id,
                    "from_status": from_status,
                },
            )
    return {"matched": len(matched), "flipped": flipped}


async def _flip_invoices_safely(
    db: AsyncSession, *, event_id: str, event_type: str, payload: Any
) -> None:
    """`apply_failed_payment_to_invoices` behind the webhook's 200 contract — never raises.

    A broken flip must not 5xx the delivery (that is what triggers Stripe's retry storm), but
    it must not be silent either: the failure is recorded as an activity row (and logged) —
    the event row itself is already persisted, well-formed, and is returned to the caller.
    """
    try:
        # F2 (V11/W2) — THE SAVEPOINT.  When the swallowed failure is a flush-level DB error
        # (constraint, serialization failure, locked database), the session is left in
        # "pending rollback": the webhook's post-handler commit (`core/database.py:106-120`)
        # then raised `PendingRollbackError` and the delivery 5xxed — which is exactly what
        # triggers Stripe's retry storm.  A savepoint contains the failure: the flip's partial
        # writes roll back, the session stays USABLE, and the delivery's own commit (the event
        # row) still lands.  Same pattern as `_common.create_task_deduped`'s guarded flush.
        async with db.begin_nested():
            await apply_failed_payment_to_invoices(
                db, event_id=event_id, event_type=event_type, payload=payload
            )
    except Exception as exc:  # noqa: BLE001 - documented contract: never raises
        logger.exception("invoice flip for %s failed", event_id)
        try:
            # The failure record gets its OWN savepoint: a broken write here must not poison
            # the request's session either (the worker log still carries the exception).
            async with db.begin_nested():
                await log_activity(
                    db,
                    "finance",
                    "invoice_payment_failed_flip_error",
                    summary=f"invoice flip for {event_id} failed: {type(exc).__name__}: {exc}",
                    level="warning",
                    detail={"event_id": event_id, "error": f"{type(exc).__name__}: {exc}"},
                )
        except Exception:  # noqa: BLE001 - a broken session cannot carry the record; the log does
            logger.exception("invoice flip error row for %s could not be written", event_id)


__all__ = [
    "FAILED_PAYMENT_EVENT_TYPES",
    "REPORTING_CURRENCY",
    "SpendTruth",
    "apply_failed_payment_to_invoices",
    "day_spend_truth",
    "excluded_currency_filter",
    "extract_payment_intent_id",
    "get_ai_spend_summary",
    "get_finance_summary",
    "list_expenses",
    "list_revenue_snapshots",
    "list_stripe_events",
    "money",
    "persist_finance_run",
    "persisted_payment_intent_id",
    "recent_failed_payments",
    "record_expense",
    "record_stripe_event",
    "reporting_currency_filter",
    "upsert_revenue_snapshot",
]
