"""GET /api/v1/ads/campaigns + /ads/campaigns/{id}/metrics (the frontend /ads page + the
stored-metrics read V19/L2 wired)."""
from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.status_filters import AD_CAMPAIGN_STATUSES, require_status
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.models.ads import AdCampaign, AdMetric
from app.schemas.social import AdCampaignResponse, AdMetricResponse
from app.services.ads_service import list_campaigns, list_metrics

router = APIRouter(prefix="/ads", tags=["ads"], dependencies=[Depends(verify_api_key)])


def _wire_float(value: Any) -> float | None:
    """A stored Numeric as a finite wire float, or None — never `NaN`, never an exception."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metric_response(metric: AdMetric) -> AdMetricResponse:
    """One stored metric day with revenue/ROAS served on the measured-only rule.

    `revenue_usd` is served when it is a real positive measurement.  The frozen DDL holds it
    `NUMERIC(14,4) DEFAULT '0' NOT NULL` (alembic 0002:203-207 / 0003:71), so an unreported
    revenue and a measured zero are the SAME stored value and no backfill may invent the
    difference.  Until the column can carry NULL, a stored 0 is served as `null` (the honest
    dash): the route will not print a `$0.00` it cannot certify, and it will never print the
    generated column's `0` ROAS for a row whose revenue was never measured.

    `roas` is therefore computed HERE — only when BOTH revenue and spend are measured and spend
    is a usable denominator — instead of echoing `ad_metrics.roas`, whose GENERATED expression
    turns an absent numerator into `0`.
    """
    revenue = _wire_float(metric.revenue_usd)
    spend = _wire_float(metric.spend_usd)
    measured_revenue = revenue if revenue is not None and revenue > 0 else None
    roas = (
        round(measured_revenue / spend, 6)
        if measured_revenue is not None and spend is not None and spend > 0
        else None
    )
    return AdMetricResponse(
        id=metric.id,
        campaign_id=metric.campaign_id,
        date=metric.date,
        impressions=metric.impressions,
        clicks=metric.clicks,
        conversions=metric.conversions,
        spend_usd=spend,
        revenue_usd=measured_revenue,
        ctr=_wire_float(metric.ctr),
        cpc=_wire_float(metric.cpc),
        roas=roas,
    )


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

    V24/L4 — the response is built row-by-row (`_metric_response`) so revenue is the stored
    measurement and ROAS is derived only from the measured revenue/spend pair; the raw
    `ad_metrics.roas` GENERATED column (0 when revenue is absent) is deliberately not served.
    """
    with refusal_guard():
        metrics = await list_metrics(db, campaign_id, limit=limit)
    return [_metric_response(metric) for metric in metrics]
