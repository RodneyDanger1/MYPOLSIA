"""WebSocket /ws/activity — relays the Redis pub/sub activity channel.

nginx.conf proxies /ws/ to the backend; frontend/src/hooks/useActivityFeed.ts
connects to `${WS_URL}/ws/activity`.

Auth (F-46, wave B2): the owner's API key must NEVER travel in the URL.  A URL is not a
credential channel — uvicorn 0.41.0 logs `get_path_with_query_string(scope)` for EVERY
websocket handshake, accepted (`"WebSocket %s" [accepted]`,
`protocols/websockets/websockets_impl.py:271`) and refused (`"%s - \"WebSocket %s\" %d"`,
same file line 301), so `?key=<api key>` wrote the owner's key into the access log on every
reconnect.  Three things now hold together:

  * `POST /api/v1/ws/ticket` (key-protected, header only) mints a SHORT-LIVED ticket — an
    HMAC over an expiry stamp, keyed from the configured API key.  It is not the key, it
    cannot be turned back into the key, and it dies after `_TICKET_TTL_SECONDS`.
  * the handshake accepts `X-API-Key` (programmatic clients) or `?ticket=` (browsers, which
    cannot set handshake headers).  `?key=` is REFUSED with 1008 — it is not a supported form
    any more, and the refusal never echoes the value.
  * `_redact_query_string` scrubs credential-bearing pairs out of the ASGI scope BEFORE the
    handshake completes.  uvicorn formats its access line from that same scope object, so the
    line it emits for a legacy `?key=` client reads `/ws/activity` — the key is not in the log
    even for a client that still sends it.

Liveness (F-52, wave B2): the socket used to be silent and uncapped, and a Redis outage closed
it with code 1000 — a CLEAN close — so the client believed the session had ended normally and
never showed a fault.  Now:

  * `_heartbeat` sends `{"type":"ping","ts":...}` every `_HEARTBEAT_SECONDS`, so a dead path is
    detected by a failed send instead of a silent socket;
  * `_drain_client` holds the receive side open, so a client that leaves is noticed immediately
    (a client disconnect is a normal end: no close frame, no warning);
  * `_MAX_SOCKETS_PER_CLIENT` caps the sockets one CLIENT IDENTITY may hold, and
    `_MAX_SOCKETS_PER_OPERATOR` caps the install; the extra one is refused with 1013 (try again
    later), before accept.  The identity is the TICKET'S SUBJECT (one browser page/connection) or,
    for a header-authenticated client, the operator — **never the peer address**.  Keying on the
    address was R-W03-1: behind nginx every browser is the same peer (`172.18.0.9`), so four open
    sockets anywhere in the install starved every other page, and the app's dashboard plus its
    activity strip could exhaust the whole budget by themselves (measured: 8 refused handshakes on
    one page, `403` on every later attempt);
  * every BACKEND fault — the activity bus unreachable, the subscription ending, a failed send
    that is not a disconnect — closes with 1011 (internal error), never 1000.  The codes are
    module constants and are pinned by `tests/unit/test_b2_api.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.config import settings
from app.core import events
from app.core.security import api_key_matches, verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter()

#: The socket's close vocabulary.  Pinned by tests: the client branches on these codes.
WS_CLOSE_POLICY = 1008      # credential refused (and, since F-46, a key sent in the query string)
WS_CLOSE_FAULT = 1011       # a backend fault: the session did NOT end normally
WS_CLOSE_CAPACITY = 1013    # this client already holds the maximum number of sockets

#: Test seams / operator knobs, read from settings with defaults so no deployment needs a new env
#: var (and an unset setting keeps the shipped behaviour).
_TICKET_TTL_SECONDS = int(getattr(settings, "ws_ticket_ttl_seconds", 30))
_MAX_SOCKETS_PER_CLIENT = int(getattr(settings, "ws_max_sockets_per_client", 4))
#: The install-wide bound, per authenticated operator (R-W03-1).  Generous on purpose: the browser
#: mints ONE ticket per connection, so this is "how many activity sockets can this install hold at
#: once" — 32 pages, far above real use, and still a real bound on a leaked/reconnecting client.
_MAX_SOCKETS_PER_OPERATOR = int(getattr(settings, "ws_max_sockets_per_operator", 32))
_HEARTBEAT_SECONDS = float(getattr(settings, "ws_heartbeat_seconds", 20.0))

#: Query parameters that are CREDENTIALS: refused as auth, and scrubbed from the scope before the
#: server can log them.  `ticket` is deliberately NOT here — a ticket is the supported URL form and
#: is short-lived by construction.
_CREDENTIAL_QUERY_KEYS = frozenset({"key", "api_key", "api-key"})

#: Ticket format version.  v1 was `v1.<expires>.<sig>` and carried NO subject, so it could not
#: identify a client; v1 tickets are refused (they live 30 s, so a redeploy retires them anyway).
_TICKET_VERSION = "v2"
#: The subject's own bound: 1-64 URL-safe characters.  Minted here (`token_urlsafe(12)` = 16), so a
#: verified ticket's subject can never be hostile.
_SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Sockets currently open, per CLIENT IDENTITY (R-W03-1: never per peer address).  The identity is
#: `<operator>|s:<ticket subject>` for a ticket client, or `<operator>|s:-` for a header client.
#: Mutated only between awaits, so the asyncio loop keeps it consistent.
_open_sockets: dict[str, int] = {}

#: The same sockets counted per OPERATOR (`<operator>`), i.e. per install for the shipped
#: single-key deployment: the bound a client cannot escape by minting more tickets.
_operator_sockets: dict[str, int] = {}


class WsTicketResponse(BaseModel):
    """`POST /api/v1/ws/ticket` — a short-lived URL credential for the activity socket."""

    ticket: str
    expires_in: int


# ── the short-lived ticket (F-46) ─────────────────────────────────────────────────────────────


def _ticket_secret() -> bytes:
    """The signing key: derived from the configured API key, never equal to it.

    Derived rather than reused so that a leaked ticket reveals nothing about the key (HMAC is
    one-way) and a key rotation invalidates every outstanding ticket for free.
    """
    material = b"polsia-ws-ticket\x00" + (settings.api_key or "").encode("utf-8")
    return hashlib.sha256(material).digest()


def _ticket_signature(expires: int, subject: str) -> str:
    """HMAC over `v2.<expires>.<subject>` — the subject is SIGNED, so it cannot be swapped."""
    return hmac.new(
        _ticket_secret(), f"{_TICKET_VERSION}.{expires}.{subject}".encode("ascii"), hashlib.sha256
    ).hexdigest()


def mint_ws_ticket(*, now: float | None = None) -> tuple[str, int]:
    """Mint `(ticket, ttl_seconds)`.  `now` is a test seam; production uses the wall clock.

    R-W03-1: every mint carries a fresh random SUBJECT.  The browser mints one ticket per
    connection (`useActivityFeed.connect`), so the subject is the connection's identity — two
    pages behind the same nginx get two identities instead of sharing one proxy address.  The
    subject is public (it rides the URL) and reveals nothing: it is random, not derived from the
    owner or the key.
    """
    subject = secrets.token_urlsafe(12)
    expires = int(now if now is not None else time.time()) + _TICKET_TTL_SECONDS
    return f"{_TICKET_VERSION}.{expires}.{subject}.{_ticket_signature(expires, subject)}", (
        _TICKET_TTL_SECONDS
    )


def parse_ws_ticket(ticket: str | None, *, now: float | None = None) -> tuple[int, str] | None:
    """`(expires, subject)` for a ticket this process minted and that is still inside its TTL.

    The ONE place a ticket is read.  `verify_ws_ticket` is this predicate's boolean, and the cap
    key is built from the returned subject (so the identity a socket is counted under is a value
    whose signature was checked here, never a client-supplied string).

    A forged far-future expiry is refused too: the expiry may not exceed the TTL, so a hand-rolled
    `<ver>.9999999999.<subject>.<sig>` cannot buy an unlimited window even if the signature check
    were bypassed.  v1 tickets (no subject) are refused: they cannot identify a client.
    """
    if not ticket:
        return None
    parts = ticket.split(".")
    if len(parts) != 4 or parts[0] != _TICKET_VERSION:
        return None
    try:
        expires = int(parts[1])
    except ValueError:
        return None
    subject = parts[2]
    if not _SUBJECT_PATTERN.match(subject):
        return None
    current = now if now is not None else time.time()
    if expires < current:
        return None
    if expires > current + _TICKET_TTL_SECONDS + 1:
        return None
    if not hmac.compare_digest(parts[3], _ticket_signature(expires, subject)):
        return None
    return expires, subject


def verify_ws_ticket(ticket: str | None, *, now: float | None = None) -> bool:
    """True only for a valid, unexpired `v2` ticket (see `parse_ws_ticket`)."""
    return parse_ws_ticket(ticket, now=now) is not None


def operator_identity() -> str:
    """A stable, one-way identity for the configured owner key — the install's operator.

    Derived (a truncated sha256 of a domain-separated string) rather than the key itself: this
    value appears in cap keys and log lines, and it must never be usable as a credential.
    An OPEN backend (no key configured) has one anonymous operator, which is the honest answer.
    """
    material = b"polsia-ws-operator\x00" + (settings.api_key or "").encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _socket_identity(websocket: WebSocket, ticket: str | None) -> tuple[str, str]:
    """`(cap key, kind)` for this handshake — the identity a socket is COUNTED under.

    R-W03-1: `kind` is `session` when a verified ticket names a subject (one page/connection),
    `operator` when the client authenticated with the header (no subject: the operator's program
    owns it).  The peer address is NOT an identity: behind nginx it is the proxy, and keying on it
    made every browser share one budget.  The operator id is always the outer key, so no client can
    escape the install-wide bound by minting tickets.
    """
    parsed = parse_ws_ticket(ticket)
    if parsed is not None:
        return f"{operator_identity()}|s:{parsed[1]}", "session"
    return f"{operator_identity()}|s:-", "operator"


def _redact_query_string(scope: dict[str, Any]) -> None:
    """Remove credential-bearing pairs from the ASGI scope's query string, IN PLACE.

    WHY here and not in a logger filter: uvicorn's access line is formatted from `self.scope`, the
    very dict this handler is handed, at accept/close time.  Scrubbing the scope before the
    handshake completes is therefore the one place an application can keep a credential out of the
    line the server is about to emit.  Non-credential pairs are preserved (re-encoded).
    """
    raw = scope.get("query_string") or b""
    if not raw:
        return
    pairs = urllib.parse.parse_qsl(raw.decode("latin-1"), keep_blank_values=True)
    kept = [(name, value) for name, value in pairs if name not in _CREDENTIAL_QUERY_KEYS]
    if len(kept) == len(pairs):
        return
    scope["query_string"] = urllib.parse.urlencode(kept).encode("latin-1")


# ── the ticket route (F-46: the ONLY way a URL can carry a credential) ────────────────────────


@router.post(
    "/api/v1/ws/ticket",
    response_model=WsTicketResponse,
    tags=["ws"],
    dependencies=[Depends(verify_api_key)],
    summary="Mint a short-lived ticket for the /ws/activity socket",
)
async def mint_activity_ticket() -> WsTicketResponse:
    """Exchange the owner key (in the `X-API-Key` header) for a ticket the browser can carry.

    Browsers cannot set handshake headers; the ticket is the answer to that constraint that does
    not put the key in a URL.  It expires in `_TICKET_TTL_SECONDS` and is safe to appear in a log.
    """
    ticket, ttl = mint_ws_ticket()
    return WsTicketResponse(ticket=ticket, expires_in=ttl)


# ── the socket ────────────────────────────────────────────────────────────────────────────────


@router.websocket("/ws/activity")
async def activity_socket(websocket: WebSocket) -> None:
    # Read the query credentials FIRST: redaction rewrites the scope, and `query_params` is parsed
    # from that scope (and cached), so the values must be captured before the scrub.
    supplied_key = websocket.query_params.get("key")
    supplied_ticket = websocket.query_params.get("ticket")
    # A real ASGI websocket always carries `scope`; the isinstance check keeps unit fakes that
    # model only the members the handler reads (tests/unit/api/test_ws_activity.py) working.
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict):
        _redact_query_string(scope)

    if supplied_key is not None:
        # An API key in a URL is the defect, not a credential: refuse it, and never echo it.
        await websocket.close(code=WS_CLOSE_POLICY)
        logger.warning(
            "activity websocket refused: the API key was sent in the query string; mint a "
            "short-lived ticket at POST /api/v1/ws/ticket and connect with ?ticket= "
            "(or send the X-API-Key header)"
        )
        return

    # Refuse BEFORE accept(): an unauthenticated subscriber never gets a working socket.  Same
    # semantics as every REST route (no key configured => open; blank counts as absent) via the ONE
    # shared matcher, plus the ticket form for browsers.
    if not (api_key_matches(websocket.headers.get("x-api-key")) or verify_ws_ticket(supplied_ticket)):
        await websocket.close(code=WS_CLOSE_POLICY)
        logger.warning("activity websocket refused: missing/invalid credential")
        return

    # R-W03-1: the identity is the ticket's SUBJECT (or the operator for a header client), never
    # the peer address — behind nginx every browser is the same peer, and the old keying let four
    # open sockets anywhere starve every page.  Refused BEFORE accept, exactly as before.
    cap_key, cap_kind = _socket_identity(websocket, supplied_ticket)
    operator_key = operator_identity()
    session_count = _open_sockets.get(cap_key, 0)
    operator_count = _operator_sockets.get(operator_key, 0)
    if session_count >= _MAX_SOCKETS_PER_CLIENT:
        await websocket.close(code=WS_CLOSE_CAPACITY)
        logger.warning(
            "activity websocket refused: %s identity already holds %d open socket(s) (cap %d); "
            "the peer address is not used for this",
            cap_kind, session_count, _MAX_SOCKETS_PER_CLIENT,
        )
        return
    if operator_count >= _MAX_SOCKETS_PER_OPERATOR:
        await websocket.close(code=WS_CLOSE_CAPACITY)
        logger.warning(
            "activity websocket refused: this install already holds %d open activity socket(s) "
            "(cap %d for the operator)",
            operator_count, _MAX_SOCKETS_PER_OPERATOR,
        )
        return

    _open_sockets[cap_key] = session_count + 1
    _operator_sockets[operator_key] = operator_count + 1
    try:
        await websocket.accept()
        await _serve(websocket)
    finally:
        remaining = _open_sockets.get(cap_key, 1) - 1
        if remaining > 0:
            _open_sockets[cap_key] = remaining
        else:
            _open_sockets.pop(cap_key, None)
        remaining_operator = _operator_sockets.get(operator_key, 1) - 1
        if remaining_operator > 0:
            _operator_sockets[operator_key] = remaining_operator
        else:
            _operator_sockets.pop(operator_key, None)


async def _serve(websocket: WebSocket) -> None:
    """Stream the feed until the client leaves (normal) or the backend faults (1011)."""
    pubsub = None
    tasks: list[asyncio.Task[None]] = []
    sender = asyncio.Lock()
    # The DEFAULT is a fault: only an ended client turns this into a clean, code-less exit.
    close_code: int | None = WS_CLOSE_FAULT
    try:
        pubsub = await events.subscribe_activity()
        tasks = [
            asyncio.create_task(_drain_client(websocket), name="activity-client"),
            asyncio.create_task(_relay(websocket, pubsub, sender), name="activity-relay"),
            asyncio.create_task(_heartbeat(websocket, sender), name="activity-heartbeat"),
        ]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, WebSocketDisconnect):
                close_code = None  # the client left: nothing to report, nothing to close
            elif exc is not None:
                logger.warning("activity websocket fault in %s", task.get_name(), exc_info=exc)
            elif task.get_name() == "activity-client":
                close_code = None
            else:
                # The relay or the heartbeat finished without raising: the bus ended or the socket
                # stopped accepting sends.  Either way the session did NOT end normally.
                logger.warning("activity websocket fault: %s ended the session", task.get_name())
    except WebSocketDisconnect:
        close_code = None
    except Exception:  # noqa: BLE001 - a socket error must never crash the server
        logger.warning("activity websocket fault: the activity bus is unreachable", exc_info=True)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if pubsub is not None:
            with contextlib.suppress(Exception):  # best-effort teardown; never mask the real error
                await pubsub.unsubscribe(events.ACTIVITY_CHANNEL)
                await pubsub.close()
        if close_code is not None:
            with contextlib.suppress(Exception):  # the peer may already be gone
                await websocket.close(code=close_code)


async def _drain_client(websocket: WebSocket) -> None:
    """Hold the receive side open so a disconnect is noticed NOW, not at the next send.

    The feed is server-push: frames from the client are ignored (there is no client protocol).
    Returns (via `WebSocketDisconnect`) only when the client goes away, which `_serve` reads as a
    normal end.
    """
    while True:
        await websocket.receive_text()


async def _relay(websocket: WebSocket, pubsub: Any, sender: asyncio.Lock) -> None:
    """Forward every activity message; a lock keeps the heartbeat and the relay off each other's
    frames (starlette supports one send at a time per socket)."""
    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        async with sender:
            await websocket.send_text(message.get("data") or "")


async def _heartbeat(websocket: WebSocket, sender: asyncio.Lock) -> None:
    """Keepalive: a silent socket cannot tell a dead path from a quiet feed."""
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        async with sender:
            await websocket.send_text(json.dumps({"type": "ping", "ts": time.time()}))

