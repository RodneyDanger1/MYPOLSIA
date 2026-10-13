"""activity_log writes + Redis broadcast (exactly one publish per call).

Measured facts this module encodes (lane 06, `DATA-PERF.md` §2.7 / §5.4):

* **One publish per `log_activity`** — the shipped test asserts
  `mock_redis.publish.assert_called_once()` and the channel must contain
  `polsia:activity`, so there is exactly one `publish_activity` call site and no
  read path publishes anything.
* **Bulk writes are a real path — and the cycle-summary path uses it (T13).**
  `insert one-by-one` = 3,018 ms vs `executemany` = 47 ms for 5,000 rows (raw
  executemany 33.8 ms) — a ×63.8 difference, and that measurement is why
  `log_activities` implements the one-executemany path.  Since T13 the morning cycle's
  summary write builds its per-item records (one row per plan entry it SKIPPED, reason
  named) and writes them through `log_activities` in one call
  (`celery_app/tasks/daily_cycle.py`), instead of a Python loop over `log_activity`.
  The cycle's headline announcement stays a SINGLE `log_activity`
  (`morning_plan_complete` / `evening_summary_complete`): `log_activities` deliberately
  publishes no per-row Redis event, and the live `/ws/activity` feed must still see the
  cycle.
* **Ordering needs the id tie-break.**  `created_at` has one-second resolution on
  SQLite, so two rows written in the same transaction are otherwise returned in an
  arbitrary order (measured: `order_by(created_at.desc())` returned the wrong
  order, failing the shipped test).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.database import utcnow
from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.report import ActivityLog
from app.services.query_helpers import fetch_scalars

ACTIVITY_CHANNEL = events.ACTIVITY_CHANNEL  # "polsia:activity"

_REQUIRED_KEYS = ("agent_type", "action", "summary")


async def log_activity(
    db: AsyncSession,
    agent_type: str,
    action: str,
    summary: str,
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> ActivityLog:
    """Insert an activity row, flush it, then publish ONE event to Redis.

    Publish happens *after* the flush so the event carries the row `id` and
    `created_at`, and a Redis outage cannot lose the DB row
    (`events.publish_activity` swallows and logs the Redis error).
    """
    entry = ActivityLog(
        agent_type=agent_type,
        action=action,
        summary=summary,
        level=level,
        detail=detail,
    )
    db.add(entry)
    await db.flush()

    await events.publish_activity(
        events.activity_payload(
            id=entry.id,
            agent_type=entry.agent_type,
            action=entry.action,
            summary=entry.summary,
            level=entry.level,
            created_at=entry.created_at,
            detail=entry.detail,
        )
    )
    return entry


async def log_activities(
    db: AsyncSession,
    entries: Iterable[Mapping[str, Any]],
    *,
    flush: bool = True,
) -> int:
    """Bulk-insert activity rows in ONE `executemany`.  Returns the row count.

    WIRED (T13/D20): the morning cycle's summary write calls this with its per-item records
    (one row per plan entry it SKIPPED, reason named) — the batch API instead of a Python loop
    over `log_activity`, which is the ×63.8 win the module docstring measures.  The cycle's
    headline announcement is still a SINGLE `log_activity` (see the module docstring): this
    function publishes no per-row Redis event by design, and a cycle must still announce itself
    on the live feed.

    For cycle summaries: a run that produces hundreds of rows should pay one round
    trip, not one per row (3,018 ms -> 33.8 ms per 5,000 rows, lane 06).  The whole
    batch shares one `created_at` so the feed's `(created_at DESC, id DESC)` order
    is deterministic — the batch is one moment in time, and the id breaks the tie.

    No per-row Redis publish by design: 5,000 publishes would cost more than the
    insert they announce.  The cycle announces itself with a single `log_activity`.
    """
    rows: list[dict[str, Any]] = []
    batch_at = utcnow()
    for index, entry in enumerate(entries):
        missing = [key for key in _REQUIRED_KEYS if not entry.get(key)]
        if missing:
            raise PolsiaError(
                E_VALIDATION,
                f"activity entry #{index} is missing required field(s): {', '.join(missing)}",
                how_to_fix=ERROR_HINTS[E_VALIDATION],
                context={"index": index, "missing": missing},
            )
        # NULL (writer did not set a level) -> "info" explicitly; `or` would also swallow "" (A-T3).
        entry_level = entry.get("level")
        rows.append(
            {
                "agent_type": entry["agent_type"],
                "action": entry["action"],
                "summary": entry["summary"],
                "level": "info" if entry_level is None else entry_level,
                "detail": entry.get("detail"),
                "created_at": entry.get("created_at") or batch_at,
            }
        )
    if not rows:
        return 0
    await db.execute(insert(ActivityLog), rows)
    if flush:
        await db.flush()
    return len(rows)


async def get_recent_activity(
    db: AsyncSession,
    limit: int = 50,
    before_id: int | None = None,
) -> list[ActivityLog]:
    """Most-recent-first, optionally older than `before_id` (feed pagination).

    The `id` tie-break is REQUIRED, not cosmetic: `created_at` is
    `server_default=func.now()`, which on SQLite is CURRENT_TIMESTAMP with
    one-second resolution, so rows written in the same second are otherwise
    returned in an arbitrary order — the shipped test fails the wrong way round.

    `before_id` is the cursor: pass the `id` of the last row already shown to get
    the next page.  Without it the call is exactly the original tail query.
    """
    stmt = select(ActivityLog)
    if before_id is not None:
        stmt = stmt.where(ActivityLog.id < before_id)
    stmt = stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="recent activity")


__all__ = ["ACTIVITY_CHANNEL", "get_recent_activity", "log_activities", "log_activity"]
