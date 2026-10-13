"""FIXER-5 / L1-04 — a coded `HTTPException` carries `X-Polsia-Error-Code` too.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l1.txt): GET
/api/v1/tasks/999999 answered `404 E_NOT_FOUND` WITHOUT the machine-readable header while
DELETE /api/v1/memory/999999 (the `refusal_guard` path) answered it WITH — the exact split
`main._refusal_headers` exists to make impossible ("a client must not see a different
response for the same refusal just because it was raised a layer earlier").

These legs run through the REAL ASGI stack (`api_client` → ASGITransport → the request
middleware + both exception handlers), so what they measure is the production response
plumbing, exactly like `test_r29_status_parity.py` does for the `PolsiaError` paths.
"""
from __future__ import annotations

import pytest

from app.core.errors import E_AUTH, E_NOT_FOUND


@pytest.mark.asyncio
async def test_direct_raise_tasks_404_carries_the_code_header(api_client, auth_headers):
    resp = await api_client.get("/api/v1/tasks/999999", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == E_NOT_FOUND
    assert resp.headers.get("X-Polsia-Error-Code") == E_NOT_FOUND


@pytest.mark.asyncio
async def test_direct_raise_config_unseeded_carries_the_code_header(api_client, auth_headers):
    resp = await api_client.get("/api/v1/config", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == E_NOT_FOUND
    assert resp.headers.get("X-Polsia-Error-Code") == E_NOT_FOUND


@pytest.mark.asyncio
async def test_direct_raise_and_guard_path_agree_header_for_header(api_client, auth_headers):
    """The two raise sites of the SAME refusal must answer the same wire shape."""
    direct = await api_client.get("/api/v1/tasks/999999", headers=auth_headers)
    guarded = await api_client.delete("/api/v1/memory/999999", headers=auth_headers)

    assert direct.status_code == guarded.status_code == 404
    assert (
        direct.headers.get("X-Polsia-Error-Code")
        == guarded.headers.get("X-Polsia-Error-Code")
        == E_NOT_FOUND
    )
    assert direct.json()["error"]["code"] == guarded.json()["error"]["code"] == E_NOT_FOUND
    assert set(direct.json()) == set(guarded.json()) == {"ok", "error"}


@pytest.mark.asyncio
async def test_uncoded_http_exceptions_keep_the_default_shape_and_no_header(api_client):
    """The negative control: `setdefault` must not turn every 404 into a refusal envelope.

    An UNCODED `HTTPException` (a plain route miss) goes straight back to FastAPI's own
    handler — byte-identical default body, and no `X-Polsia-Error-Code` invented for it.
    """
    resp = await api_client.get("/api/v1/__no_such_route__")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert resp.headers.get("X-Polsia-Error-Code") is None


@pytest.mark.asyncio
async def test_webhook_coded_400_direct_raise_carries_the_code_header(api_client, monkeypatch):
    """The third direct-raise site the finding names: `api/v1/finance.py`'s coded 400.

    The signature leg is other lanes' terrain (R28/R32); here the verified-event step is
    doubled so the test exercises the CODED raise that fires when the event carries no id.
    """
    from app.api.v1 import finance as finance_api
    from app.config import settings
    from app.core.errors import E_VALIDATION

    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fix5_test")
    monkeypatch.setattr(finance_api, "construct_stripe_event", lambda *a, **k: {})

    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook",
        headers={"stripe-signature": "t=1,v1=fake"},
        content=b"{}",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == E_VALIDATION
    assert resp.headers.get("X-Polsia-Error-Code") == E_VALIDATION


@pytest.mark.asyncio
async def test_e_auth_from_the_router_dependency_still_carries_its_challenge(api_client):
    """Control next to L1-04: the `PolsiaError` path keeps code + challenge headers."""
    resp = await api_client.get("/api/v1/config")  # no key
    assert resp.status_code == 401
    assert resp.headers.get("X-Polsia-Error-Code") == E_AUTH
    assert resp.headers.get("WWW-Authenticate") == "X-API-Key"
