"""company_config - the single company profile row."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
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
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CompanyConfig id={self.id} name={self.name!r}>"
