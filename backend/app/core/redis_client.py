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

import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings

_CONNECT_TIMEOUT_S = float(getattr(settings, "redis_connect_timeout_seconds", 1.0))
_SOCKET_TIMEOUT_S = float(getattr(settings, "redis_socket_timeout_seconds", 1.0))

_client: aioredis.Redis | None = None


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


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
