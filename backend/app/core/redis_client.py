"""Redis client factory.

Convention (load-bearing for the shipped mock_redis fixture): `get_redis()` is a
plain SYNCHRONOUS function returning an async client; only the client's own methods
are awaited.  `mocker.patch("app.core.redis_client.get_redis", return_value=mock)`
replaces the function with a MagicMock whose return_value is the AsyncMock, so
callers must do `client = get_redis(); await client.publish(...)` -- never
`await get_redis()`.

Transport bounds (measured, then fixed).  `from_url` with only url/encoding/decode_responses left
redis-py on its defaults: **no socket timeout and a connection retry ladder (ExponentialBackoff,
3 retries) that `await client.publish(...)` waits out inside the call**.  With Redis absent, that made
every activity write stall for seconds AND log a full traceback — 78 events produced 3,510 lines of
log on the path every agent action uses (found by the 200k-row shipped-code benchmark, which had to be
abandoned because of it).  A 1s connect timeout, a 1s socket timeout and `Retry(NoBackoff(), 0)` turn
"stall for seconds per event" into "fail in ≤1s, then the caller's breaker owns recovery".

These two knobs are read with a getattr fallback so `app/config.py` (a frozen interface owned by the
campaign, and asserted field-by-field by the parity suite) does not have to change for a transport
detail — same pattern the service layer uses for `stripe_signature_tolerance`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings

_CONNECT_TIMEOUT_S = float(getattr(settings, "redis_connect_timeout_seconds", 1.0))
_SOCKET_TIMEOUT_S = float(getattr(settings, "redis_socket_timeout_seconds", 1.0))

_client: aioredis.Redis | None = None
_subscriber: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return a process-wide async Redis client (never connects here)."""
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT_S,
            socket_timeout=_SOCKET_TIMEOUT_S,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )
    return _client


def get_redis_subscriber() -> aioredis.Redis:
    """Return the SUBSCRIBER client: same process, one knob deliberately different — no socket read timeout.

    Why (U6-F01, measured live): the bounded client above is right for PUBLISH and request paths —
    fail in <=1s and let the caller's breaker own recovery.  A pubsub READER is the opposite
    workload: `pubsub.listen()` blocks between messages by design, so the 1s socket timeout fired
    on every quiet second, redis-py raised `TimeoutError`, and `app/api/v1/ws.py` treated it as a
    socket error — every dashboard visit showed `connection closed (code 1000)` + RECONNECTING and
    the feed never streamed a live event.  This client keeps a bounded CONNECT (a dead Redis still
    refuses fast) but leaves the read blocking: a real outage closes the TCP stream, the relay
    ends, and the frontend's shipped reconnect logic runs — the behaviour its states were built for.
    """
    global _subscriber
    if _subscriber is None:
        _subscriber = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT_S,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )
    return _subscriber


# ── the worker's publish-breaker snapshot (V23/L4, COMPLETENESS #8) ──────────────────────────
#
# The activity breaker's counters are module globals in `app.core.events`, i.e. PER PROCESS.  The
# API's own numbers are served by GET /ops/events/stats; the CELERY WORKER's numbers used to exist
# only in the worker log.  `check_activity_publish_health` (the 15-minute beat tick) now publishes
# its snapshot here and `GET /ops/activity-publish` reads the SAME key — the one bridge between the
# two processes' views.  The TTL is one beat period: a key that expired is a worker that stopped
# publishing, and the route reports `data: null` rather than serving a stale snapshot as current.

#: The shared key the worker writes and the ops route reads.
ACTIVITY_PUBLISH_STATS_KEY = "polsia:activity-publish:stats"
#: One beat period (15 min): the snapshot outlives nothing it did not measure.
ACTIVITY_PUBLISH_STATS_TTL_SECONDS = 15 * 60


async def write_activity_publish_stats(stats: Mapping[str, Any]) -> None:
    """Publish the worker's stats snapshot under the shared key, with the beat-period TTL.

    RAISES on a Redis failure ON PURPOSE: only the caller knows whether the write is allowed to
    fail its job.  The maintenance tick catches it and continues (a broken observability write must
    never take the maintenance sweep down); a future caller with a different contract can decide
    for itself instead of inheriting a silent swallow.
    """
    await get_redis().set(
        ACTIVITY_PUBLISH_STATS_KEY,
        json.dumps(dict(stats), sort_keys=True),
        ex=ACTIVITY_PUBLISH_STATS_TTL_SECONDS,
    )


async def close_redis() -> None:
    global _client, _subscriber
    if _client is not None:
        # types-redis 4.6 stubs lag redis-py 5's async `aclose` (the documented close API);
        # CI's mypy 1.13 sees the stub gap, local 2.x does not — keep the real call, pin the ignore.
        await _client.aclose()  # type: ignore[attr-defined]  # stubs lag redis-py 5
        _client = None
    if _subscriber is not None:
        await _subscriber.aclose()  # type: ignore[attr-defined]  # stubs lag redis-py 5
        _subscriber = None
