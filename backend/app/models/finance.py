"""stripe_events + revenue_snapshots + expense_records."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Integer, String, Text, func
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
    __tablename__ = "revenue_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    mrr_cents: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    arr_cents: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    active_subscribers: Mapped[int | None] = mapped_column(
        Integer, server_default="0", default=0
    )
    churned_today: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    new_today: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    total_revenue_month_cents: Mapped[int | None] = mapped_column(
        Integer, server_default="0", default=0
    )
    stripe_balance_cents: Mapped[int | None] = mapped_column(
        Integer, server_default="0", default=0
    )
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
