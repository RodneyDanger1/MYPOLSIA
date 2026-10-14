"""Social posts.

Index-friendly: newest-first with the `(created_at DESC, id DESC)` tie-break, so a
post inserted in the same second as another cannot reorder the feed and the sort is
served by an index on that pair (REQUESTS #3).  Failure paths are named refusals: a
failed read is `E_DB` — never a `None`/`[]` that reads like success.

T13/D20 removals: `create_post`, `mark_published` and `list_engagements` were labelled
zero-callers (fix-5 §4) with no consumer in the shipped surface — no compose route, no status
PATCH, no `/social/engagements` read — and `mark_published` carried a recorded RISK if wired
as-is (it accepts `tweet_id=None`, so it could write a `status='published'` row with no provider
id into the very table the twitter/linkedin caps count, bypassing `record_published_posts`'
"a row exists only where the provider returned an id" contract).  The shipped social write path
is agent -> worker -> `record_published_posts`; that is what stays.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import (
    E_DB,
    ERROR_HINTS,
    PolsiaError,
)
from app.core.timeutil import dialect_name
from app.models.social import SocialPost
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


async def get_post(db: AsyncSession, post_id: int) -> SocialPost | None:
    """One post by id, or `None`; a failed read is a named `E_DB` (L2-06 — was a raw
    `db.execute`).

    NOTE (T13): `mark_published` — its only production-shaped caller — was removed as a labelled
    zero-caller (fix-5 §4), so this read is currently consumed by tests only.  It is kept (not in
    the ticket's eight) because it is the natural single-row read a post-detail view would use,
    and it is an L2-06 `E_DB`-discipline pin; the residual is recorded in the lane REQUESTS.
    """
    return await fetch_scalar(
        db, select(SocialPost).where(SocialPost.id == post_id), what=f"social post {post_id}"
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
            # NAMING (U5/P1): `tweet_id` is the platform-GENERIC provider id — the column name
            # predates the multi-platform dispatch (it now carries X tweet ids, LinkedIn
            # `urn:li:share:` ids and Meta graph ids alike) and is kept because renaming the
            # column needs a migration; the dedupe key `(platform, tweet_id)` is what makes it
            # per-platform correct. A future rename is a schema ticket, never a behavior change.
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
    "get_post",
    "list_posts",
    "record_published_posts",
]
