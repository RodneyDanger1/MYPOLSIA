"""ad_campaigns + ad_metrics (module name INFERRED - unreferenced)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    goal: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="active", default="active"
    )
    # money is exact at rest: NUMERIC(14, 4) (A-T17 / alembic 0003).
    daily_budget_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), server_default="0", default=0
    )
    total_spent_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), server_default="0", default=0
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdMetric(Base):
    __tablename__ = "ad_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ad_campaigns.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    clicks: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    conversions: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    spend_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), server_default="0", default=0
    )
    # ctr/cpc/roas are RATIOS, not money — they stay Float (per-column decision
    # recorded in alembic/versions/0003_money_exact.py; 0002 derives them).
    ctr: Mapped[float | None] = mapped_column(Float, server_default="0", default=0)
    cpc: Mapped[float | None] = mapped_column(Float, server_default="0", default=0)
    roas: Mapped[float | None] = mapped_column(Float, server_default="0", default=0)
