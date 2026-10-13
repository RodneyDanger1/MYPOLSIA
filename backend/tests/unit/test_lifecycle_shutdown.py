"""RD4 item 5 — shutdown closes the Redis client, alongside the DB engine.

`main.lifespan`'s `finally` disposed the DB engine and left the shared async Redis client's
connection pool to the OS.  `app/core/redis_client.close_redis()` already existed (it closes the
client when one was ever created and is a no-op otherwise — the client is lazy), so the wiring, not
a new helper, was the missing piece.

The test drives the REAL lifespan through `starlette.testclient.TestClient` as a context manager
(that is what actually runs startup/shutdown; a plain `TestClient(app)` does not — see
tests/unit/api/test_ws_auth.py:23) and spies on the real functions.

RED leg (measured this session; lane receipt build/RD4-unwired/RECEIPT.md): deleting the
`await close_redis()` line makes this test fail with `order == ["engine"]`.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app


def test_shutdown_closes_redis_after_disposing_the_engine(mocker):
    order: list[str] = []

    async def _close_redis() -> None:
        order.append("redis")

    async def _dispose_engine() -> None:
        order.append("engine")

    mocker.patch("app.main.close_redis", side_effect=_close_redis)
    mocker.patch("app.main.dispose_engine", side_effect=_dispose_engine)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200, "the lifespan must have started the app"

    assert order == ["engine", "redis"], (
        "shutdown must dispose the DB engine and then close the redis client"
    )
