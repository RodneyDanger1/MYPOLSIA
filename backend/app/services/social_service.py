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
    E_DB,
    E_NOT_FOUND,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.models.social import SocialEngagement, SocialPost
from app.services.query_helpers import fetch_scalar, fetch_scalars


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


async def record_published_posts(db: AsyncSession, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the posts a social_media run REALLY published — the worker calls this after `run()`.

    WHY this lives here: agents have no DB session, and only the worker can write the
    `social_posts` rows that `channel_service` counts the twitter/linkedin caps against
    (A2 F-04). A row may exist ONLY for the path where the provider integration really returned
    an id: `posted` items without `tweet_id` were never sent, so they are counted as invalid and
    never written ("not sent" and "sent" must stay distinguishable). Deduped by
    `(platform, tweet_id)` so a retried run cannot double-count. `flush` only — the worker owns
    the commit. Never raises: any failure comes back in the report's `error` field.
    """
    report: dict[str, Any] = {
        "ok": True,
        "posts_written": 0,
        "duplicates_skipped": 0,
        "skipped_invalid": 0,
        "error": None,
    }
    try:
        for item in (result or {}).get("posted") or []:
            if not isinstance(item, dict):
                report["skipped_invalid"] += 1
                continue
            tweet_id = str(item.get("tweet_id") or "").strip()
            if not tweet_id:
                # No provider id = the integration never confirmed a send; never counted.
                report["skipped_invalid"] += 1
                continue
            platform = str(item.get("platform") or "twitter")
            existing = await fetch_scalar(
                db,
                select(SocialPost.id).where(
                    SocialPost.platform == platform, SocialPost.tweet_id == tweet_id
                ),
                what=f"social post ({platform}, {tweet_id})",
            )
            if existing is not None:
                report["duplicates_skipped"] += 1
                continue
            db.add(
                SocialPost(
                    platform=platform,
                    content=str(item.get("content") or ""),
                    status="published",
                    tweet_id=tweet_id,
                    published_at=utcnow(),
                )
            )
            await db.flush()
            report["posts_written"] += 1
    except PolsiaError as exc:
        report["ok"] = False
        report["error"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 — the worker contract is a report, never a raise
        refusal = PolsiaError(
            E_DB,
            f"could not persist social posts: {type(exc).__name__}: {exc}",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"agent": "social_media", "posts_written": report["posts_written"]},
        )
        report["ok"] = False
        report["error"] = {"code": refusal.code, "message": refusal.message}
    return report


__all__ = [
    "create_post",
    "get_post",
    "list_engagements",
    "list_posts",
    "mark_published",
    "record_published_posts",
]
