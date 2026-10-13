from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class FinanceSummaryResponse(BaseModel):
    """Wire shape of `GET /finance/summary`.

    Every money field is nullable on purpose: `None` is "not measured" (no snapshot
    row yet, no expenses this month, no ad spend recorded) and must stay
    distinguishable from a measured `0` — the frontend renders `None` as an em dash
    and a real `0` as `$0.00`.
    """

    mrr_cents: int | None = None
    arr_cents: int | None = None
    active_subscribers: int | None = None
    total_ad_spend_usd: float | None = None
    total_expenses_month_cents: int | None = None
    stripe_balance_cents: int | None = None
    last_snapshot_date: date | None = None


class RevenueSnapshotResponse(ORMModel):
    id: int
    snapshot_date: date
    mrr_cents: int
    arr_cents: int
    active_subscribers: int
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
    received: bool = True


class ExpenseCreate(BaseModel):
    category: str
    vendor: str
    amount_cents: int
    currency: str = "usd"
    description: str | None = None
    date: date
    external_ref: str | None = None
