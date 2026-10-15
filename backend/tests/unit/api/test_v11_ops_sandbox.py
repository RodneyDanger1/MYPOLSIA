"""(V11 unit 2 / parent-surgical) GET /api/v1/ops/sandbox — owner auth + the REAL settings flag.

The route exists so the UI can SAY whether external actions are sandboxed (capability audit
B1/B2: `SANDBOX_MODE=true` is the shipped default and was invisible everywhere). The test reads
the live `settings` object (imported, not replaced) and flips it with monkeypatch, so the route
is proven against the same value every refusal path checks — never a stub agreeing with itself.
"""
from __future__ import annotations

import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_requires_the_owner_key(api_client):
    resp = await api_client.get("/api/v1/ops/sandbox")
    assert resp.status_code == 401
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_reports_the_live_sandbox_flag(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    resp = await api_client.get("/api/v1/ops/sandbox", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_mode": True}

    monkeypatch.setattr(settings, "sandbox_mode", False)
    resp = await api_client.get("/api/v1/ops/sandbox", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_mode": False}
