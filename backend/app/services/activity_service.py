"""activity_log writes + Redis broadcast.

Measured facts this module encodes (lane 06, `DATA-PERF.md` §2.7 / §5.4):

* **One publish per SINK CALL** — the shipped test asserts
  `mock_redis.publish.assert_called_once()` and the channel must contain
  `polsia:activity`, so there is exactly one `publish_activity` call site per writer and no
  read path publishes anything.
* **Two writers, two transaction rules.**  `log_activity` writes through the caller's session
  (the feed's own writes, a worker's outcome transaction) and publishes right after its `flush`,
  exactly as before.  `log_activity_isolated` is the AUDIT writer: its row lives in its own
  transaction on its own connection, it COMMITS, and only then publishes — an audit row must not
  be able to fail the action it records, and the live feed must never announce a row that a later
  rollback erases (F-47 / A9-04).
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

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import Text, cast, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import events
from app.core.database import get_session_factory, utcnow
from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.report import ActivityLog
from app.services.query_helpers import fetch_rows, json_or_none

logger = logging.getLogger(__name__)

ACTIVITY_CHANNEL = events.ACTIVITY_CHANNEL  # "polsia:activity"

_REQUIRED_KEYS = ("agent_type", "action", "summary")

#: The session factory `log_activity_isolated` uses.  `None` (the default) DERIVES one from the
#: caller's own engine (`AsyncSession.bind`), so the row lands in the same database the caller and
#: its tests read — on a second connection, in its own transaction.  Set it only to point the audit
#: store somewhere else (a separate writer database, or a fault-injection test).
ISOLATED_SESSION_FACTORY: Callable[[], AsyncSession] | None = None


def _isolated_factory(db: AsyncSession) -> Callable[[], AsyncSession]:
    """The factory the isolated writer uses: the explicit override, else `db`'s own engine.

    `db` is read for its ENGINE only.  Never round-trip the write through `db` itself: that is the
    F-47 defect (a failed audit `flush` leaves the caller's session needing a rollback).
    """
    if ISOLATED_SESSION_FACTORY is not None:
        return ISOLATED_SESSION_FACTORY
    bind = db.bind
    if isinstance(bind, AsyncEngine):
        return async_sessionmaker(bind, class_=AsyncSession, expire_on_commit=False)
    return get_session_factory()


async def _insert_activity(
    db: AsyncSession,
    agent_type: str,
    action: str,
    summary: str,
    *,
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> ActivityLog:
    """Insert ONE activity row and flush it (no publish, no commit) — the shared write step."""
    entry = ActivityLog(
        agent_type=agent_type,
        action=action,
        summary=summary,
        level=level,
        detail=detail,
    )
    db.add(entry)
    await db.flush()
    return entry


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

    This writer shares the CALLER's transaction.  When the row records an ACTION whose outcome
    must not depend on it — a send, a provider save, a probe — use `log_activity_isolated`.
    """
    entry = await _insert_activity(
        db, agent_type, action, summary, level=level, detail=detail
    )

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


async def log_activity_isolated(
    db: AsyncSession,
    agent_type: str,
    action: str,
    summary: str,
    *,
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> bool:
    """Write ONE audit row in its OWN transaction, commit it, THEN publish it (F-47 / A9-04).

    An audit row must not be able to fail the action it records, and the action's refusal must not
    roll the audit row back: so this writer never touches `db` — it takes that session's ENGINE,
    opens a second session, inserts, COMMITS, and only then publishes.  The publish therefore
    follows the commit: the live feed can never announce a row that is not in the ledger.

    `db` is the caller's session and is used for its engine alone (plus the test seam it carries).
    Returns False, and logs the driver error, when the store refused the write — the caller's
    answer is unchanged either way (`phone.py` reports `audit: "unavailable"`; the integrations
    routes have no field to carry it and keep their answer).  A publish that fails AFTER the commit
    is logged and does not change the answer: the row is durable, only the live announcement was lost.
    """
    try:
        async with _isolated_factory(db)() as audit_db:
            entry = await _insert_activity(
                audit_db, agent_type, action, summary, level=level, detail=detail
            )
            await audit_db.commit()
            payload = events.activity_payload(
                id=entry.id,
                agent_type=entry.agent_type,
                action=entry.action,
                summary=entry.summary,
                level=entry.level,
                created_at=entry.created_at,
                detail=entry.detail,
            )
    except Exception:  # noqa: BLE001 — an audit failure must never rewrite the outcome it records
        logger.exception("activity row could not be written: %s/%s", agent_type, action)
        return False
    try:
        await events.publish_activity(payload)
    except Exception:  # noqa: BLE001 — the row is COMMITTED; a failed announce must not rewrite it
        logger.exception("activity row written but not published: %s/%s", agent_type, action)
    return True


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

    V24/L6: `detail` is read as TEXT and parsed per row (`query_helpers.json_or_none`).
    Reading the ORM JSON column made the driver raise `JSONDecodeError` while fetching when one
    at-rest row held malformed bytes — `GET /dashboard/activity` answered a bare 500 for the
    whole feed (measured: `pre/probe_junk_json.txt`).  A detail that is not a JSON OBJECT is
    served as null (the response field's type is `dict | None`), with a warning naming the row;
    the honest columns beside it always serve.  The returned rows are DETACHED snapshots.
    """
    stmt = select(
        ActivityLog.id,
        ActivityLog.agent_type,
        ActivityLog.action,
        ActivityLog.summary,
        ActivityLog.level,
        cast(ActivityLog.detail, Text).label("detail_text"),
        ActivityLog.created_at,
    )
    if before_id is not None:
        stmt = stmt.where(ActivityLog.id < before_id)
    stmt = stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(limit)
    entries = await fetch_rows(db, stmt, what="recent activity")
    return [_activity_from_row(entry) for entry in entries]


def _activity_from_row(entry: Any) -> ActivityLog:
    """A detached `ActivityLog` whose `detail` was parsed tolerantly to a dict (or None)."""
    values = entry._mapping
    detail = json_or_none(values["detail_text"])
    if detail is not None and not isinstance(detail, dict):
        logger.warning(
            "activity row %s carries a non-object detail (%s); serving null",
            values["id"],
            type(detail).__name__,
        )
        detail = None
    return ActivityLog(
        id=values["id"],
        agent_type=values["agent_type"],
        action=values["action"],
        summary=values["summary"],
        level=values["level"],
        detail=detail,
        created_at=values["created_at"],
    )


__all__ = [
    "ACTIVITY_CHANNEL",
    "ISOLATED_SESSION_FACTORY",
    "get_recent_activity",
    "log_activities",
    "log_activity",
    "log_activity_isolated",
]
