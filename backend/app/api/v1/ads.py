"""GET /api/v1/ads/campaigns + /ads/campaigns/{id}/metrics (the frontend /ads page + the
stored-metrics read V19/L2 wired)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.social import AdCampaignResponse, AdMetricResponse
from app.services.ads_service import list_campaigns, list_metrics

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


@router.get("/campaigns/{campaign_id}/metrics", response_model=list[AdMetricResponse])
async def ads_campaign_metrics(
    # S4-F3: the same int64 upper bound the sibling id routes carry — an id past the storage
    # range is caller input (422), not a driver fault.
    campaign_id: int = Path(ge=1, le=2**63 - 1),
    limit: int = Query(default=90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[AdMetricResponse]:
    """Per-day metrics for ONE campaign, newest first — the read the R33 writer feeds.

    V19/L2 (closes V17/L3-08 + V18 DEBT): `ads_service.list_metrics` was reachable only from
    tests while `record_ads_run` wrote real `ad_metrics` rows from every completed ads run —
    the stored measurement had no product consumer.  This is that consumer; an unknown campaign
    is the service's named `E_NOT_FOUND` (a refusal, never an empty list that reads like
    "no metrics yet").
    """
    with refusal_guard():
        metrics = await list_metrics(db, campaign_id, limit=limit)
    return [AdMetricResponse.model_validate(metric) for metric in metrics]
