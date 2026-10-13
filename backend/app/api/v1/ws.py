"""WebSocket /ws/activity - relays the Redis pub/sub activity channel.

nginx.conf proxies /ws/ to the backend; frontend/src/hooks/useActivityFeed.ts
connects to `${WS_URL}/ws/activity?key=...`.

Auth (audit L1-01/L3-08): the REST twin `GET /api/v1/dashboard/activity` is key-protected;
this socket checks the SAME key through `api_key_matches` before accepting. Browsers cannot
set handshake headers, so the key travels as a query parameter; the header form is accepted
too for programmatic clients.
"""
from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import events
from app.core.security import api_key_matches

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/activity")
async def activity_socket(websocket: WebSocket) -> None:
    # Refuse BEFORE accept(): an unauthenticated subscriber never gets a working socket. The key
    # rides the query string because browser WebSocket clients cannot set handshake headers; the
    # header form stays available for programmatic clients. Same semantics as every REST route
    # (no key configured => open; blank counts as absent) via the ONE shared matcher.
    provided = websocket.query_params.get("key") or websocket.headers.get("x-api-key")
    if not api_key_matches(provided):
        await websocket.close(code=1008)
        logger.warning("activity websocket refused: missing/invalid key")
        return
    await websocket.accept()
    pubsub = None
    try:
        pubsub = await events.subscribe_activity()
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            await websocket.send_text(message.get("data") or "")
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never crash the server on a socket error
        logger.warning("activity websocket error", exc_info=True)
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):  # best-effort teardown; never mask the real error
                await pubsub.unsubscribe(events.ACTIVITY_CHANNEL)
                await pubsub.close()
        with contextlib.suppress(Exception):  # best-effort teardown; never mask the real error
            await websocket.close()
