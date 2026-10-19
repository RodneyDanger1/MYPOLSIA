from __future__ import annotations

from datetime import date
from typing import Any

from app.schemas.common import ORMModel, UtcDatetime


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
    scheduled_for: UtcDatetime | None = None
    published_at: UtcDatetime | None = None
    engagement: dict[str, Any] | None = None
    created_at: UtcDatetime | None = None


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
    created_at: UtcDatetime | None = None


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
    created_at: UtcDatetime | None = None


class AdMetricResponse(ORMModel):
    """One stored `ad_metrics` day (V19/L2 — the read `list_metrics` never had).

    WHY: the R33 writer stores each completed ads run's `metrics[]` as `AdMetric` rows, but the
    API exposed NO read for them — `list_metrics` had no module-external consumer and was
    V17/L3-08/V18 DEBT.  The write is real; a stored measurement nobody can read is the dead
    half of the seam.  Money is served as float (the repo's `*_usd` wire convention; the column
    stays `NUMERIC(14,4)` exact at rest); `ctr`/`cpc`/`roas` are the engines' GENERATED columns,
    `null` when the denominator is zero/absent (never a fabricated ratio).
    """

    id: int
    campaign_id: int
    date: date
    impressions: int | None = None
    clicks: int | None = None
    conversions: int | None = None
    spend_usd: float | None = None
    revenue_usd: float | None = None
    ctr: float | None = None
    cpc: float | None = None
    roas: float | None = None
