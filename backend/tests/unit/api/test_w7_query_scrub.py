"""W7 / SENTINEL — W6-S3: a credential in a QUERY STRING never reaches the access log.

THE DEFECT THIS PINS.  uvicorn formats its access line from the live ASGI scope at response time
(`get_path_with_query_string(self.scope)` — `uvicorn/protocols/http/h11_impl.py:477`, the same call
in `httptools_impl.py`).  Wave 6 scrubbed the WebSocket scope only, so an HTTP request carrying the
legacy `?api_key=<key>` form wrote the owner key verbatim into the uvicorn log (measured in wave 6:
four `GET /api/v1/sites?api_key=… 401` lines, W6-S3).  `app.core.query_scrub.RequestLogScrub` now
scrubs EVERY http scope before routing, so the line uvicorn writes afterwards reads
`/api/v1/sites?limit=1` for the same request.

WHY A SCOPE PROBE AND NOT A LOG CAPTURE: the app cannot capture uvicorn's own access logger (it is
configured by the server, not by `create_app`), and the thing under test is precisely the object
that logger formats from.  The probe records the SAME scope dict the server hands to the app — the
one uvicorn keeps as `self.scope` — so `scrubbed_log_target(scope)` is byte-for-byte the target the
access line would carry, and `tests/unit/test_b2_api.py` uses the same method for the socket half.

RED LEG (wave 7): remove `application.add_middleware(RequestLogScrub)` from `create_app` and
`test_the_access_log_target_carries_no_credential` fails with `api_key=sentinel-w7-secret` still in
the target (raw output in `sentinel/raw/w7/RED-query-scrub.txt`).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core.query_scrub import (
    CREDENTIAL_QUERY_KEYS,
    RequestLogScrub,
    scrub_query_credentials,
    scrubbed_log_target,
)

#: A value that cannot be mistaken for a configured key: it must be refused as auth either way.
SECRET = "sentinel-w7-secret-under-test"


@pytest.fixture()
def keyed(monkeypatch):
    """A configured owner key, restored afterwards (the app reads `settings.api_key` per request)."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "w7-owner-key")
    return "w7-owner-key"


# ── the middleware: what uvicorn would write for a request that carried a credential ───────────


def test_the_access_log_target_carries_no_credential(keyed):
    """The whole point: the scope uvicorn logs from no longer contains the credential."""
    from app.main import app

    scopes: list[dict] = []

    async def probe(scope, receive, send):
        scopes.append(scope)
        await app(scope, receive, send)

    client = TestClient(probe)
    try:
        resp = client.get(f"/api/v1/sites?api_key={SECRET}&limit=1")
    finally:
        client.close()

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "E_AUTH", resp.text
    assert SECRET not in resp.text, "the refusal must never echo the credential"
    scope = scopes[-1]
    target = scrubbed_log_target(scope)
    assert SECRET not in target, f"the access-log line would carry the credential: {target!r}"
    assert secret_in_log_target(scope) is False
    assert target == "/api/v1/sites?limit=1", (
        "the non-credential pair must SURVIVE (a route that declares `limit` still reads it)"
    )


def test_the_scrub_does_not_change_the_response(keyed):
    """A query credential never authenticated; scrubbing it must not alter the answer either."""
    from app.main import app

    client = TestClient(app)
    try:
        with_cred = client.get(f"/api/v1/sites?api_key={SECRET}")
        without = client.get("/api/v1/sites")
    finally:
        client.close()
    assert with_cred.status_code == without.status_code == 401
    assert with_cred.content == without.content, (
        "the response to a request that carried a credential must be the same refusal"
    )


def test_the_middleware_is_installed_on_the_app():
    """A structural pin: deleting the wiring is a visible failure, not a silent hole."""
    from app.main import create_app

    classes = [middleware.cls for middleware in create_app().user_middleware]
    assert RequestLogScrub in classes, (
        "RequestLogScrub must wrap the app; without it an HTTP query credential is logged"
    )


def test_every_credential_spelling_is_removed_and_other_pairs_survive():
    """All three spellings, one pass; ordering, duplicates and blank values behave."""
    scope = {
        "path": "/api/v1/sites",
        "query_string": b"status=&api_key=one&limit=5&key=two&api-key=three&ticket=v2.t.s.s",
    }
    removed = scrub_query_credentials(scope)
    assert removed == ("api_key", "key", "api-key"), removed
    assert scope["query_string"] == b"status=&limit=5&ticket=v2.t.s.s", scope["query_string"]
    target = scrubbed_log_target(scope)
    for value in ("one", "two", "three"):
        assert value not in target, target


def test_the_fast_path_leaves_the_query_bytes_untouched():
    """No credential in the URL -> the raw bytes are byte-identical (nothing is re-encoded)."""
    original = b"status=slow+cook&limit=%2B5&blank="
    scope = {"path": "/api/v1/x", "query_string": original}
    assert scrub_query_credentials(scope) == ()
    assert scope["query_string"] is original, "an untouched request must not be re-encoded"


def test_a_credential_value_can_be_empty_and_is_still_removed():
    """`?api_key=` is still a credential-shaped pair: the NAME is the rule, not the value."""
    scope = {"path": "/api/v1/sites", "query_string": b"api_key=&status=won"}
    assert scrub_query_credentials(scope) == ("api_key",)
    assert scope["query_string"] == b"status=won"


@pytest.mark.parametrize("name", sorted(CREDENTIAL_QUERY_KEYS))
def test_the_websocket_half_uses_the_same_scrub(name):
    """The socket's own scrub delegates here (it reads the values first, then scrubs)."""
    from app.api.v1 import ws as ws_module

    scope: dict = {"query_string": f"{name}={SECRET}&ticket=v2.t.s.s".encode()}
    ws_module._redact_query_string(scope)
    assert SECRET.encode() not in scope["query_string"], scope["query_string"]
    assert scope["query_string"] == b"ticket=v2.t.s.s", (
        "the supported ticket form must survive the socket's scrub"
    )


def test_the_socket_still_refuses_a_url_key_by_name_after_the_scrub(keyed):
    """Refactor guard: the WS refusal reason still distinguishes a URL key from a missing one."""
    from app.api.v1 import ws as ws_module
    from app.main import app
    from tests.support.ws_handshake import refused_handshake

    result = refused_handshake(app, f"/ws/activity?key={SECRET}")
    assert result["accepted"] is True, result["messages"]
    assert result["close_code"] == ws_module.WS_CLOSE_POLICY, result
    assert "must not travel in a URL" in (result["close_reason"] or ""), result["close_reason"]
    assert SECRET not in (result["close_reason"] or ""), result["close_reason"]


def secret_in_log_target(scope: dict) -> bool:
    """Helper for the first test: the raw credential bytes anywhere in the logged target."""
    return SECRET in scrubbed_log_target(scope) or SECRET.encode() in (scope.get("raw_path") or b"")
