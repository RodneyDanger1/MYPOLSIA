from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class AiBurnRateView(BaseModel):
    """`ai_spend.burn_rate` — the literal output of `accounting.burn_rate` (accounting.py:292).

    `projected_day_total_usd` is None only in the first instant of the local day (elapsed fraction
    0), where the function refuses to project a rate from a zero-length window instead of dividing
    by it; `status` is that function's own verdict (`ok` / `burning_fast` /
    `over_budget_projection`).
    """

    spent_usd: float
    day_limit_usd: float
    elapsed_fraction: float
    projected_day_total_usd: float | None = None
    status: str


class AgentEconomicsView(BaseModel):
    """One agent's row inside `ai_spend.unit_economics.per_agent`.

    `cost_per_completed_task_usd` is None — "unmeasured", never `$0.00` — when no completion was
    measured for that agent.
    """

    spend_usd: float
    completed: int
    cost_per_completed_task_usd: float | None = None


class UnitEconomicsView(BaseModel):
    """`ai_spend.unit_economics` — the literal output of `accounting.unit_economics` (:310), plus
    the additive `unmeasured_spend_agents` marker.

    The totals describe exactly the agents present in `per_agent`, over the same company-local day
    as the block above.  An agent whose day spend was NOT measurable (all of its `agent_runs
    .cost_usd` values are NULL) is named in `unmeasured_spend_agents` rather than priced: it is
    neither `$0.00`/task nor silently dropped, because `unit_economics` alone would collapse a
    missing spend entry into a measured-looking zero.
    """

    total_spend_usd: float
    total_completed: int
    cost_per_completed_task_usd: float | None = None
    per_agent: dict[str, AgentEconomicsView] = {}
    note: str
    unmeasured_spend_agents: list[str] = []


class AiSpendSummary(BaseModel):
    """`ai_spend` — the additive AI-spend block of `GET /finance/summary` (R24).

    `limit` is the day cap the dispatch gate enforces (`agent_daily_cost_cap_usd`, read through
    `accounting.budget_policy_from_settings`), `spent` the day's measured `agent_runs.cost_usd`
    total.  `spent`/`remaining` are None when the day could not be measured — an absent day is a
    measured `0.0`, never the other way round (accounting.py:276-289).
    """

    limit: float
    spent: float | None = None
    remaining: float | None = None
    burn_rate: AiBurnRateView | None = None
    unit_economics: UnitEconomicsView | None = None


class FinanceSummaryResponse(BaseModel):
    """Wire shape of `GET /finance/summary`.

    Every money field is nullable on purpose: `None` is "not measured" (no snapshot
    row yet, no expenses this month, no ad spend recorded) and must stay
    distinguishable from a measured `0` — the frontend renders `None` as an em dash
    and a real `0` as `$0.00`.

    `ai_spend` (R24) is that same contract for the AI-spend day view: it is additive
    (appended last, nullable) so every pre-R24 client keeps parsing the seven keys
    above unchanged.
    """

    mrr_cents: int | None = None
    arr_cents: int | None = None
    active_subscribers: int | None = None
    total_ad_spend_usd: float | None = None
    total_expenses_month_cents: int | None = None
    stripe_balance_cents: int | None = None
    last_snapshot_date: date | None = None
    ai_spend: AiSpendSummary | None = None


class RevenueSnapshotResponse(ORMModel):
    """`mrr_cents`/`arr_cents`/`active_subscribers` are NULLABLE in the frozen schema
    (`models/finance.py`; `0001`'s `CREATE TABLE revenue_snapshots` carries `server_default`s but
    no `NOT NULL`), so a raw-SQL/ops writer can leave them NULL — declaring them required turned
    ONE such row into a bare, codeless 500 for the whole `/finance/revenue` list (L1-11).  `None`
    is served as null ("not measured"), never a fabricated `0` (the house rule in api.ts: a null
    money field is not a zero reading).
    """

    id: int
    snapshot_date: date
    mrr_cents: int | None = None
    arr_cents: int | None = None
    active_subscribers: int | None = None
    churned_today: int | None = None
    new_today: int | None = None
    total_revenue_month_cents: int | None = None
    stripe_balance_cents: int | None = None
    created_at: datetime | None = None


class ExpenseRecordResponse(ORMModel):
    id: int
    category: str
    vendor: str
    amount_cents: int
    currency: str | None = None
    description: str | None = None
    date: date
    external_ref: str | None = None
    created_at: datetime | None = None


class StripeEventResponse(ORMModel):
    id: int
    stripe_event_id: str
    event_type: str
    customer_id: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    status: str | None = None
    processed_at: datetime | None = None


class StripeWebhookResponse(BaseModel):
    """The declared shape of `POST /api/v1/finance/stripe/webhook`'s 200 body — now WIRED.

    fix-5 (2026-10-13) labelled this model zero-consumer: the route returned its own hand-built
    `{"received": True}` dict and declared no `response_model`, so the documented and the actual
    response were two copies of one fact.  D20 ruled the model is the contract: the route now
    declares `response_model=StripeWebhookResponse`, which — per FastAPI's own semantics — makes
    the model VALIDATE the returned dict and publish the response schema, instead of the response
    merely happening to match.  The schema was left TRUE to the handler: the handler returns
    exactly `{"received": True}`, so this model gained no field and dropped none (adjusting the
    model to some imagined response would have documented a body the route never returns).

    A non-2xx delivery answers the refusal envelope (`{"ok": false, "error": {…}}`), never this
    shape: 500 when the webhook secret is unconfigured, 400 for a missing/invalid signature or a
    payload that cannot be de-duplicated.
    """

    received: bool = True
