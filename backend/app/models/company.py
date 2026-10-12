"""company_config - the single company profile row."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from app.core.database import Base
from app.models.types import json_type


class CompanyConfig(Base):
    __tablename__ = "company_config"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    mission = Column(Text)
    vision = Column(Text)
    description = Column(Text)
    target_market = Column(Text)
    value_prop = Column(Text)
    pricing_model = Column(json_type())
    goals = Column(json_type(), default=dict)
    kpis = Column(json_type(), default=dict)
    website_url = Column(String(512))
    github_repo = Column(String(512))
    product_type = Column(String(100))
    industry = Column(String(100))
    timezone = Column(String(50), server_default="UTC", default="UTC")
    daily_cycle_hour = Column(Integer, server_default="6", default=6)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CompanyConfig id={self.id} name={self.name!r}>"
