"""Social posts/engagements.

Index-friendly: newest-first with the `(created_at DESC, id DESC)` tie-break, so a
post inserted in the same second as another cannot reorder the feed and the sort is
served by an index on that pair (REQUESTS #3).  Failure paths are named refusals:
a missing post is `E_NOT_FOUND`, malformed input `E_VALIDATION`, a failed read
`E_DB` — never a `None`/`[]` that reads like success.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import (
    E_NOT_FOUND,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.models.social import SocialEngagement, SocialPost
from app.services.query_helpers import fetch_scalars


async def list_posts(
    db: AsyncSession,
    status: str | None = None,
    platform: str | None = None,
    limit: int = 50,
) -> list[SocialPost]:
    stmt = select(SocialPost)
    if status is not None:
        stmt = stmt.where(SocialPost.status == status)
    if platform is not None:
        stmt = stmt.where(SocialPost.platform == platform)
    stmt = (
        stmt.order_by(SocialPost.created_at.desc(), SocialPost.id.desc()).limit(limit)
    )
    return await fetch_scalars(db, stmt, what="social posts")


async def create_post(
    db: AsyncSession,
    content: str,
    platform: str = "twitter",
    status: str = "draft",
    scheduled_for: datetime | None = None,
    engagement: dict[str, Any] | None = None,
) -> SocialPost:
    if not content or not content.strip():
        raise PolsiaError(
            E_VALIDATION,
            "post content must not be empty",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"platform": platform},
        )
    post = SocialPost(
        platform=platform,
        content=content,
        status=status,
        scheduled_for=scheduled_for,
        engagement=engagement,
    )
    db.add(post)
    await db.flush()
    return post


async def get_post(db: AsyncSession, post_id: int) -> SocialPost | None:
    result = await db.execute(select(SocialPost).where(SocialPost.id == post_id))
    return result.scalar_one_or_none()


async def mark_published(
    db: AsyncSession,
    post_id: int,
    tweet_id: str | None = None,
    published_at: datetime | None = None,
) -> SocialPost:
    """Mark a post published.  A post that does not exist is `E_NOT_FOUND`.

    Returning `None` here (as the original did) makes "we published nothing"
    indistinguishable from "the id was wrong" at the call site.
    """
    post = await get_post(db, post_id)
    if post is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"social post {post_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"post_id": post_id},
        )
    post.status = "published"
    post.tweet_id = tweet_id
    post.published_at = published_at or utcnow()
    await db.flush()
    return post


async def list_engagements(db: AsyncSession, limit: int = 100) -> list[SocialEngagement]:
    return await fetch_scalars(
        db,
        select(SocialEngagement)
        .order_by(SocialEngagement.created_at.desc(), SocialEngagement.id.desc())
        .limit(limit),
        what="social engagements",
    )


__all__ = [
    "create_post",
    "get_post",
    "list_engagements",
    "list_posts",
    "mark_published",
]
