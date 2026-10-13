from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.common import ORMModel


class SocialPostResponse(ORMModel):
    """`platform`/`status` are NULLABLE in the frozen schema (`models/social.py`; `0001`'s
    `CREATE TABLE social_posts` carries `server_default`s but no `NOT NULL`), so a raw-SQL/ops
    writer can leave them NULL — declaring them required turned ONE such row into a bare, codeless
    500 for the whole `/social/posts` list (L1-11).  `None` means "not recorded" and is served as
    null; the social page renders the dash / gray badge for it (same rule as `AdCampaignResponse`).
    """

    id: int
    platform: str | None = None
    content: str
    status: str | None = None
    tweet_id: str | None = None
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    engagement: dict[str, Any] | None = None
    created_at: datetime | None = None


class SocialEngagementResponse(ORMModel):
    """The would-be `/social/engagements` row shape.

    ZERO CONSUMERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — no route serves
    engagements, and the service read it would serialise
    (`social_service.list_engagements`) is itself a labelled zero-caller (L2-04).  Wiring the
    pair vs deleting them is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §2).
    """

    id: int
    mention_id: str | None = None
    author_handle: str | None = None
    content: str | None = None
    our_reply: str | None = None
    reply_id: str | None = None
    created_at: datetime | None = None


class ProspectResponse(ORMModel):
    """`status` is NULLABLE in the frozen schema (`models/outreach.py`; `0001`'s
    `CREATE TABLE prospects` carries `server_default='new'` but no `NOT NULL`), so a raw-SQL/ops
    writer can leave it NULL — declaring it required turned ONE such row into a bare, codeless 500
    for the whole `/outreach/prospects` list (L1-11).  `None` means "not recorded" and is served as
    null; the outreach page renders the dash for it.
    """

    id: int
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None
    source: str | None = None
    status: str | None = None
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
