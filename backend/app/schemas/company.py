from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class CompanyConfigResponse(ORMModel):
    id: int
    name: str
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    pricing_model: dict[str, Any] | None = None
    goals: dict[str, Any] | None = None
    kpis: dict[str, Any] | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str = "UTC"
    daily_cycle_hour: int = 6
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CompanyConfigUpdate(BaseModel):
    name: str | None = None
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    pricing_model: dict[str, Any] | None = None
    goals: dict[str, Any] | None = None
    kpis: dict[str, Any] | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str | None = None
    daily_cycle_hour: int | None = None
