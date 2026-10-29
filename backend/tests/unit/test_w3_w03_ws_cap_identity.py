"""R-W03-1 (wave 3) — the activity-socket cap was keyed on nginx's IP, so all browsers shared it.

Measured (VISUALS-1 F-03, verbatim backend log):
    INFO: ('172.18.0.9', 53750) - "WebSocket /ws/activity?ticket=v1.1793215892.b9d3…" 403
    WARNING:app.api.v1.ws:activity websocket refused: 172.18.0.9 already holds 4 open socket(s)
`172.18.0.9` is the NGINX container: every browser is that one peer, so four open sockets anywhere
in the install refused every later handshake — the dashboard alone mounts two consumers, and the
wave-2 drive measured 8 refused handshakes on one page and 22 ticket mints across three pages.

The fix: the cap key is the socket's IDENTITY, taken from the credential it presented —

  * a verified ticket carries a random SUBJECT (one per mint, one mint per browser connection):
    key `<operator>|s:<subject>`;
  * a header-authenticated client has no subject: key `<operator>|s:-`;
  * the peer address is never used, and a second, install-wide OPERATOR cap keeps the resource
    bound that the address cap accidentally provided.

Pinned here: two tickets from the same proxy address are two identities; one ticket cannot exceed
its own cap; the install-wide cap still binds; the subject is signed (not forgeable); and the
header path keeps its old single-client semantics.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.api.v1 import ws as ws_module
from app.core import events

NGINX = "172.18.0.9"


class _IdlePubsub:
    """Subscribes and never yields: the socket's own lifecycle is what these tests drive."""

    async def listen(self):
        await asyncio.Event().wait()
        yield {}  # pragma: no cover - unreachable, keeps this an async generator

    async def unsubscribe(self, channel: str) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeWebSocket:
    """The members the handler reads, with the nginx peer address every browser really has."""

    def __init__(self, *, ticket: str | None = None, api_key: str | None = None,
                 host: str = NGINX) -> None:
        self.accepted = False
        self.closed = False
        self.close_codes: list[int | None] = []
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        if ticket is not None:
            self.query_params["ticket"] = ticket
        if api_key is not None:
            self.headers["x-api-key"] = api_key
        self.client = type("Peer", (), {"host": host, "port": 51000})()
        self.scope: dict[str, Any] = {"query_string": b""}

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        return None

    async def receive_text(self) -> str:
        await asyncio.Event().wait()      # a quiet client: parks until the handler is torn down
        return ""                          # pragma: no cover - unreachable

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self.close_codes.append(code)


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    """A configured owner key, an idle bus, and EMPTY counters — the handler's real code runs."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "DEV-KEY-R-W03-1")
    monkeypatch.setattr(events, "subscribe_activity", lambda: _idle())
    monkeypatch.setattr(ws_module, "_open_sockets", {})
    monkeypatch.setattr(ws_module, "_operator_sockets", {})
    yield
    ws_module._open_sockets.clear()
    ws_module._operator_sockets.clear()


async def _idle() -> _IdlePubsub:
    return _IdlePubsub()


async def _hold(ws: FakeWebSocket) -> asyncio.Task:
    """Start the handler and wait until it accepted or refused; return the running task."""
    task = asyncio.create_task(ws_module.activity_socket(ws))
    for _ in range(400):
        if ws.accepted or ws.closed:
            return task
        await asyncio.sleep(0.002)
    raise AssertionError("the handler neither accepted nor refused")


async def _release(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ── the identity itself ──────────────────────────────────────────────────────────────────────


def test_the_identity_is_the_ticket_subject_and_never_the_peer_address() -> None:
    ticket, _ = ws_module.mint_ws_ticket()
    subject = ticket.split(".")[2]
    ws = FakeWebSocket(ticket=ticket)

    key, kind = ws_module._socket_identity(ws, ticket)

    assert kind == "session"
    assert key.endswith(f"|s:{subject}")
    assert NGINX not in key, "the proxy address must not be part of the cap key"


def test_two_mints_carry_two_distinct_subjects() -> None:
    first, _ = ws_module.mint_ws_ticket()
    second, _ = ws_module.mint_ws_ticket()
    assert first.split(".")[2] != second.split(".")[2]
    assert first.split(".")[0] == "v2"
    assert "DEV-KEY-R-W03-1" not in first, "the ticket must never embed the key"


def test_a_header_client_is_identified_by_the_operator_not_the_address() -> None:
    ws = FakeWebSocket(api_key="DEV-KEY-R-W03-1")
    key, kind = ws_module._socket_identity(ws, None)
    assert kind == "operator"
    assert key == f"{ws_module.operator_identity()}|s:-"
    assert NGINX not in key


def test_the_operator_identity_is_one_way() -> None:
    """`operator_identity()` appears in cap keys and log lines; it must not be the key."""
    ident = ws_module.operator_identity()
    assert ident and "DEV-KEY-R-W03-1" not in ident
    assert len(ident) == 16


# ── the cap behaviour (the measured defect) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_tabs_behind_the_proxy_do_not_share_one_socket_cap(monkeypatch) -> None:
    """RED-1: with per-address keying the second tab was refused (403/1013) by the first one."""
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_CLIENT", 1)
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_OPERATOR", 32)

    tab_one = FakeWebSocket(ticket=ws_module.mint_ws_ticket()[0])
    task_one = await _hold(tab_one)
    try:
        assert tab_one.accepted is True
        tab_two = FakeWebSocket(ticket=ws_module.mint_ws_ticket()[0])
        task_two = await _hold(tab_two)
        try:
            assert tab_two.accepted is True, (
                f"a second tab behind the same proxy was refused (codes {tab_two.close_codes})"
            )
            assert tab_two.close_codes == []
            assert len(ws_module._open_sockets) == 2, ws_module._open_sockets
        finally:
            await _release(task_two)
    finally:
        await _release(task_one)
    assert ws_module._open_sockets == {}, "slots must be released on teardown"
    assert ws_module._operator_sockets == {}


@pytest.mark.asyncio
async def test_one_ticket_cannot_hold_more_than_its_own_cap(monkeypatch) -> None:
    """The per-identity cap still binds: one credential, one budget."""
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_CLIENT", 1)
    ticket = ws_module.mint_ws_ticket()[0]

    first = FakeWebSocket(ticket=ticket)
    task = await _hold(first)
    try:
        second = FakeWebSocket(ticket=ticket)
        await _hold(second)
        assert second.accepted is False
        assert second.close_codes == [ws_module.WS_CLOSE_CAPACITY] == [1013]
    finally:
        await _release(task)


@pytest.mark.asyncio
async def test_the_install_wide_operator_cap_still_binds(monkeypatch) -> None:
    """Minting more tickets must not escape the resource bound (that was the address cap's job)."""
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_CLIENT", 4)
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_OPERATOR", 2)

    tasks = []
    try:
        for _ in range(2):
            ws = FakeWebSocket(ticket=ws_module.mint_ws_ticket()[0])
            tasks.append(await _hold(ws))
            assert ws.accepted is True
        third = FakeWebSocket(ticket=ws_module.mint_ws_ticket()[0])
        await _hold(third)
        assert third.accepted is False
        assert third.close_codes == [ws_module.WS_CLOSE_CAPACITY]
        assert sum(ws_module._operator_sockets.values()) == 2
    finally:
        for task in tasks:
            await _release(task)


@pytest.mark.asyncio
async def test_header_clients_keep_their_single_client_semantics(monkeypatch) -> None:
    """A programmatic client (no ticket) shares one identity, exactly as before."""
    monkeypatch.setattr(ws_module, "_MAX_SOCKETS_PER_CLIENT", 1)
    first = FakeWebSocket(api_key="DEV-KEY-R-W03-1")
    task = await _hold(first)
    try:
        second = FakeWebSocket(api_key="DEV-KEY-R-W03-1")
        await _hold(second)
        assert first.accepted is True
        assert second.accepted is False and second.close_codes == [1013]
    finally:
        await _release(task)


# ── the credential rules are unchanged ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_ticket_with_a_swapped_subject_is_refused() -> None:
    """The subject is signed: it is not a client-supplied cap key."""
    ticket, _ = ws_module.mint_ws_ticket()
    head, expiry, _subject, signature = ticket.split(".")
    forged = f"{head}.{expiry}.picked-by-the-client.{signature}"
    ws = FakeWebSocket(ticket=forged)
    await _hold(ws)
    assert ws.accepted is False
    assert ws.close_codes == [ws_module.WS_CLOSE_POLICY]
    assert ws_module._open_sockets == {}


@pytest.mark.asyncio
async def test_an_expired_or_v1_ticket_is_refused() -> None:
    expired, _ = ws_module.mint_ws_ticket(now=time.time() - ws_module._TICKET_TTL_SECONDS - 5)
    legacy = f"v1.{int(time.time()) + 10}.{ws_module._ticket_signature(int(time.time()) + 10, 's')}"
    for ticket in (expired, legacy):
        ws = FakeWebSocket(ticket=ticket)
        await _hold(ws)
        assert ws.accepted is False, ticket
        assert ws.close_codes == [ws_module.WS_CLOSE_POLICY], ticket
