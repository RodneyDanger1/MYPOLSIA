"""ad_campaigns + ad_metrics (module name INFERRED - unreferenced)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The derived-ratio expressions, copied verbatim from `alembic/versions/0002_queue_and_perf.py`
#: (`_CTR_EXPR` / `_CPC_EXPR` / `_ROAS_EXPR`).  BOTH engines hold these three columns
#: `GENERATED ALWAYS AS (<expr>)` (0002:233-245 drops the writable columns and re-adds them;
#: 0003:71/79 rebuilds the table in the same shape), so the ORM must declare them computed.
#: `Computed(expr)` without `persisted` is the dialect-honest form (measured): SQLite renders the
#: expression with its VIRTUAL default, PostgreSQL's dialect renders the STORED keyword it requires.
_CTR_EXPR = "CASE WHEN impressions > 0 THEN round(CAST(clicks AS REAL) / impressions, 6) ELSE 0 END"
_CPC_EXPR = "CASE WHEN clicks > 0 THEN round(CAST(spend_usd AS REAL) / clicks, 6) ELSE 0 END"
_ROAS_EXPR = (
    "CASE WHEN spend_usd > 0 THEN round(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL), 6) "
    "ELSE 0 END"
)


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
    # `revenue_usd` is the real money column both engines hold — `NOT NULL` (0002:203-207 adds it
    # `nullable=False`; 0003:71 re-states `NUMERIC(14, 4) DEFAULT '0' NOT NULL`) — and the input
    # `roas` is derived from.  It was unmapped, so no ORM insert could ever write the shipped
    # schema without tripping the NOT NULL (R26 F2).
    revenue_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), server_default="0", default=0, nullable=False
    )
    # ctr/cpc/roas are RATIOS, not money — `Numeric(14, 6)` is the type the shipped DDL holds them
    # in (0002:244), and they are GENERATED: the ORM omits them from INSERT/UPDATE and reads the
    # engine's value back (measured; an explicitly set value still reaches the INSERT and the
    # engine refuses it with `cannot INSERT into generated column "ctr"`).  Declaring them
    # writable Floats (pre-R31) made the create_all unit fixture accept an insert the migrated
    # schema rejects — the fixture hid a production failure.
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_CTR_EXPR))
    cpc: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_CPC_EXPR))
    roas: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_ROAS_EXPR))
