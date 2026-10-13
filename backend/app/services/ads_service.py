"""Ad campaigns/metrics.

`list_metrics` for a campaign that does not exist used to return `[]` — the caller
cannot tell "this campaign has no metrics yet" from "there is no such campaign".
It now asks the cheap existence question first and raises `E_NOT_FOUND`.

Read ordering is deterministic (`date DESC, id DESC` for metrics; `id DESC` for
campaigns, which have no per-day series) and every read failure is `E_DB`.
`AdMetric.spend_usd` is `Numeric(14, 4)` (alembic `0003_money_exact`); money that must
be compared rather than merely displayed is handled as `Decimal`
(`finance_service.money`) — the column itself is exact at rest now.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.ads import AdCampaign, AdMetric
from app.services.query_helpers import fetch_scalars


async def list_campaigns(
    db: AsyncSession,
    platform: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[AdCampaign]:
    stmt = select(AdCampaign)
    if platform is not None:
        stmt = stmt.where(AdCampaign.platform == platform)
    if status is not None:
        stmt = stmt.where(AdCampaign.status == status)
    stmt = stmt.order_by(AdCampaign.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="ad campaigns")


async def create_campaign(
    db: AsyncSession, platform: str, name: str, **values: Any
) -> AdCampaign:
    if not (platform or "").strip() or not (name or "").strip():
        raise PolsiaError(
            E_VALIDATION,
            "ad campaign requires a non-empty platform and name",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"platform": platform, "name": name},
        )
    campaign = AdCampaign(platform=platform, name=name, **values)
    db.add(campaign)
    await db.flush()
    return campaign


async def get_campaign(db: AsyncSession, campaign_id: int) -> AdCampaign | None:
    result = await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))
    return result.scalar_one_or_none()


async def list_metrics(db: AsyncSession, campaign_id: int, limit: int = 90) -> list[AdMetric]:
    """Per-day metrics, newest first.  Unknown campaign ⇒ `E_NOT_FOUND`."""
    if await get_campaign(db, campaign_id) is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"ad campaign {campaign_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"campaign_id": campaign_id},
        )
    return await fetch_scalars(
        db,
        select(AdMetric)
        .where(AdMetric.campaign_id == campaign_id)
        .order_by(AdMetric.date.desc(), AdMetric.id.desc())
        .limit(limit),
        what="ad metrics",
    )


__all__ = ["create_campaign", "get_campaign", "list_campaigns", "list_metrics"]
