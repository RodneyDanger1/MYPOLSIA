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
  * `_MAX_SOCKETS_PER_CLIENT` caps the sockets one client address may hold; the extra one is
    refused with 1013 (try again later), before accept;
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
_HEARTBEAT_SECONDS = float(getattr(settings, "ws_heartbeat_seconds", 20.0))

#: Query parameters that are CREDENTIALS: refused as auth, and scrubbed from the scope before the
#: server can log them.  `ticket` is deliberately NOT here — a ticket is the supported URL form and
#: is short-lived by construction.
_CREDENTIAL_QUERY_KEYS = frozenset({"key", "api_key", "api-key"})

#: Sockets currently open, per client address.  Single-user install: the API key is global, so the
#: only per-client identity the handshake carries is the peer address — that is what "per user"
#: means here.  Mutated only between awaits, so the asyncio loop keeps it consistent.
_open_sockets: dict[str, int] = {}


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


def _ticket_signature(expires: int) -> str:
    return hmac.new(_ticket_secret(), f"v1.{expires}".encode("ascii"), hashlib.sha256).hexdigest()


def mint_ws_ticket(*, now: float | None = None) -> tuple[str, int]:
    """Mint `(ticket, ttl_seconds)`.  `now` is a test seam; production uses the wall clock."""
    expires = int(now if now is not None else time.time()) + _TICKET_TTL_SECONDS
    return f"v1.{expires}.{_ticket_signature(expires)}", _TICKET_TTL_SECONDS


def verify_ws_ticket(ticket: str | None, *, now: float | None = None) -> bool:
    """True only for a ticket this process minted, still inside its TTL.

    A forged far-future expiry is refused too: the expiry may not exceed the TTL, so a hand-rolled
    `v1.9999999999.<sig>` cannot buy an unlimited window even if the signature check were bypassed.
    """
    if not ticket:
        return False
    parts = ticket.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if expires < current:
        return False
    if expires > current + _TICKET_TTL_SECONDS + 1:
        return False
    return hmac.compare_digest(parts[2], _ticket_signature(expires))


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

    client = getattr(websocket, "client", None)
    client_key = client.host if client is not None else "unknown"
    if _open_sockets.get(client_key, 0) >= _MAX_SOCKETS_PER_CLIENT:
        await websocket.close(code=WS_CLOSE_CAPACITY)
        logger.warning(
            "activity websocket refused: %s already holds %d open socket(s) (cap %d)",
            client_key, _open_sockets.get(client_key, 0), _MAX_SOCKETS_PER_CLIENT,
        )
        return

    _open_sockets[client_key] = _open_sockets.get(client_key, 0) + 1
    try:
        await websocket.accept()
        await _serve(websocket)
    finally:
        remaining = _open_sockets.get(client_key, 1) - 1
        if remaining > 0:
            _open_sockets[client_key] = remaining
        else:
            _open_sockets.pop(client_key, None)


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

