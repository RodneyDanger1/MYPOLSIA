from __future__ import annotations

from datetime import datetime
from typing import Any

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
    """The `/ads` page's row shape.  `status`/`daily_budget_usd`/`total_spent_usd` are NULLABLE in
    the frozen schema (`models/ads.py:21-30`; `0001`'s `CREATE TABLE` carries `DEFAULT`s but no
    `NOT NULL`, and on PostgreSQL `CHECK(NULL)` passes), and raw-SQL/ops writers are an accepted
    path into this table (`e2e/ads.spec.ts`).  Declaring them required turned ONE schema-legal NULL
    row into an unnamed 500 for the whole list (R26 F1).  `None` here means "not recorded" — never
    a fabricated `0`/`"active"`; the frontend renders the dash for it.
    """

    id: int
    platform: str
    external_id: str | None = None
    name: str
    goal: str | None = None
    status: str | None = None
    daily_budget_usd: float | None = None
    total_spent_usd: float | None = None
    created_at: datetime | None = None
