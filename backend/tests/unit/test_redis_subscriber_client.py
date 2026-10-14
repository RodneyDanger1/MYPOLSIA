"""U6-F01 pins: the subscriber client must NOT inherit the 1s socket read timeout.

Why this file exists: live, the shared client's `socket_timeout` (correct for publish and request
paths — fail fast, breaker owns recovery) fired `TimeoutError` on every quiet second of
`pubsub.listen()`, so `app/api/v1/ws.py` closed each dashboard socket with code 1000 and the
activity feed never streamed a live event (fresh-eyes F-01, replayed in a real browser).  These
pins keep the two clients' contracts separate: bounded request client, blocking subscriber, and
the relay routed through the latter.
"""
from __future__ import annotations

import pytest

from app.core import events, redis_client


class _FromUrlRecorder:
    """Captures every from_url call's kwargs; returns a bare sentinel the factories may cache."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, url: str, **kwargs) -> object:
        self.calls.append({"url": url, **kwargs})
        return object()


@pytest.fixture
def recorder(monkeypatch) -> _FromUrlRecorder:
    rec = _FromUrlRecorder()
    monkeypatch.setattr(redis_client.aioredis, "from_url", rec)
    # Fresh module state per test: the factories cache process-wide, and a previously cached
    # sentinel would mask the contract these pins assert.
    monkeypatch.setattr(redis_client, "_client", None)
    monkeypatch.setattr(redis_client, "_subscriber", None)
    return rec


def test_subscriber_client_has_no_socket_read_timeout(recorder):
    redis_client.get_redis_subscriber()
    assert len(recorder.calls) == 1
    kwargs = recorder.calls[0]
    assert kwargs.get("socket_timeout") is None, (
        "the subscriber must block between messages (U6-F01) — a read timeout kills listen()"
    )
    assert kwargs.get("socket_connect_timeout") == redis_client._CONNECT_TIMEOUT_S, (
        "connect stays bounded: a dead Redis still refuses fast"
    )


def test_request_client_keeps_its_socket_read_timeout(recorder):
    redis_client.get_redis()
    assert len(recorder.calls) == 1
    kwargs = recorder.calls[0]
    assert kwargs.get("socket_timeout") == redis_client._SOCKET_TIMEOUT_S, (
        "the publish/request bound is deliberate (measured 200k-row benchmark) — do not move it"
    )


class _FakePubSub:
    def __init__(self) -> None:
        self.subscribed: str | None = None

    async def subscribe(self, channel: str) -> None:
        self.subscribed = channel


class _FakeClient:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


@pytest.mark.asyncio
async def test_subscribe_activity_routes_through_the_subscriber_factory(monkeypatch):
    """The routing IS the fix: a regression back to get_redis() reintroduces F-01 silently."""
    pubsub = _FakePubSub()
    calls = {"subscriber": 0, "shared": 0}

    def subscriber_factory():
        calls["subscriber"] += 1
        return _FakeClient(pubsub)

    def shared_factory():
        calls["shared"] += 1
        return _FakeClient(pubsub)

    monkeypatch.setattr(events, "get_redis_subscriber", subscriber_factory)
    monkeypatch.setattr(events, "get_redis", shared_factory)

    got = await events.subscribe_activity()

    assert got is pubsub
    assert calls == {"subscriber": 1, "shared": 0}, (
        "subscribe_activity must use the blocking subscriber client, never the bounded one"
    )
    assert pubsub.subscribed == events.ACTIVITY_CHANNEL


@pytest.mark.asyncio
async def test_close_redis_closes_both_clients(monkeypatch):
    closed = {"client": False, "subscriber": False}

    class _Closable:
        def __init__(self, key: str) -> None:
            self._key = key

        async def aclose(self) -> None:
            closed[self._key] = True

    monkeypatch.setattr(redis_client, "_client", _Closable("client"))
    monkeypatch.setattr(redis_client, "_subscriber", _Closable("subscriber"))

    await redis_client.close_redis()

    assert closed == {"client": True, "subscriber": True}, (
        "shutdown must not leak the subscriber connection"
    )
    assert redis_client._client is None and redis_client._subscriber is None
