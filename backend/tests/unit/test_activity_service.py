"""Test activity_service — logging and Redis broadcast."""
import pytest
from sqlalchemy import insert

from app.core import events
from app.models.report import ActivityLog
from app.services.activity_service import get_recent_activity, log_activities, log_activity


@pytest.mark.asyncio
async def test_log_activity_creates_record(async_db_session, mock_redis):
    entry = await log_activity(
        async_db_session,
        agent_type="social_media",
        action="post_tweet",
        summary="Posted a tweet about our product",
        level="success",
    )
    await async_db_session.commit()

    assert entry.id is not None
    assert entry.agent_type == "social_media"
    assert entry.level == "success"


@pytest.mark.asyncio
async def test_log_activity_publishes_to_redis(async_db_session, mock_redis):
    await log_activity(
        async_db_session,
        agent_type="finance",
        action="revenue_snapshot",
        summary="Saved daily revenue snapshot",
    )
    mock_redis.publish.assert_called_once()
    call_args = mock_redis.publish.call_args
    assert "polsia:activity" in call_args[0]


@pytest.mark.asyncio
async def test_get_recent_activity_returns_ordered(async_db_session, mock_redis):
    await log_activity(async_db_session, agent_type="a", action="x", summary="First")
    await log_activity(async_db_session, agent_type="b", action="y", summary="Second")
    await async_db_session.commit()

    results = await get_recent_activity(async_db_session, limit=10)
    assert len(results) == 2
    # Most recent first
    assert results[0].summary == "Second"


@pytest.mark.asyncio
async def test_null_level_row_reads_back_and_payload_normalizes_to_info(async_db_session, mock_redis):
    """A NULL level is legal (DDL-nullable — A-T3): the read path must not raise, the stored state
    stays distinguishable (None, not a fabricated "info"), and the payload boundary normalizes to
    "info" for the wire (frontend/src/lib/api.ts ActivityEvent has no null member)."""
    # Core insert, not the ORM constructor: the ORM fires the python-side `default="info"`, so only
    # an insert that carries the value explicitly reaches the NULL the DDL allows (probe: `raw/`).
    await async_db_session.execute(
        insert(ActivityLog).values(
            agent_type="legacy",
            action="imported_row",
            summary="row written without a level",
            level=None,
        )
    )
    await async_db_session.commit()

    rows = await get_recent_activity(async_db_session, limit=10)
    assert len(rows) == 1
    assert rows[0].level is None  # "not set" in the DB — NOT a stored "info"

    payload = events.activity_payload(
        id=rows[0].id,
        agent_type=rows[0].agent_type,
        action=rows[0].action,
        summary=rows[0].summary,
        level=rows[0].level,
        created_at=rows[0].created_at,
        detail=rows[0].detail,
    )
    assert payload["level"] == "info"


def test_activity_payload_normalizes_none_level_to_info():
    """`level=None` (a NULL read back from the DDL-nullable column) is not an outage: the wire
    payload carries "info" — the one documented meaning (A-T3)."""
    payload = events.activity_payload(
        id=1, agent_type="social_media", action="post_tweet", summary="Posted", level=None
    )
    assert payload["level"] == "info"


@pytest.mark.asyncio
async def test_log_activities_keeps_empty_string_level(async_db_session, mock_redis):
    """`level=""` is a value the writer chose and must stay visible; `or "info"` would silently
    collapse it. Only None means "not set" (the A-T3 class rule)."""
    count = await log_activities(
        async_db_session,
        [{"agent_type": "a", "action": "x", "summary": "s", "level": ""}],
    )
    assert count == 1

    rows = await get_recent_activity(async_db_session, limit=1)
    assert rows[0].level == ""
