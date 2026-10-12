"""GET /api/v1/social/posts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.social import SocialPostResponse
from app.services.social_service import list_posts

router = APIRouter(prefix="/social", tags=["social"], dependencies=[Depends(verify_api_key)])


@router.get("/posts", response_model=list[SocialPostResponse])
async def social_posts(
    status_filter: str | None = Query(default=None, alias="status"),
    platform: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[SocialPostResponse]:
    with refusal_guard():
        posts = await list_posts(db, status=status_filter, platform=platform, limit=limit)
    return [SocialPostResponse.model_validate(post) for post in posts]
