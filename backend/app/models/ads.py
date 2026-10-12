"""ad_campaigns + ad_metrics (module name INFERRED - unreferenced)."""
from __future__ import annotations

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, func

from app.core.database import Base


class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    id = Column(Integer, primary_key=True)
    platform = Column(String(50), nullable=False)
    external_id = Column(String(255))
    name = Column(String(512), nullable=False)
    goal = Column(String(255))
    status = Column(String(50), server_default="active", default="active")
    daily_budget_usd = Column(Float, server_default="0", default=0)
    total_spent_usd = Column(Float, server_default="0", default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdMetric(Base):
    __tablename__ = "ad_metrics"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("ad_campaigns.id"), nullable=False)
    date = Column(Date, nullable=False)
    impressions = Column(Integer, server_default="0", default=0)
    clicks = Column(Integer, server_default="0", default=0)
    conversions = Column(Integer, server_default="0", default=0)
    spend_usd = Column(Float, server_default="0", default=0)
    ctr = Column(Float, server_default="0", default=0)
    cpc = Column(Float, server_default="0", default=0)
    roas = Column(Float, server_default="0", default=0)
