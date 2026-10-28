"""stripe_events + revenue_snapshots + expense_records + the dunning claim results.

`invoice_dunning_claim_results` (DT-05) lives here rather than in a new models module because it
is a money/audit ledger and because `app/models/__init__.py` imports THIS module: a class defined
here is registered on `Base.metadata` (so `create_all` and alembic/env.py see it) without touching
the shared `__init__.py`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(255))
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="processed", default="processed"
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RevenueSnapshot(Base):
    """One day's measured revenue facts.  NULL means "the run did not measure it".

    DT-01: the seven money/count columns used to carry `server_default="0"` AND an ORM
    `default=0`, so a finance run that reported only MRR stored `arr=0, active=0, churn=0,
    new=0, month_revenue=0, balance=0` and `GET /finance/summary` served those zeros as
    measurements.  Both defaults are gone (migration 0017 drops the DDL half) and the writer
    (`finance_service.upsert_revenue_snapshot`) now writes an explicit NULL for every column a
    caller did not supply.  A caller-supplied `0` is still stored as a measured zero — that is a
    different state and it must survive.
    """

    __tablename__ = "revenue_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    mrr_cents: Mapped[int | None] = mapped_column(Integer)
    arr_cents: Mapped[int | None] = mapped_column(Integer)
    active_subscribers: Mapped[int | None] = mapped_column(Integer)
    churned_today: Mapped[int | None] = mapped_column(Integer)
    new_today: Mapped[int | None] = mapped_column(Integer)
    total_revenue_month_cents: Mapped[int | None] = mapped_column(Integer)
    stripe_balance_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExpenseRecord(Base):
    __tablename__ = "expense_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str | None] = mapped_column(
        String(10), server_default="usd", default="usd"
    )
    description: Mapped[str | None] = mapped_column(Text)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InvoiceDunningClaimResult(Base):
    """The APPEND-ONLY finalization of one dunning claim (DT-05, migration 0017).

    Why this table exists.  The ladder's at-most-once ledger used to be a mutable row: the tick
    INSERTed an `invoice_dunning_claim` activity row (`state=pending`) before the provider call and
    then UPDATE'd that row to `state=sent|refused` afterwards.  `activity_log` is append-only on
    PostgreSQL (migration 0002's `trg_activity_log_no_update` trigger refuses every UPDATE), so on
    the production engine the finalize/release UPDATE raised, the step never finalized and a
    refused step was consumed forever.  SQLite has no such trigger, which is why every unit test
    stayed green.

    The fix keeps the audit table append-only: one row here records the outcome of one claim
    (`claim_row_id` = the `activity_log` claim row it closes, UNIQUE — a claim can be finalized at
    most once).  `state` is `sent` or `refused`; a `sent` result consumes its step (and every rung
    it names in `detail.superseded`), a `refused` one consumes nothing — the V9 refusal contract.
    There is no `pending` state and no UPDATE path: pending lives in the claim activity row.

    NO FOREIGN KEY to `invoices`/`activity_log` on purpose: this is the durable ledger that must
    survive a retention delete of the audit feed (and an invoice row delete), exactly like the
    activity ledger it replaces.  `detail` carries the same JSON the claim row used to hold
    (`superseded`, `status_flipped_to`, `message` for a refusal).
    """

    __tablename__ = "invoice_dunning_claim_results"
    __table_args__ = (
        UniqueConstraint("claim_row_id", name="uq_invoice_dunning_claim_results_claim"),
        Index("ix_invoice_dunning_claim_results_invoice", "invoice_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: the `activity_log.id` of the `invoice_dunning_claim` row this result finalizes.
    claim_row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str] = mapped_column(String(50), nullable=False)
    #: `sent` or `refused` — the only two outcomes the ladder can record.
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(255))
    #: the named `E_*` code a refusal carries; NULL for a delivered step.
    code: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

