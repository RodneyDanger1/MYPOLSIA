"""The activity socket authenticates through the SAME matcher as its REST twin (D1; audit L1-01/L3-08).

`api_key_matches` keeps the exact REST semantics: no key configured => the service is open (a
blank value counts as "no credential"); otherwise a constant-time compare.

SV-W4-12 (wave 5) — WHAT A REFUSAL LOOKS LIKE, corrected.  The wave-4 pin was
`WebSocketDisconnect.code == 1008` raised from `TestClient.__enter__`, i.e. the ASGI message a
`close()` BEFORE `accept()` produces.  On the wire that is not a close code at all: uvicorn answers
the HTTP upgrade with **403** (`uvicorn/protocols/websockets/websockets_impl.py:287-295`) and a
browser, which cannot read a failed handshake's status, reports close code **1006** — a refused
credential was indistinguishable from a dead network (measured live: 9 refusals, all HTTP 403).
The handler now ACCEPTS the handshake and closes it with the documented code, so these tests assert
the delivery the client really gets: `accepted`, then a close frame carrying 1008, and NO data
frame.  The wire-level proof (a raw TCP upgrade and a real `websockets` client against a real
uvicorn) is `tests/unit/api/test_w5_ws_handshake_real.py`.

RED leg for the wave-5 change: restore the pre-fix `ws.py` (close before accept) and these tests
fail with `accepted is False` / no close code — the same failure the live image showed.
"""
from __future__ import annotations

import pytest

from tests.support.ws_handshake import accepted_handshake, refused_handshake


@pytest.fixture()
def ws_app():
    """The real app.  A plain TestClient does not run lifespan -> no redis/chroma startup needed."""
    from app.main import app

    return app


def _set_api_key(value: str) -> str:
    from app.config import settings

    old = settings.api_key
    settings.api_key = value
    return old


@pytest.mark.parametrize("url", ["/ws/activity", "/ws/activity?key=wrong"])
def test_ws_refused_with_1008_when_key_configured_and_missing_or_wrong(ws_app, url):
    old = _set_api_key("secret-key")
    try:
        result = refused_handshake(ws_app, url)
        assert result["accepted"] is True, (
            "the refusal must complete the handshake, otherwise the browser sees 1006: "
            f"{result['messages']}"
        )
        assert result["close_code"] == 1008, (
            f"the refusal must be the policy-violation close, not a silent drop: {result}"
        )
        assert result["data_frames"] == 0, "a refused socket must never deliver data"
    finally:
        _set_api_key(old)


def test_ws_refuses_the_key_in_the_query_string_and_accepts_the_header_form(ws_app):
    """F-46 (wave B2): the URL is not a credential channel.

    OLD pin: `test_ws_accepted_with_the_right_key_in_the_query` asserted
    `_handshake(ws_client, "/ws/activity?key=secret-key")` was ACCEPTED — the defect, pinned as a
    contract.  uvicorn logs `get_path_with_query_string(scope)` for every handshake, so that accept
    wrote the owner key into the access log on every reconnect.  NEW pin: the same URL is REFUSED
    with 1008 (browsers use `POST /api/v1/ws/ticket` + `?ticket=`, covered by test_b2_api.py), and
    the header form is what a programmatic client uses.
    """
    old = _set_api_key("secret-key")
    try:
        result = refused_handshake(ws_app, "/ws/activity?key=secret-key")
        assert result["accepted"] is True, "a key in the query string must not authenticate"
        assert result["close_code"] == 1008, result
        assert result["data_frames"] == 0
        accepted = accepted_handshake(
            ws_app, "/ws/activity", headers={"x-api-key": "secret-key"}
        )
        assert accepted["accepted"] is True, accepted
    finally:
        _set_api_key(old)


def test_ws_accepted_with_the_right_key_header_for_programmatic_clients(ws_app):
    old = _set_api_key("secret-key")
    try:
        result = accepted_handshake(ws_app, "/ws/activity", headers={"x-api-key": "secret-key"})
        assert result["accepted"] is True, result
    finally:
        _set_api_key(old)


def test_ws_is_open_when_no_key_is_configured_blank_counts_as_absent(ws_app):
    old = _set_api_key("")
    try:
        result = accepted_handshake(ws_app, "/ws/activity")
        assert result["accepted"] is True, result
    finally:
        _set_api_key(old)


class _VanishedPeer:
    """A handshake whose peer is already gone: `accept()` raises, as a closing transport does.

    Exercises `_refuse_handshake`'s failure mode.  A refusal must never raise just because the
    client left mid-refusal: the decision was already made and logged, and nothing may escape into
    the ASGI server as an unhandled exception.
    """

    def __init__(self) -> None:
        self.query_params = {"key": "sent-in-the-url"}
        self.headers: dict[str, str] = {}
        self.scope = {"query_string": b"key=sent-in-the-url"}
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        raise RuntimeError("transport already closed")

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_refusal_to_a_vanished_peer_is_logged_and_does_not_raise(ws_app):
    from app.api.v1.ws import activity_socket

    peer = _VanishedPeer()
    await activity_socket(peer)  # must return, not raise
    assert peer.closed is False, "there is no socket to close once accept() has failed"
