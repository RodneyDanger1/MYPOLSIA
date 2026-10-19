"""GET /api/v1/social/posts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.status_filters import SOCIAL_POST_STATUSES, require_status
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
    """The post queue, newest first.

    V20/L1 (card L2-08): an unknown `status` is the named `E_VALIDATION` 422 the sibling
    status-filtered reads answer (`ck_social_posts_status`'s vocabulary) — this route answered
    `200 []` at 8fa2b38.  No total header here: the card (L2-07) does not name this route and its
    page size is the whole queue in practice; declaring a total is a separate, additive change.
    """
    with refusal_guard():
        require_status(status_filter, SOCIAL_POST_STATUSES, what="social post")
        posts = await list_posts(db, status=status_filter, platform=platform, limit=limit)
    return [SocialPostResponse.model_validate(post) for post in posts]
