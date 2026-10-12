"""The /ws/activity relay — the one route with no test, and the one that hid a `NameError`.

Every dashboard page view opens this socket in production, and its teardown branch (unsubscribe +
close) runs only when a client disconnects — the branch least likely to be exercised by hand. Ruff
found a NameError in that exact block while the suite was green around it, which is the definition of
an untested branch.

The handler is called DIRECTLY with fakes rather than through a test client: a client/server test has
to race the server task to observe teardown, while a direct call returns only after `finally` has run,
so "the subscription was released" is asserted deterministically instead of polled.
"""
from __future__ import annotations

import json

import pytest
from fastapi import WebSocketDisconnect

from app.api.v1.ws import activity_socket
from app.core import events


class FakePubSub:
    """Stands in for redis.asyncio.client.PubSub: async listen() + recorded teardown."""

    def __init__(self, messages: list[dict] | None = None, raises: BaseException | None = None) -> None:
        self._messages = messages or []
        self._raises = raises
        self.subscribed: str | None = None
        self.unsubscribed: str | None = None
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed = channel

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed = channel

    async def close(self) -> None:
        self.closed = True

    async def listen(self):
        if self._raises is not None:
            raise self._raises
        for message in self._messages:
            yield message


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> FakePubSub:
        return self._pubsub


class FakeWebSocket:
    """Only the four members the handler touches."""

    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.closed = False
        self.close_calls = 0

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True
        self.close_calls += 1


@pytest.fixture
def payload() -> dict:
    return {
        "id": 7,
        "agent_type": "social_media",
        "action": "tweet_drafted",
        "summary": "drafted a reply",
        "level": "info",
        "created_at": "2026-10-11T00:00:00+00:00",
        "detail": None,
    }


def _wire(monkeypatch, pubsub: FakePubSub) -> FakePubSub:
    monkeypatch.setattr(events, "get_redis", lambda: FakeRedis(pubsub))
    return pubsub


@pytest.mark.asyncio
async def test_relays_only_message_frames_and_releases_the_subscription(monkeypatch, payload):
    pubsub = _wire(
        monkeypatch,
        FakePubSub(
            [
                {"type": "subscribe", "data": "1"},  # redis sends this first — must NOT reach the browser
                {"type": "message", "data": json.dumps(payload)},
                {"type": "message", "data": None},  # null data must relay as "" and not crash
            ]
        ),
    )
    ws = FakeWebSocket()

    await activity_socket(ws)

    assert ws.accepted is True, "accept() must happen before subscribing"
    assert pubsub.subscribed == events.ACTIVITY_CHANNEL
    assert len(ws.sent) == 2, f"the subscribe confirmation must be dropped, got {ws.sent!r}"
    assert json.loads(ws.sent[0])["action"] == "tweet_drafted"
    assert json.loads(ws.sent[0])["agent_type"] == "social_media"
    assert ws.sent[1] == "", "a null payload must serialise to an empty frame, not 'None'"
    assert pubsub.unsubscribed == events.ACTIVITY_CHANNEL
    assert pubsub.closed is True, "teardown must close the redis pubsub, not leak the subscription"
    assert ws.closed is True


@pytest.mark.asyncio
async def test_client_disconnect_ends_the_handler_without_raising(monkeypatch):
    pubsub = _wire(monkeypatch, FakePubSub(raises=WebSocketDisconnect(code=1000)))
    ws = FakeWebSocket()

    await activity_socket(ws)  # a disconnect is a normal life event, not an error to propagate

    assert ws.sent == []
    assert pubsub.unsubscribed == events.ACTIVITY_CHANNEL
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_an_unexpected_error_is_contained_and_still_tears_down(monkeypatch):
    """This is the branch that hid a NameError: it must swallow, log, and still release the socket."""
    pubsub = _wire(monkeypatch, FakePubSub(raises=RuntimeError("redis went away")))
    ws = FakeWebSocket()

    await activity_socket(ws)  # must return, not raise: a socket error may never crash the server

    assert pubsub.closed is True
    assert ws.closed is True
    assert ws.close_calls == 1, "the socket must be closed exactly once"
