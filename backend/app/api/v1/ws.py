"""WebSocket /ws/activity - relays the Redis pub/sub activity channel.

nginx.conf proxies /ws/ to the backend; frontend/src/hooks/useActivityFeed.ts
connects to `${WS_URL}/ws/activity`.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import events

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/activity")
async def activity_socket(websocket: WebSocket) -> None:
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
            try:
                await pubsub.unsubscribe(events.ACTIVITY_CHANNEL)
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
