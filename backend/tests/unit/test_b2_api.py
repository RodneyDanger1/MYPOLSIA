"""Seat C1 (wave B2) — the API-surface truth defects.

Four items, each with a RED leg measured at HEAD `af76c44` (transcripts in
`meridian-work/build/wave-b2/raw/C1/`):

  * **F-46** — the owner's API key rode `?key=` on `/ws/activity`, and uvicorn 0.41.0 logs
    `get_path_with_query_string(scope)` for every handshake (`websockets_impl.py:271` accepted,
    `:301` refused), so every reconnect wrote the key into the access log.  Pinned here: a key in
    the query is REFUSED; the scope the server logs is SCRUBBED before the handshake completes;
    the supported URL credential is a short-lived ticket that is not the key and cannot be forged.
  * **F-52** — the socket had no keepalive and no cap, and a Redis outage closed it with code
    1000 (a CLEAN close), so the client read a backend fault as a normal session end.  Pinned
    here: 1011 on a backend fault, a `{"type":"ping"}` heartbeat, and 1013 at the per-client cap.
  * **F-50** — `/health` was a constant and is the compose healthcheck, so a dead database was
    invisible; there was no readiness probe.  Pinned here: `/health` is liveness and resolves NO
    dependency (a `get_db` that raises proves it), `/ready` answers 200 only when the database
    answers and FAILS CLOSED with the named `E_DB` 503 when it does not.
  * **F-49** — the export filename used the LOCAL date while MANIFEST.json used UTC.  Pinned
    here: one clock (UTC) for both, including a stamp whose local date is the day before.

RED legs (each restored to a sha256-verified file unless noted):
  * F-46: HEAD's `?key=` accept → `test_the_api_key_in_the_query_string_is_refused...` FAILS
    (`accepted` was True) and the scrub assertion FAILS (scope still carried `key=...`).
  * F-52: HEAD closed 1000 → `test_a_redis_outage_closes_with_1011...` FAILS on the code.
  * F-50: HEAD had no `/ready` → the readiness tests FAIL with 404; `/health` had no DB
    dependency, so that leg is the ANTI-regression pin (green at HEAD by construction).
  * F-49: HEAD named the file from `date.today()` → the export test FAILS with
    `...-1999-12-31.zip` (the fake local clock) while the manifest says 2026-01-01.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import data as data_module
from app.api.v1 import ws as ws_module
from app.config import settings
from tests.support.ws_handshake import accepted_handshake, refused_handshake

#: The configured owner key for every socket test in this file.
KEY = "b2-seat-c1-owner-key"


# ── fixtures ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def ws_key():
    """A configured API key, restored afterwards (the handshake semantics depend on it)."""
    old = settings.api_key
    settings.api_key = KEY
    try:
        yield KEY
    finally:
        settings.api_key = old


@pytest.fixture(autouse=True)
def _clear_socket_registry():
    """The per-client counter is process state: never let one test's leak fail another."""
    ws_module._open_sockets.clear()
    yield
    ws_module._open_sockets.clear()


@pytest.fixture()
def ws_client():
    """A TestClient without lifespan: the handshake path needs no Redis/Chroma startup."""
    from app.main import app

    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


class _IdlePubsub:
    """A pubsub that subscribes fine and never yields a message (the quiet-feed case).

    It is a FAULT INJECTOR for the bus boundary, not a stand-in for code under test: the real
    `subscribe_activity` is replaced at the module seam so the socket's own lifecycle is what runs.
    """

    def __init__(self) -> None:
        self.unsubscribed = False
        self.closed = False

    async def listen(self):
        import asyncio

        await asyncio.Event().wait()  # cancellable, never yields
        yield {}  # pragma: no cover - unreachable, keeps this an async generator

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def ws_app():
    """The real FastAPI app, for the handshake helpers (they own their own TestClient)."""
    from app.main import app

    return app


def _handshake(client: TestClient, url: str, headers: dict[str, str] | None = None):
    """(accepted, refusal_code) — DEPRECATED in wave 5; kept for the scope-scrub test only.

    SV-W4-12: a refusal is now accept-then-close, so this helper's tuple no longer describes it.
    Use `tests.support.ws_handshake.refused_handshake` / `accepted_handshake`, which record the
    frames the route sends and read the close code the client really gets.
    """
    kwargs = {"headers": headers} if headers else {}
    ws = client.websocket_connect(url, **kwargs)
    try:
        ws.__enter__()
    except WebSocketDisconnect as exc:
        return False, getattr(exc, "code", None)
    ws.__exit__(None, None, None)
    return True, None


# ── F-46 · the key must not travel in the URL ─────────────────────────────────────────────────


def test_the_api_key_in_the_query_string_is_refused_and_never_reaches_the_access_log(ws_key, caplog):
    """The defect, end to end: `?key=` must not authenticate, and must not survive in the scope.

    The probe records the ASGI scope object the SERVER holds; uvicorn formats its access line from
    that same dict (`get_path_with_query_string(scope)`).  After the handshake the line must read
    `/ws/activity` — with no key in it — for a client that still sends the key in the URL.

    SV-W4-12 (wave 5): the refusal itself is asserted through the shared helper, which records the
    frames the route sends — OLD shape was a pre-accept close (an HTTP 403 on the wire, read by a
    browser as close 1006); NEW shape is accept-then-close carrying 1008.
    """
    from uvicorn.protocols.utils import get_path_with_query_string

    from app.main import app

    scopes: list[dict] = []

    async def probe(scope, receive, send):
        scopes.append(scope)
        await app(scope, receive, send)

    client = TestClient(probe)
    try:
        with caplog.at_level(logging.WARNING):
            ws = client.websocket_connect(f"/ws/activity?key={KEY}")
            ws.__enter__()
            ws.__exit__(None, None, None)
    finally:
        client.close()

    scope = scopes[-1]
    line = get_path_with_query_string(scope)
    assert scope["query_string"] == b"", "the credential must be scrubbed from the ASGI scope"
    assert KEY not in line, f"the access-log line would carry the key: {line!r}"
    assert line == "/ws/activity"
    assert KEY not in caplog.text, "no log record may echo the refused credential"

    result = refused_handshake(app, f"/ws/activity?key={KEY}")
    assert result["accepted"] is True, (
        f"the refusal must complete the handshake so the code is readable: {result['messages']}"
    )
    assert result["close_code"] == ws_module.WS_CLOSE_POLICY
    assert result["data_frames"] == 0, "a refused socket must never deliver data"
    assert KEY not in (result["close_reason"] or ""), result["close_reason"]


def test_a_ticket_is_not_the_key_and_it_authenticates_the_socket(ws_key, ws_client, ws_app):
    """The supported URL form: a short-lived, signed ticket from the header-authenticated route."""
    resp = ws_client.post("/api/v1/ws/ticket", headers={"X-API-Key": KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ticket = body["ticket"]
    assert KEY not in ticket, "the ticket must not embed the key"
    # R-W03-1 (wave 3) re-pin.  OLD: `v1.<expires>.<sig>` — 3 fields, no subject, so the socket had
    # no client identity and its cap fell back to the peer address (nginx's IP, shared by every
    # browser).  NEW: `v2.<expires>.<subject>.<sig>` — the subject is the identity a socket is
    # counted under, and it is covered by the signature.  The SEMANTICS below are unchanged.
    assert ticket.startswith("v2."), "the ticket must carry its version, expiry and subject"
    assert len(ticket.split(".")) == 4, ticket
    assert body["expires_in"] == ws_module._TICKET_TTL_SECONDS
    assert ws_module.verify_ws_ticket(ticket) is True

    accepted = accepted_handshake(ws_app, f"/ws/activity?ticket={ticket}")
    assert accepted["accepted"], f"a freshly minted ticket must be accepted: {accepted}"

    # The header form is unchanged for programmatic clients.
    accepted = accepted_handshake(ws_app, "/ws/activity", headers={"X-API-Key": KEY})
    assert accepted["accepted"], f"the X-API-Key header must still be accepted: {accepted}"


def test_the_ticket_route_requires_the_owner_key_and_a_forged_ticket_is_refused(
    ws_key, ws_client, ws_app
):
    assert ws_client.post("/api/v1/ws/ticket").status_code == 401
    assert ws_client.post(
        "/api/v1/ws/ticket", headers={"X-API-Key": "not-the-key"}
    ).status_code == 401

    ticket, _ = ws_module.mint_ws_ticket()
    # R-W03-1: 4 fields now — `v2.<expires>.<subject>.<signature>` (was `v1.<expires>.<signature>`).
    head, expiry, subject, signature = ticket.split(".")
    forged = f"{head}.{expiry}.{subject}.{'0' * len(signature)}"
    assert ws_module.verify_ws_ticket(forged) is False
    refused = refused_handshake(ws_app, f"/ws/activity?ticket={forged}")
    assert refused["accepted"] is True, refused
    assert refused["close_code"] == ws_module.WS_CLOSE_POLICY
    assert refused["data_frames"] == 0

    # The subject is SIGNED: swapping it for another identity breaks the signature (otherwise a
    # client could pick any cap key it liked).
    assert ws_module.verify_ws_ticket(f"{head}.{expiry}.other-subject.{signature}") is False

    # A far-future expiry is refused even with a valid signature for it: a ticket cannot outlive
    # its TTL by construction.
    # R-W03-1: `_ticket_signature` now takes the subject too (`v2.<expires>.<subject>` is signed).
    far = ws_module._ticket_signature(9999999999, "subj")
    assert ws_module.verify_ws_ticket(f"v2.9999999999.subj.{far}") is False

    # And an expired ticket is refused.
    expired, _ = ws_module.mint_ws_ticket(now=time.time() - ws_module._TICKET_TTL_SECONDS - 5)
    assert ws_module.verify_ws_ticket(expired) is False
    refused = refused_handshake(ws_app, f"/ws/activity?ticket={expired}")
    assert refused["accepted"] is True, refused
    assert refused["close_code"] == ws_module.WS_CLOSE_POLICY


def test_the_socket_is_still_open_when_no_key_is_configured(ws_app):
    """The house semantics, unchanged: no key configured => the service is open."""
    old = settings.api_key
    settings.api_key = ""
    try:
        result = accepted_handshake(ws_app, "/ws/activity")
    finally:
        settings.api_key = old
    assert result["accepted"], f"an open backend must accept: {result}"


# ── F-52 · keepalive, cap, and a fault that is not a clean close ─────────────────────────────


def test_a_redis_outage_closes_with_1011_not_a_clean_1000(ws_key, ws_client, monkeypatch):
    """A backend fault must be DISTINGUISHABLE from a normal session end."""
    from app.core import events

    async def unreachable():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(events, "subscribe_activity", unreachable)

    with (
        ws_client.websocket_connect("/ws/activity", headers={"X-API-Key": KEY}) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_text()

    assert exc.value.code == ws_module.WS_CLOSE_FAULT == 1011, (
        f"a Redis outage closed with {exc.value.code}; 1000 tells the client the session "
        "ended normally"
    )


def test_the_heartbeat_sends_a_ping_so_a_dead_path_is_noticed(ws_key, ws_client, monkeypatch):
    from app.core import events

    async def idle():
        return _IdlePubsub()  # subscribed, never yields: the quiet-feed case

    monkeypatch.setattr(events, "subscribe_activity", idle)
    monkeypatch.setattr(ws_module, "_HEARTBEAT_SECONDS", 0.01)

    with ws_client.websocket_connect("/ws/activity", headers={"X-API-Key": KEY}) as ws:
        import json

        frame = json.loads(ws.receive_text())
        assert frame["type"] == "ping", frame
        assert isinstance(frame["ts"], float)


def test_one_client_cannot_hold_more_than_the_socket_cap(ws_key, ws_client, ws_app, monkeypatch):
    from app.core import events

    async def idle():
        return _IdlePubsub()

    monkeypatch.setattr(events, "subscribe_activity", idle)
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_CLIENT", 1)

    first = ws_client.websocket_connect("/ws/activity", headers={"X-API-Key": KEY})
    first.__enter__()  # the handshake completed => the counter is 1
    try:
        assert sum(ws_module._open_sockets.values()) == 1
        refused = refused_handshake(ws_app, "/ws/activity", headers={"X-API-Key": KEY})
    finally:
        first.__exit__(None, None, None)

    assert refused["accepted"] is True, (
        f"the capacity refusal must complete the handshake so 1013 is readable: {refused}"
    )
    assert refused["close_code"] == ws_module.WS_CLOSE_CAPACITY == 1013
    assert refused["data_frames"] == 0
    assert ws_module._open_sockets == {}, "the cap slot must be released when a socket ends"


def test_the_close_codes_are_the_documented_ones():
    """Pinned literals: the client branches on these numbers, so they cannot drift silently."""
    assert ws_module.WS_CLOSE_POLICY == 1008
    assert ws_module.WS_CLOSE_FAULT == 1011
    assert ws_module.WS_CLOSE_CAPACITY == 1013


# ── F-50 · liveness vs readiness ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_liveness_is_constant_and_resolves_no_dependency(api_client, monkeypatch):
    """`/health` is LIVENESS: a dependency outage is not a reason to answer anything but ok."""
    from app.core.database import get_db
    from app.main import app

    def _must_not_be_resolved():
        raise AssertionError("/health must not resolve the database dependency")

    monkeypatch.setitem(app.dependency_overrides, get_db, _must_not_be_resolved)
    resp = await api_client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_is_ok_when_the_database_answers_and_needs_no_key(api_client):
    """`/ready` is READINESS: one real `SELECT 1`, unauthenticated (probes carry no key)."""
    resp = await api_client.get("/api/v1/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready", "checks": {"database": "ok"}}


@pytest.mark.asyncio
async def test_readiness_fails_closed_with_a_named_503_when_the_database_is_down(api_client, monkeypatch):
    from app.core.database import get_db
    from app.main import app

    class _DeadSession:
        """Fault injector: a session whose first statement never reaches a server."""

        async def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: _DeadSession())

    resp = await api_client.get("/api/v1/ready")
    assert resp.status_code == 503, resp.text
    assert resp.headers["X-Meridian-Error-Code"] == "E_DB"
    error = resp.json()["error"]
    assert error["code"] == "E_DB"
    assert error["how_to_fix"], "a refusal must say how to fix it"


# ── F-49 · one clock for the filename and the manifest ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_export_filename_and_the_manifest_share_one_utc_clock(
    api_client, auth_headers, monkeypatch
):
    """A stamp whose LOCAL date is the day before must still name the file by its UTC date.

    The local clock is faked to 1999-12-31, so this fails on ANY host zone at HEAD: the old
    `date.today()` filename disagrees with the manifest in the same archive.
    """
    stamp = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)  # 2025-12-31 20:30 in UTC-6

    class _FakeDate:
        @staticmethod
        def today():
            return date(1999, 12, 31)  # the real class: never the monkeypatched module attribute

    monkeypatch.setattr(data_module, "_export_stamp", lambda: stamp)
    monkeypatch.setattr(data_module, "date", _FakeDate, raising=False)  # absent after the fix

    import io
    import json
    import zipfile

    resp = await api_client.get("/api/v1/data/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text

    disposition = resp.headers["content-disposition"]
    assert 'filename="meridian-data-export-2026-01-01.zip"' in disposition, disposition

    manifest = json.loads(zipfile.ZipFile(io.BytesIO(resp.content)).read("MANIFEST.json"))
    assert manifest["exported_at"].startswith("2026-01-01"), manifest["exported_at"]
    assert manifest["exported_at"][:10] == disposition.split("meridian-data-export-")[1][:10]


def test_the_export_filename_is_the_utc_date_of_the_stamp_it_is_given():
    """The pure boundary pin: the local wall clock of an aware stamp is NOT the filename date."""
    local_midnight_ish = datetime(2025, 12, 31, 20, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert local_midnight_ish.astimezone(UTC).date().isoformat() == "2026-01-01"
    assert data_module._export_filename(local_midnight_ish) == "meridian-data-export-2026-01-01.zip"

    assert data_module._export_filename(
        datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    ) == "meridian-data-export-2025-12-31.zip"
    assert data_module._export_filename(
        datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    ) == "meridian-data-export-2026-01-01.zip"
