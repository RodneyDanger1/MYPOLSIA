"""GET /api/v1/ads/campaigns (consumed by the frontend /ads page)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.social import AdCampaignResponse
from app.services.ads_service import list_campaigns

router = APIRouter(prefix="/ads", tags=["ads"], dependencies=[Depends(verify_api_key)])


@router.get("/campaigns", response_model=list[AdCampaignResponse])
async def ads_campaigns(
    platform: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[AdCampaignResponse]:
    with refusal_guard():
        campaigns = await list_campaigns(db, platform=platform, status=status_filter, limit=limit)
    return [AdCampaignResponse.model_validate(campaign) for campaign in campaigns]
