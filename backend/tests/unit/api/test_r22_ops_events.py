"""R22 — /api/v1/ops/events/{stats,reset}: owner auth, honest shape, and the REAL breaker state.

The breaker counters are module globals in `app.core.events`; these tests set and clear them
through the real module (imported, not replaced), so the routes are proven to read/write the live
state a Redis incident produces — not a stub that happens to agree with itself.
"""
from __future__ import annotations

import pytest

from app.core import events

STATS_KEYS = {"failures", "skipped_while_open", "breaker_open", "cooldown_seconds", "last_error"}


@pytest.fixture(autouse=True)
def _isolate_breaker():
    """Breaker state is module-global; no test may inherit another test's incident."""
    events.reset_publish_breaker()
    yield
    events.reset_publish_breaker()


@pytest.mark.asyncio
async def test_both_verbs_require_the_owner_key(api_client):
    """No key -> the standard E_AUTH refusal envelope on BOTH verbs."""
    for method, path in (
        ("get", "/api/v1/ops/events/stats"),
        ("post", "/api/v1/ops/events/reset"),
    ):
        resp = await getattr(api_client, method)(path)
        assert resp.status_code == 401, f"{method} {path} without a key must be 401"
        body = resp.json()
        assert body["ok"] is False and body["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_stats_route_serves_the_breaker_shape(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/events/stats", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == STATS_KEYS
    assert body == {
        "failures": 0,
        "skipped_while_open": 0,
        "breaker_open": False,
        "cooldown_seconds": events._PUBLISH_COOLDOWN_S,
        "last_error": None,
    }


@pytest.mark.asyncio
async def test_stats_route_reports_a_dirty_breaker(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(events, "_publish_failures", 3)
    monkeypatch.setattr(events, "_last_publish_error", "ConnectionError: refused")

    body = (await api_client.get("/api/v1/ops/events/stats", headers=auth_headers)).json()

    assert body["failures"] == 3
    assert body["last_error"] == "ConnectionError: refused"


@pytest.mark.asyncio
async def test_reset_clears_the_counters_and_the_following_read_agrees(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(events, "_publish_failures", 5)
    monkeypatch.setattr(events, "_publish_skipped", 9)

    resp = await api_client.post("/api/v1/ops/events/reset", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["failures"] == 0
    assert body["skipped_while_open"] == 0
    assert body["breaker_open"] is False
    assert body["last_error"] is None

    after = (await api_client.get("/api/v1/ops/events/stats", headers=auth_headers)).json()
    assert after == body, "the reset acted on the live module state, not a copy"
