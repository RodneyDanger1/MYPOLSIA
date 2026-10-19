"""GET /api/v1/ads/campaigns + /ads/campaigns/{id}/metrics (the frontend /ads page + the
stored-metrics read V19/L2 wired)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.status_filters import AD_CAMPAIGN_STATUSES, require_status
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.models.ads import AdCampaign
from app.schemas.social import AdCampaignResponse, AdMetricResponse
from app.services.ads_service import list_campaigns, list_metrics

router = APIRouter(prefix="/ads", tags=["ads"], dependencies=[Depends(verify_api_key)])


@router.get("/campaigns", response_model=list[AdCampaignResponse])
async def ads_campaigns(
    response: Response,
    platform: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[AdCampaignResponse]:
    """The campaign list, newest first.

    V20/L1 (cards L2-08 + L2-07): an unknown `status` is the named `E_VALIDATION` 422 every
    sibling status-filtered read answers — this route answered `200 []` at 8fa2b38, the same hole
    L2-08 names on `/runs` and `/outreach/prospects` — and the full match count rides
    `X-Polsia-Total` so a full page cannot read as the whole table.  The body stays a bare array.
    """
    with refusal_guard():
        require_status(status_filter, AD_CAMPAIGN_STATUSES, what="ad campaign")
        campaigns = await list_campaigns(db, platform=platform, status=status_filter, limit=limit)
        conditions = []
        if platform is not None:
            conditions.append(AdCampaign.platform == platform)
        if status_filter is not None:
            conditions.append(AdCampaign.status == status_filter)
        total = await declared_total(db, AdCampaign, conditions, what="ad campaigns total")
    response.headers[TOTAL_HEADER] = str(total)
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

    V19/L2 — the V17/L3-08 gap is CLOSED (it was carried into V18): `ads_service.list_metrics`
    was reachable only from tests while `record_ads_run` wrote real `ad_metrics` rows from every
    completed ads run — the stored measurement had no product consumer.  This route IS that
    consumer; an unknown campaign is the service's named `E_NOT_FOUND` (a refusal, never an
    empty list that reads like
    "no metrics yet").
    """
    with refusal_guard():
        metrics = await list_metrics(db, campaign_id, limit=limit)
    return [AdMetricResponse.model_validate(metric) for metric in metrics]
