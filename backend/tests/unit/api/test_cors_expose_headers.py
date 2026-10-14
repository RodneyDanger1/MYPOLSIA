"""V7-T07 — `X-Polsia-Row-Count` joins the CORS-exposed headers (wire level).

`GET /api/v1/runs/export` declares its total in `X-Polsia-Row-Count`
(app/api/v1/runs.py — the declared-total law the usage/export UI consumes).  A
cross-origin browser client (the shipped `cors_origins` pair) must be able to READ it,
exactly like the refusal/timing headers the L1-08 fix exposed.  Mirrors the wire-level
style of tests/unit/api/test_fix5_cors_expose.py.

Raw RED evidence: raw/v7-red-tests.txt in the lane dir.
"""
from __future__ import annotations

import pytest

from app.config import settings

#: Every custom response header the app emits that a cross-origin JS client must read.
EXPOSED = {
    "x-polsia-error-code",
    "x-request-id",
    "x-response-time-ms",
    "x-polsia-row-count",
}


def _exposed(resp) -> set[str]:
    raw = resp.headers.get("access-control-expose-headers") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@pytest.mark.asyncio
async def test_cross_origin_200_exposes_the_row_count_header(api_client):
    origin = settings.cors_origins[0]
    resp = await api_client.get("/api/v1/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert "x-polsia-row-count" in _exposed(resp), (
        resp.headers.get("access-control-expose-headers")
    )


@pytest.mark.asyncio
async def test_the_full_exposed_set_is_readable_cross_origin(api_client):
    origin = settings.cors_origins[0]
    resp = await api_client.get("/api/v1/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert _exposed(resp) >= EXPOSED, resp.headers.get("access-control-expose-headers")
