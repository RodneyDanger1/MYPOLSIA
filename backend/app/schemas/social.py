from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class SocialPostResponse(ORMModel):
    id: int
    platform: str
    content: str
    status: str
    tweet_id: str | None = None
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    engagement: dict[str, Any] | None = None
    created_at: datetime | None = None


class SocialEngagementResponse(ORMModel):
    id: int
    mention_id: str | None = None
    author_handle: str | None = None
    content: str | None = None
    our_reply: str | None = None
    reply_id: str | None = None
    created_at: datetime | None = None


class ProspectResponse(ORMModel):
    id: int
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None
    source: str | None = None
    status: str
    created_at: datetime | None = None


class AdCampaignResponse(ORMModel):
    id: int
    platform: str
    external_id: str | None = None
    name: str
    goal: str | None = None
    status: str
    daily_budget_usd: float
    total_spent_usd: float
    created_at: datetime | None = None
