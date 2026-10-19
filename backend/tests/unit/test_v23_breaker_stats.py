"""V23/L4 — the worker process's publish-breaker counters: written by the tick, served by the route.

THE GAP THIS CLOSES (COMPLETENESS #8).  `events.publish_stats()` counts failed and skipped activity
publishes, but its counters are module globals in the CELERY WORKER process — a different process
from the API, so `GET /ops/events/stats` can only ever show the API's own numbers.  The 15-minute
tick wrote the worker's stats into the worker log and nowhere else: no operator surface could show
them.  The freeze for the wave (V23-PLAN.md, L4) is:

    the tick writes `{failures, skipped_while_open, breaker_open, ts}` to a shared Redis key
    (TTL ~15 min); `GET /api/v1/ops/activity-publish` returns `200 {data: {...}}` when the worker
    has published, `200 {data: null}` when it never has (NEVER fabricated zeros), and a named
    `E_REDIS` 503 when Redis is unreachable.

RED-FIRST.  Before this wave's code: the route did not exist (404), the key constants did not exist
(the module-level import below fails at collection), and the tick performed no Redis write.  Each
test below fails for the missing behaviour, never for a mock that disagrees with itself.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from unittest.mock import AsyncMock

from app.core.redis_client import (
    ACTIVITY_PUBLISH_STATS_KEY,
    ACTIVITY_PUBLISH_STATS_TTL_SECONDS,
)

ROUTE = "/api/v1/ops/activity-publish"
LOGGER_NAME = "celery_app.tasks.maintenance"


def payload(**over: object) -> dict[str, object]:
    """The frozen worker write shape, as the tick emits it."""
    body: dict[str, object] = {
        "failures": 2,
        "skipped_while_open": 7,
        "breaker_open": False,
        "ts": "2026-10-19T17:05:00+00:00",
    }
    body.update(over)
    return body


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 1. The maintenance tick publishes its own stats — with a TTL, and never at the job's expense
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_health_tick_writes_the_frozen_stats_shape_with_a_ttl(mock_redis):
    """The worker's half of the contract: one SETEX-shaped write of the four frozen keys.

    Every field is compared against the task's OWN returned stats, so the key cannot silently
    drift from what the tick reported in its result/log.
    """
    from celery_app.tasks.maintenance import check_activity_publish_health

    result = check_activity_publish_health()

    assert result["status"] == "completed"
    mock_redis.set.assert_awaited_once()
    args, kwargs = mock_redis.set.call_args
    assert args[0] == ACTIVITY_PUBLISH_STATS_KEY

    written = json.loads(args[1])
    assert set(written) == {"failures", "skipped_while_open", "breaker_open", "ts"}
    assert written["failures"] == result["stats"]["failures"]
    assert written["skipped_while_open"] == result["stats"]["skipped_while_open"]
    assert written["breaker_open"] is result["stats"]["breaker_open"]
    # `ts` is a real, timezone-aware instant — the card prints it as the publish time.
    assert datetime.fromisoformat(written["ts"]).tzinfo is not None
    # TTL ~15 minutes: the key expires with the beat period it describes.
    assert ACTIVITY_PUBLISH_STATS_TTL_SECONDS == 15 * 60
    assert kwargs["ex"] == ACTIVITY_PUBLISH_STATS_TTL_SECONDS


def test_a_redis_refusal_never_fails_the_maintenance_job(mock_redis, caplog):
    """Redis down: the tick still completes, still reports its stats, and NAMES the failed write."""
    from celery_app.tasks.maintenance import check_activity_publish_health

    mock_redis.set = AsyncMock(side_effect=ConnectionError("redis refused the write"))

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = check_activity_publish_health()

    assert result["status"] == "completed"
    assert result["clean"] is True
    assert "stats" in result
    assert "redis refused the write" in caplog.text, (
        "a failed publish of the stats must leave a trace, not vanish"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 2. GET /ops/activity-publish — the worker's payload, an honest null, or a named E_REDIS
# ═══════════════════════════════════════════════════════════════════════════════════════════════


async def test_the_route_serves_the_workers_published_payload(api_client, auth_headers, mock_redis):
    mock_redis.get.return_value = json.dumps(payload())

    resp = await api_client.get(ROUTE, headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {"data": payload()}
    mock_redis.get.assert_awaited_once_with(ACTIVITY_PUBLISH_STATS_KEY)


async def test_the_route_reports_null_when_the_worker_never_wrote(api_client, auth_headers, mock_redis):
    """`data: null` — the honest absence.  Zeros here would read as a clean, measured worker."""
    mock_redis.get.return_value = None

    resp = await api_client.get(ROUTE, headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"data": None}
    assert "failures" not in json.dumps(body)


async def test_the_route_names_redis_when_the_dependency_is_unreachable(
    api_client, auth_headers, mock_redis
):
    mock_redis.get = AsyncMock(side_effect=ConnectionError("connection refused"))

    resp = await api_client.get(ROUTE, headers=auth_headers)

    assert resp.status_code == 503
    assert resp.headers["X-Polsia-Error-Code"] == "E_REDIS"
    # The app's named-refusal envelope (app/main.py) — the same shape every other refusal uses.
    error = resp.json()["error"]
    assert error["code"] == "E_REDIS"
    assert "connection refused" in error["message"]


async def test_an_unreadable_value_is_named_not_rendered_as_zero(api_client, auth_headers, mock_redis):
    """Bytes that are not the frozen shape cannot be rendered as counters — they are refused."""
    mock_redis.get.return_value = "{not json"

    resp = await api_client.get(ROUTE, headers=auth_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "E_REDIS"


async def test_a_hostile_value_is_not_coerced_into_a_count(api_client, auth_headers, mock_redis):
    """`"2"` / `True` are not the worker's own JSON types; coercing them would invent a reading."""
    mock_redis.get.return_value = json.dumps(payload(failures="2"))

    resp = await api_client.get(ROUTE, headers=auth_headers)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "E_REDIS"


async def test_the_route_is_behind_the_router_credential(api_client, mock_redis):
    resp = await api_client.get(ROUTE)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"
