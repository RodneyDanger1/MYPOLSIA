"""The activity publish path must degrade, not stall — and the degradation must be visible.

Measured before this existed: with Redis absent, every publish waited out redis-py's connection retry
ladder (no socket timeout) and logged a full traceback, so 78 activity events produced 3,510 log lines
and a stall on every write. The 200k-row shipped-code benchmark had to be abandoned because of it.

RED leg for this file: `monkeypatch.setattr(events, "_PUBLISH_COOLDOWN_S", 0.0)` — with the cooldown at
zero the breaker can never skip, and `test_breaker_skips_after_a_failure_and_counts_the_skips` fails on
`client.calls == 1` (it observes 10). Restore the cooldown and the file is green again.
"""
from __future__ import annotations

import types

import pytest

from app.core import events
from app.services import activity_service


class _Client:
    """Stand-in for the async Redis client: counts attempts, optionally fails like a dead server."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def publish(self, channel: str, message: str) -> int:
        self.calls += 1
        if self.fail:
            raise ConnectionError("Error 22 connecting to localhost:6379. refused.")
        return 1


@pytest.fixture(autouse=True)
def _isolate_breaker():
    """Breaker state is module-global, so it must not leak into the shipped publish tests."""
    events.reset_publish_breaker()
    yield
    events.reset_publish_breaker()


@pytest.mark.asyncio
async def test_publish_is_attempted_once_per_event_when_redis_is_up(monkeypatch):
    client = _Client()
    monkeypatch.setattr(events, "get_redis", lambda: client)

    for _ in range(3):
        await events.publish_activity({"action": "x"})

    assert client.calls == 3, "a healthy Redis path must not be throttled by the breaker"
    stats = events.publish_stats()
    assert stats["failures"] == 0
    assert stats["skipped_while_open"] == 0
    assert stats["breaker_open"] is False


@pytest.mark.asyncio
async def test_breaker_skips_after_a_failure_and_counts_the_skips(monkeypatch):
    client = _Client(fail=True)
    monkeypatch.setattr(events, "get_redis", lambda: client)

    await events.publish_activity({"action": "x"})  # the one bounded attempt for this window
    for _ in range(9):  # ...the outage continues
        await events.publish_activity({"action": "x"})

    assert client.calls == 1, (
        "a Redis outage must cost ONE attempt per cooldown window, not one stalled attempt per event"
    )
    stats = events.publish_stats()
    assert stats["failures"] == 1
    assert stats["skipped_while_open"] == 9, "skips are counted — a skip is never silent"
    assert stats["breaker_open"] is True
    assert "ConnectionError" in stats["last_error"]
    assert "localhost:6379" in stats["last_error"], "the error must name the thing that is down"


@pytest.mark.asyncio
async def test_a_success_after_the_cooldown_closes_the_breaker(monkeypatch):
    client = _Client(fail=True)
    monkeypatch.setattr(events, "get_redis", lambda: client)
    # The fake clock is installed BEFORE the failure on purpose: the breaker stores an absolute
    # deadline, so a test that fails on the real clock and then tries to recover on a fake one that
    # sits BEHIND it observes a skip instead of a retry. (First revision of this test did exactly
    # that and failed — the test was wrong, not the breaker.)
    clock = {"t": 1_000.0}
    monkeypatch.setattr(events, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))

    await events.publish_activity({"action": "x"})
    assert events.publish_stats()["breaker_open"] is True

    clock["t"] += events._PUBLISH_COOLDOWN_S + 1  # the cooldown elapses
    client.fail = False

    await events.publish_activity({"action": "x"})

    stats = events.publish_stats()
    assert client.calls == 2, "after the cooldown the path must try again, not stay muted forever"
    assert stats["breaker_open"] is False
    assert stats["failures"] == 1, "history is kept: the outage stays on the record"


@pytest.mark.asyncio
async def test_log_activity_still_writes_the_row_while_redis_is_down(async_db_session, monkeypatch):
    """The oldest promise on this path: a Redis outage must never break the DB write."""
    client = _Client(fail=True)
    monkeypatch.setattr(events, "get_redis", lambda: client)

    row = await activity_service.log_activity(
        async_db_session,
        agent_type="orchestrator",
        action="breaker_probe",
        summary="redis is down",
    )
    await async_db_session.commit()

    assert row is not None and row.id is not None, "the activity row must still land"
    assert events.publish_stats()["failures"] == 1
