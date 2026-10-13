"""The activity socket authenticates through the SAME matcher as its REST twin (D1; audit L1-01/L3-08).

`api_key_matches` keeps the exact REST semantics: no key configured => the service is open (a
blank value counts as "no credential"); otherwise a constant-time compare. The socket refuses
BEFORE accept, so a denied handshake raises `WebSocketDisconnect` with code 1008 and no stream
is ever established; an accepted client enters fine and the server then closes on its own
bounded redis path (code 1000 — honest and bounded, not a hang).

RED leg (observed): neutralize the guard in `activity_socket` (`if False:`) -> the refused
handshakes enter successfully and the first two tests fail; restore -> green.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture()
def ws_client():
    from app.main import app

    # A plain TestClient (no `with`) does not run lifespan -> no redis/chroma startup is needed
    # to exercise the handshake check.
    client = TestClient(app)
    yield client
    client.close()


def _set_api_key(value: str) -> str:
    from app.config import settings

    old = settings.api_key
    settings.api_key = value
    return old


def _handshake(client: TestClient, url: str, headers: dict[str, str] | None = None):
    """(accepted, refusal_code). A pre-accept refusal raises from `__enter__` (code 1008)."""
    kwargs = {"headers": headers} if headers else {}
    ws = client.websocket_connect(url, **kwargs)
    try:
        ws.__enter__()
    except WebSocketDisconnect as exc:  # refused before accept: no stream ever exists
        return False, getattr(exc, "code", None)
    ws.__exit__(None, None, None)
    return True, None


@pytest.mark.parametrize("url", ["/ws/activity", "/ws/activity?key=wrong"])
def test_ws_refused_with_1008_when_key_configured_and_missing_or_wrong(ws_client, url):
    old = _set_api_key("secret-key")
    try:
        accepted, code = _handshake(ws_client, url)
        assert not accepted, "an unauthenticated subscriber must not complete the handshake"
        assert code == 1008, "the refusal must be the policy-violation close, not a silent drop"
    finally:
        _set_api_key(old)


def test_ws_accepted_with_the_right_key_in_the_query(ws_client):
    old = _set_api_key("secret-key")
    try:
        accepted, _ = _handshake(ws_client, "/ws/activity?key=secret-key")
        assert accepted
    finally:
        _set_api_key(old)


def test_ws_accepted_with_the_right_key_header_for_programmatic_clients(ws_client):
    old = _set_api_key("secret-key")
    try:
        accepted, _ = _handshake(ws_client, "/ws/activity", headers={"x-api-key": "secret-key"})
        assert accepted
    finally:
        _set_api_key(old)


def test_ws_is_open_when_no_key_is_configured_blank_counts_as_absent(ws_client):
    old = _set_api_key("")
    try:
        accepted, _ = _handshake(ws_client, "/ws/activity")
        assert accepted
    finally:
        _set_api_key(old)
