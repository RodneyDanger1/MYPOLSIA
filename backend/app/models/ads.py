"""ad_campaigns + ad_metrics — consumed by app.services.ads_service (persist + reads) and
app.services.finance_service's ad-spend reads; DDL-pinned by tests/unit/test_models_schema_frozen.py
(M3: the old "(module name INFERRED - unreferenced)" header was stale — only models/competitor.py
is genuinely unreferenced by services)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The derived-ratio expressions, copied verbatim from `alembic/versions/0002_queue_and_perf.py`
#: (`_CTR_EXPR` / `_CPC_EXPR`) and `0014_ads_nullable_revenue.py` (`_ROAS_EXPR`).  BOTH engines
#: hold these three columns `GENERATED ALWAYS AS (<expr>)` (0002:233-245 drops the writable columns
#: and re-adds them; 0003:71/79 rebuilds the table; 0014 swaps the roas expression to the
#: measured-only rule), so the ORM must declare them computed.
#: `Computed(expr)` without `persisted` is the dialect-honest form (measured): SQLite renders the
#: expression with its VIRTUAL default, PostgreSQL's dialect renders the STORED keyword it requires.
_CTR_EXPR = "CASE WHEN impressions > 0 THEN round(CAST(CAST(clicks AS REAL) / impressions AS NUMERIC), 6) ELSE 0 END"
_CPC_EXPR = "CASE WHEN clicks > 0 THEN round(CAST(CAST(spend_usd AS REAL) / clicks AS NUMERIC), 6) ELSE 0 END"
_ROAS_EXPR = (
    "CASE WHEN revenue_usd IS NOT NULL AND spend_usd > 0 THEN "
    "round(CAST(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL) AS NUMERIC), 6) END"
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

    # migration 0006 (D18/T19): the provider's own id is the honest natural key, scoped by platform.
    # PARTIAL (`WHERE external_id IS NOT NULL`): legacy/manual rows without a provider ref stay legal
    # and NULLs are distinct in a unique index on both dialects (SQLite partial-index doc: "A partial
    # index definition may include the UNIQUE keyword ... requires every entry in the index to be
    # unique"; PostgreSQL: "When the WHERE clause is present, a partial index is created").  Mapped
    # here for the same reason 0005's two keys are mapped next door: `create_all` must describe the
    # schema the chain ships, or the unit-test fixture hides the constraint the migration adds.
    __table_args__ = (
        Index(
            "uq_ad_campaigns_platform_external_id",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
            sqlite_where=text("external_id IS NOT NULL"),
        ),
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
    # V26/L6 (migration 0015): spend is the second measured money column and absence is a REAL
    # state too — NULLABLE with NO default of any kind.  Before 0015 the column carried
    # `DEFAULT '0'`, so an omitted spend persisted as a stored zero that no run had measured;
    # `record_ads_run` now writes NULL when the run reports none and a reported `0` stays `0`.
    spend_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # V25/L4: `revenue_usd` is the measured money column, and absence is a REAL state now.  It is
    # NULLABLE with NO default (alembic 0014; the pre-0014 shape was `NOT NULL DEFAULT 0`, which
    # made "never measured" indistinguishable from "measured $0").  An unreported revenue is
    # stored as NULL, a reported 0 stays 0, and the API serves exactly that difference.
    revenue_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # ctr/cpc/roas are RATIOS, not money — `Numeric(14, 6)` is the type the shipped DDL holds them
    # in (0002:244), and they are GENERATED: the ORM omits them from INSERT/UPDATE and reads the
    # engine's value back (measured; an explicitly set value still reaches the INSERT and the
    # engine refuses it with `cannot INSERT into generated column "ctr"`).  Declaring them
    # writable Floats (pre-R31) made the create_all unit fixture accept an insert the migrated
    # schema rejects — the fixture hid a production failure.
    ctr: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_CTR_EXPR))
    cpc: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_CPC_EXPR))
    roas: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), Computed(_ROAS_EXPR))
