"""FIXER-5 / L1-08 — CORS exposes the app's custom response headers.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l1.txt): with the shipped
`cors_origins` (`http://localhost`, `http://localhost:3000`) and an allowed Origin, both the
200 and the 401 legs answered `access-control-expose-headers: None` — so a browser JS client
could not read `X-Polsia-Error-Code` / `X-Request-ID` / `X-Response-Time-ms`, while the
shipped error page tells the operator to look up the X-Request-ID.  The three names are
exactly the custom headers the app greps to (main.py middleware + `_refusal_headers` +
`deps.as_http_exception`); `WWW-Authenticate` is a standard header and not exposed here.
"""
from __future__ import annotations

import pytest

from app.config import settings

EXPOSED = {"x-polsia-error-code", "x-request-id", "x-response-time-ms"}


def _exposed(resp) -> set[str]:
    raw = resp.headers.get("access-control-expose-headers") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@pytest.mark.asyncio
async def test_cross_origin_200_exposes_the_custom_headers(api_client):
    origin = settings.cors_origins[0]
    resp = await api_client.get("/api/v1/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert _exposed(resp) >= EXPOSED, resp.headers.get("access-control-expose-headers")


@pytest.mark.asyncio
async def test_cross_origin_401_exposes_the_custom_headers(api_client):
    """The refusal leg: the code header must be READABLE when it is the payload that matters."""
    origin = settings.cors_origins[0]
    resp = await api_client.get("/api/v1/config", headers={"Origin": origin})
    assert resp.status_code == 401
    assert resp.headers.get("x-polsia-error-code") == "E_AUTH"
    assert _exposed(resp) >= EXPOSED, resp.headers.get("access-control-expose-headers")


@pytest.mark.asyncio
async def test_no_origin_gets_no_cors_headers(api_client, auth_headers):
    """Same-origin / non-browser callers see exactly the pre-fix wire (no CORS add-ons)."""
    resp = await api_client.get("/api/v1/health", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers.get("access-control-expose-headers") is None
    assert resp.headers.get("access-control-allow-origin") is None


@pytest.mark.asyncio
async def test_disallowed_origin_gets_no_allow_origin_header(api_client):
    """For an un-allowed origin the BROWSER-side gate is the missing ACAO header.

    (Starlette 0.41 emits the middleware's `simple_headers` — which include
    `Access-Control-Expose-Headers` — on every CORS-processed response regardless of the
    origin; without `Access-Control-Allow-Origin` a browser still refuses to hand the
    response to JS, so the meaningful assertion is the absent ACAO.)
    """
    resp = await api_client.get(
        "/api/v1/health", headers={"Origin": "http://not-allowed.invalid"}
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is None
