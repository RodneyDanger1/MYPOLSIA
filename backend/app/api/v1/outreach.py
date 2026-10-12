"""GET /api/v1/outreach/prospects (consumed by the frontend /outreach page)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.social import ProspectResponse
from app.services.outreach_service import list_prospects

router = APIRouter(prefix="/outreach", tags=["outreach"], dependencies=[Depends(verify_api_key)])


@router.get("/prospects", response_model=list[ProspectResponse])
async def outreach_prospects(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[ProspectResponse]:
    with refusal_guard():
        prospects = await list_prospects(db, status=status_filter, limit=limit)
    return [ProspectResponse.model_validate(prospect) for prospect in prospects]
