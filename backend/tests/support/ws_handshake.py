"""Read a `/ws/activity` handshake the way the WIRE behaves, through Starlette's `TestClient`.

SV-W4-12 (wave 5).  The wave-4 tests asserted `close(code=...)` on a hand-written fake, and
`TestClient.__enter__`'s `WebSocketDisconnect.code`.  Neither models what a client sees: an ASGI
server turns a `websocket.close` sent BEFORE `accept()` into an HTTP **403** on the upgrade and
DISCARDS the code (`uvicorn/protocols/websockets/websockets_impl.py:287-295`), and a browser cannot
read a failed handshake's status — its `CloseEvent.code` is **1006**.  The app therefore ACCEPTS and
then CLOSES the handshake, so the documented code really is what arrives.

These helpers record the frames the route sends, so a unit test can assert "accepted, then closed
with N, and no data frame was ever sent" DETERMINISTICALLY (a `threading.Event`, never a sleep).
The real-socket proof — a raw TCP upgrade plus a `websockets` client against a real uvicorn — is
`tests/unit/api/test_w5_ws_handshake_real.py`; this module is the fast unit-level twin.
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import AsyncIterator
from typing import Any

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

#: Long enough for an in-process handshake to finish; short enough that a regression fails the
#: test instead of hanging the gate.
CLOSE_TIMEOUT_SECONDS = 5.0


class HandshakeProbe:
    """An ASGI wrapper that records every frame the app sends for one handshake."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.messages: list[dict[str, Any]] = []
        self.closed = threading.Event()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def record(message: dict[str, Any]) -> None:
            self.messages.append(message)
            if message["type"] == "websocket.close":
                self.closed.set()
            await send(message)

        await self.app(scope, receive, record)

    @property
    def accepted(self) -> bool:
        return any(message["type"] == "websocket.accept" for message in self.messages)

    @property
    def close_code(self) -> int | None:
        for message in self.messages:
            if message["type"] == "websocket.close":
                return message.get("code")
        return None

    @property
    def close_reason(self) -> str:
        for message in self.messages:
            if message["type"] == "websocket.close":
                return str(message.get("reason") or "")
        return ""

    @property
    def data_frames(self) -> int:
        return sum(1 for message in self.messages if message["type"] == "websocket.send")


@contextlib.contextmanager
def _session(app: Any) -> AsyncIterator[tuple[HandshakeProbe, TestClient]]:
    probe = HandshakeProbe(app)
    client = TestClient(probe)
    try:
        yield probe, client
    finally:
        client.close()


def refused_handshake(
    app: Any, url: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Open one handshake the server is expected to REFUSE and read its close frame.

    Returns `{"accepted", "close_code", "close_reason", "data_frames", "messages"}`.  A pre-accept
    close (the wave-4 behaviour, which uvicorn turns into HTTP 403) leaves `accepted` False with the
    fake-only code; an accept-then-close leaves `accepted` True and the code on the close frame.
    The caller asserts which one it expects — this helper never pretends.
    """
    with _session(app) as (probe, client):
        kwargs = {"headers": headers} if headers else {}
        ws = client.websocket_connect(url, **kwargs)
        try:
            ws.__enter__()
        except WebSocketDisconnect as exc:  # a pre-accept close: no stream ever exists
            return {
                "accepted": False,
                "close_code": getattr(exc, "code", None),
                "close_reason": "",
                "data_frames": probe.data_frames,
                "messages": list(probe.messages),
            }
        try:
            probe.closed.wait(CLOSE_TIMEOUT_SECONDS)  # the refusal arrives immediately
        finally:
            with contextlib.suppress(Exception):
                ws.__exit__(None, None, None)
        return {
            "accepted": probe.accepted,
            "close_code": probe.close_code,
            "close_reason": probe.close_reason,
            "data_frames": probe.data_frames,
            "messages": list(probe.messages),
        }


def accepted_handshake(
    app: Any, url: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Open one handshake the server is expected to SERVE, then close it from the client side.

    A served socket parks on its subscription, so there is nothing to wait for beyond the accept.
    """
    with _session(app) as (probe, client):
        kwargs = {"headers": headers} if headers else {}
        ws = client.websocket_connect(url, **kwargs)
        try:
            ws.__enter__()
        except WebSocketDisconnect as exc:
            return {
                "accepted": False,
                "close_code": getattr(exc, "code", None),
                "close_reason": "",
                "data_frames": probe.data_frames,
                "messages": list(probe.messages),
            }
        finally:
            with contextlib.suppress(Exception):
                ws.__exit__(None, None, None)
        return {
            "accepted": probe.accepted,
            "close_code": probe.close_code,
            "close_reason": probe.close_reason,
            "data_frames": probe.data_frames,
            "messages": list(probe.messages),
        }
