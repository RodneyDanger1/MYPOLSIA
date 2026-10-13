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

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import (
    E_DB,
    E_NOT_FOUND,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.core.timeutil import dialect_name
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
    """Insert a draft/scheduled post row (blank content is a named refusal).

    ZERO PRODUCTION CALLERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the only callers are
    `tests/unit/test_r27_social_outreach_sweep.py`; production's shipped social path is the
    agent -> worker -> `record_published_posts` chain, and no route composes a post.  Intended
    purpose: a service-layer writer for a manual compose/schedule flow.  Removal vs adoption is
    an owner decision (polsia-work/build/full-sweep/fix-5/REQUESTS.md §4).
    """
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
    """One post by id, or `None`; a failed read is a named `E_DB` (L2-06 — was a raw
    `db.execute`)."""
    return await fetch_scalar(
        db, select(SocialPost).where(SocialPost.id == post_id), what=f"social post {post_id}"
    )


async def mark_published(
    db: AsyncSession,
    post_id: int,
    tweet_id: str | None = None,
    published_at: datetime | None = None,
) -> SocialPost:
    """Mark a post published.  A post that does not exist is `E_NOT_FOUND`.

    Returning `None` here (as the original did) makes "we published nothing"
    indistinguishable from "the id was wrong" at the call site.

    ZERO PRODUCTION CALLERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the only callers are
    `tests/unit/test_r27_social_outreach_sweep.py`; the shipped writer for published rows is
    `record_published_posts` (the worker path).  RISK IF WIRED AS-IS: this sets
    `status='published'` while accepting `tweet_id=None`, so it can put a published row with
    NO provider id into `social_posts` — the table `channel_service` counts the
    twitter/linkedin caps from — bypassing `record_published_posts`' contract that "a row
    exists only where the provider really returned an id".  Removal vs adoption (and, if
    adopted, the id requirement) is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §4).
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
    """Newest-first mentions/replies read.

    ZERO PRODUCTION CALLERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the only callers are
    `tests/unit/test_r27_social_outreach_sweep.py`; no route serves social engagements (the
    would-be wire model `SocialEngagementResponse` carries its own zero-consumer label, L1-05).
    Intended purpose: the engagement feed read for a mentions/replies page.  Removal vs adoption
    is an owner decision (polsia-work/build/full-sweep/fix-5/REQUESTS.md §4).
    """
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
    `(platform, tweet_id)` so a retried run cannot double-count — and since migration 0005 that
    key is a REAL partial unique index, so two writers that both miss the SELECT cannot both
    insert: the write is one `INSERT ... ON CONFLICT DO NOTHING RETURNING id` (the
    `record_stripe_event` shape) whose empty RETURNING means the raced row already exists and is
    counted as `duplicates_skipped`. `flush` only — the worker owns the commit. Never raises:
    any failure comes back in the report's `error` field.
    """
    report: dict[str, Any] = {
        "ok": True,
        "posts_written": 0,
        "duplicates_skipped": 0,
        "skipped_invalid": 0,
        "error": None,
    }
    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
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
            # ONE statement (the `record_stripe_event` shape): the natural key is a REAL
            # partial unique index (migration 0005), so a concurrent writer that missed the
            # SELECT above cannot double-insert — the conflict resolves atomically in the
            # database and `RETURNING id` comes back empty.  No savepoint: a savepoint
            # released as the OUTERMOST transaction commits on SQLite (pysqlite legacy
            # control), which would break the "flush only — the worker owns the commit"
            # contract; ON CONFLICT has no such edge.
            created_id = (
                await db.execute(
                    insert_fn(SocialPost)
                    .values(
                        platform=platform,
                        content=str(item.get("content") or ""),
                        status="published",
                        tweet_id=tweet_id,
                        published_at=utcnow(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["platform", "tweet_id"],
                        index_where=text("tweet_id IS NOT NULL"),
                    )
                    .returning(SocialPost.id)
                )
            ).scalars().one_or_none()
            if created_id is None:
                # The raced row is the natural key's row: it IS this send, recorded once.
                report["duplicates_skipped"] += 1
                continue
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
