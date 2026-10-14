"""company_config - the single company profile row."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class CompanyConfig(Base):
    __tablename__ = "company_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mission: Mapped[str | None] = mapped_column(Text)
    vision: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    target_market: Mapped[str | None] = mapped_column(Text)
    value_prop: Mapped[str | None] = mapped_column(Text)
    pricing_model: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    goals: Mapped[dict[str, Any] | None] = mapped_column(json_type(), default=dict)
    kpis: Mapped[dict[str, Any] | None] = mapped_column(json_type(), default=dict)
    website_url: Mapped[str | None] = mapped_column(String(512))
    github_repo: Mapped[str | None] = mapped_column(String(512))
    product_type: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str | None] = mapped_column(
        String(50), server_default="UTC", default="UTC"
    )
    daily_cycle_hour: Mapped[int | None] = mapped_column(
        Integer, server_default="6", default=6
    )
    # ── Night Task + owner notifications (U5 migration 0007; frozen names) ──────────────────
    # Every bool is NOT NULL with a SERVER default so existing rows migrate with a materialised
    # value (FALSE — the live default; night task and every notification toggle ship OFF).
    # `night_task_hour` stays NULLABLE: NULL means "unset", and the night-task tick resolves the
    # documented 23:00 default at read time (never a stored fabrication).  `night_task_limit` is
    # NOT NULL default 3 with the wire validator holding 1..5.
    night_task_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    night_task_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3", default=3
    )
    night_task_hour: Mapped[int | None] = mapped_column(Integer)
    notification_email: Mapped[str | None] = mapped_column(String(255))
    notify_on_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    notify_on_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    notify_on_awaiting_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    notify_on_run_requeued: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    # ── Owner profile + sender identity + readiness (V8 migration 0008; frozen names) ──────
    # Every NOT NULL column carries a SERVER default (the 0007 doctrine): that is what lets
    # SQLite ALTER TABLE ... ADD COLUMN accept it AND materialises a value for every EXISTING
    # row.  The six text fields default to '' — '' is their honest "not configured".  The two
    # JSON fields default to the empty list/object so consumers (readiness checklist, cycle
    # report) never handle NULL for them.  `cycle_report_email` (the cycle-report email opt-in)
    # and `dispatch_paused` (C5's pause feature rides this column) are NOT NULL server default
    # FALSE.  `owner_hours_per_week` / `owner_budget_usd` / `confirmed_idea` stay NULLABLE:
    # NULL is "no value", and money keeps the A-T17 "NULL when unmeasured" rule.
    cycle_report_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    sender_legal_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    sender_postal_address: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    owner_skills: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    owner_hours_per_week: Mapped[int | None] = mapped_column(Integer)
    owner_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    owner_goal: Mapped[str] = mapped_column(Text, nullable=False, server_default="", default="")
    risk_tolerance: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    channels_ready: Mapped[list[str]] = mapped_column(
        json_type(), nullable=False, server_default="[]", default=list
    )
    market_locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    confirmed_idea: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    checklist_state: Mapped[dict[str, Any]] = mapped_column(
        json_type(), nullable=False, server_default="{}", default=dict
    )
    dispatch_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CompanyConfig id={self.id} name={self.name!r}>"
