"""Redis pub/sub event bus for the /ws/activity WebSocket feed.

`get_redis` is imported into THIS namespace on purpose: the shipped conftest patches
`app.core.events.get_redis` (CLAUDE.md: patch where the name is used).

Outage behaviour (measured, then fixed).  `publish_activity` always caught its exception so a Redis
outage could never break the DB write — that promise was kept.  What was missing was a latency bound
and a quiet: with redis-py's default connection retry ladder and no socket timeout, EVERY event waited
out the ladder (seconds, per event, on the path every agent action uses) and logged a full traceback
(78 events -> 3,510 log lines).  Now the client is bounded (see `app/core/redis_client.py`) and this
module keeps a circuit breaker: the first failure opens it for `_PUBLISH_COOLDOWN_S`, later events are
SKIPPED and COUNTED, and one success closes it.

A skip is never silent: `publish_stats()` reports failures, skips, breaker state and the last error.
It is a function rather than a `/health` field because that payload is frozen at exactly
`{"status": "ok"}` by the schema and its test.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.core.redis_client import get_redis  # noqa: F401  (re-exported for patching)

logger = logging.getLogger(__name__)

ACTIVITY_CHANNEL = settings.activity_channel

# How long a failed publish mutes further attempts.  Setting it to 0 disables the breaker — that is
# also this file's RED leg: with the cooldown at 0 the breaker can never skip, and
# `test_breaker_skips_after_a_failure_and_counts_the_skips` fails on the attempt count.
_PUBLISH_COOLDOWN_S = float(getattr(settings, "activity_publish_cooldown_seconds", 30.0))

_publish_blocked_until: float = 0.0
_publish_failures: int = 0
_publish_skipped: int = 0
_last_publish_error: str | None = None


def publish_stats() -> dict[str, Any]:
    """Outage visibility: failed and skipped publishes are COUNTED, never swallowed."""
    return {
        "failures": _publish_failures,
        "skipped_while_open": _publish_skipped,
        "breaker_open": time.monotonic() < _publish_blocked_until,
        "cooldown_seconds": _PUBLISH_COOLDOWN_S,
        "last_error": _last_publish_error,
    }


def reset_publish_breaker() -> None:
    """Clear breaker state — for tests, and for an operator after fixing a Redis incident."""
    global _publish_blocked_until, _publish_failures, _publish_skipped, _last_publish_error
    _publish_blocked_until = 0.0
    _publish_failures = 0
    _publish_skipped = 0
    _last_publish_error = None


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def serialize_event(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default, sort_keys=True)


async def publish_activity(payload: dict[str, Any]) -> None:
    """Publish one activity event.  Called exactly ONCE per log_activity().

    While the breaker is open this returns immediately having recorded a skip, so an outage costs one
    bounded attempt per cooldown window instead of one stalled attempt per event.
    """
    global _publish_blocked_until, _publish_failures, _publish_skipped, _last_publish_error

    now = time.monotonic()
    if now < _publish_blocked_until:
        _publish_skipped += 1
        return

    client = get_redis()
    message = serialize_event(payload)
    try:
        await client.publish(ACTIVITY_CHANNEL, message)
    except Exception as exc:  # never let a Redis outage break the DB write
        _publish_failures += 1
        _last_publish_error = f"{type(exc).__name__}: {exc}"
        _publish_blocked_until = now + _PUBLISH_COOLDOWN_S
        if _publish_failures == 1:
            # One full traceback per outage window; a traceback per event buries the real error and
            # costs more than the failed publish did.
            logger.warning(
                "activity publish failed (%s); muting further attempts for %.0fs — the DB write is "
                "unaffected and the skips are counted in publish_stats()",
                _last_publish_error,
                _PUBLISH_COOLDOWN_S,
                exc_info=True,
            )
        else:
            logger.warning(
                "activity publish still failing (%s); breaker reopened for %.0fs "
                "(failures=%d, skipped=%d)",
                _last_publish_error,
                _PUBLISH_COOLDOWN_S,
                _publish_failures,
                _publish_skipped,
            )
    else:
        if _publish_blocked_until:
            logger.info("activity publish recovered; breaker closed")
        _publish_blocked_until = 0.0


def activity_payload(
    *,
    id: int | None = None,
    agent_type: str,
    action: str,
    summary: str,
    level: str | None = "info",
    created_at: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wire shape consumed by frontend/src/lib/api.ts ActivityEvent.

    `level` accepts None because the activity_log column is DDL-nullable (A-T3): the normalize
    below is the single boundary that guarantees the wire vocabulary stays one word — "info".
    """
    # NULL means the writer did not set a level; it is not an outage. Normalized here so the
    # payload vocabulary stays one word ("info") — see the A-T3 decision in the lane receipt.
    if level is None:
        level = "info"
    return {
        "id": id,
        "agent_type": agent_type,
        "action": action,
        "summary": summary,
        "level": level,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "detail": detail,
    }


async def subscribe_activity():
    """Return a pubsub object already subscribed to the activity channel."""
    client = get_redis()
    pubsub = client.pubsub()
    await pubsub.subscribe(ACTIVITY_CHANNEL)
    return pubsub
