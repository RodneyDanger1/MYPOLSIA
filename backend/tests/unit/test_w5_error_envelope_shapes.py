"""SV-W4-02 (wave 5): 404/405/422/500 must each have ONE documented wire shape.

At the wave-4 HEAD the product had two shapes for a refusal:
`{"ok": false, "error": {code,message,how_to_fix,context}}` for every named `PolsiaError`, and
`{"detail": "Not Found"}` / `{"detail": "Method Not Allowed"}` for the framework's own route miss
and wrong verb (live-measured by `systems-validator`, `raw/w5/live-openapi-probe.txt`).  The
dashboard's `isRefusal` parses the envelope only, so a 405 reached the owner unnamed.
`app/main.py::framework_refusal` names those two (404 `E_NOT_FOUND`, 405
`E_METHOD_NOT_ALLOWED`); the 422 validation array and the webhook's plain-detail 400 stay the
FRAMEWORK shape on purpose (documented exception set; pinned by `test_refusal_unify.py`).

This file pins all four shapes at once, on the real app.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.config import settings
from app.core.security import verify_api_key
from app.main import ErrorBoundary, create_app

ENVELOPE_KEYS = {"ok", "error"}
ERROR_KEYS = {"code", "message", "how_to_fix", "context"}


def _envelope(body: dict[str, Any]) -> dict[str, Any]:
    assert set(body) == ENVELOPE_KEYS, f"not the product envelope: {body}"
    assert body["ok"] is False
    assert set(body["error"]) == ERROR_KEYS, body
    return body["error"]


def test_404_route_miss_is_the_product_envelope() -> None:
    with TestClient(create_app()) as client:
        resp = client.get(
            "/api/v1/definitely-not-a-route", headers={"X-API-Key": settings.api_key}
        )
    assert resp.status_code == 404
    error = _envelope(resp.json())
    assert error["code"] == "E_NOT_FOUND"
    assert resp.headers["x-meridian-error-code"] == "E_NOT_FOUND"
    assert error["how_to_fix"], "a named refusal must say how to fix it"
    assert error["context"]["method"] == "GET"
    assert "definitely-not-a-route" in error["context"]["path"]


def test_405_wrong_verb_is_a_named_refusal_and_keeps_allow() -> None:
    """`GET /api/v1/sites/{slug}` does not exist; only DELETE/POST verbs do."""
    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/sites/cinder-lye", headers={"X-API-Key": settings.api_key})
    assert resp.status_code == 405
    error = _envelope(resp.json())
    assert error["code"] == "E_METHOD_NOT_ALLOWED"
    assert resp.headers["x-meridian-error-code"] == "E_METHOD_NOT_ALLOWED"
    assert "DELETE" in resp.headers.get("allow", ""), (
        "the framework's Allow header must survive the envelope rewrite"
    )


def test_422_validation_keeps_the_framework_detail_array() -> None:
    """The one framework body that is NOT wrapped: pydantic's array is a documented contract."""
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/v1/tasks", headers={"X-API-Key": settings.api_key}, json={"nothing": "here"}
        )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], list) and body["detail"]
    assert "ok" not in body


def test_500_uncaught_exception_is_the_internal_envelope(monkeypatch) -> None:
    """A dependency raising a non-PolsiaError exception becomes E_INTERNAL, not a bare 500.

    The overrides are installed with monkeypatch so they are RESTORED: a bare dict assignment here
    leaked into the shared app and reddened a later hygiene guard in a full run (R-W5-LEAD-5).
    """
    application = create_app()

    def _boom() -> None:
        raise RuntimeError("planted for the 500 envelope pin")

    monkeypatch.setitem(application.dependency_overrides, verify_api_key, lambda: None)
    monkeypatch.setitem(application.dependency_overrides, get_db, _boom)
    with TestClient(application, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/tasks")
    assert resp.status_code == 500
    error = _envelope(resp.json())
    assert error["code"] == "E_INTERNAL"
    assert error["context"]["request_id"], "the 500 envelope must carry the correlation id"
    assert resp.headers["x-request-id"] == error["context"]["request_id"]


async def test_error_boundary_alone_answers_the_envelope() -> None:
    """The boundary is the seam, so it is exercised without a route at all."""

    async def _boom(scope, receive, send):  # noqa: ANN001, ARG001 - ASGI signature
        raise RuntimeError("planted: no route involved")

    transport = httpx.ASGITransport(app=ErrorBoundary(_boom), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/anything")
    assert resp.status_code == 500
    error = _envelope(resp.json())
    assert error["code"] == "E_INTERNAL"
    assert resp.headers["x-meridian-error-code"] == "E_INTERNAL"


def test_the_documented_uncoded_400_still_passes_through(monkeypatch) -> None:
    """The Stripe webhook's bad-signature 400 is plain detail BY CONTRACT — not envelope-wrapped."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_w5_envelope_pin")
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/v1/finance/stripe/webhook",
            content=b'{"id":"evt_x"}',
            headers={"stripe-signature": "t=1,v1=badsig"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == {"detail"}, "a documented plain-detail 400 must not change shape"
    assert "signature" in body["detail"].lower()


@pytest.mark.parametrize("path", ["/api/v1/nope", "/api/v1/tasks/not-a-route/extra"])
def test_route_misses_are_enveloped_for_every_unknown_path(path: str) -> None:
    with TestClient(create_app()) as client:
        resp = client.get(path, headers={"X-API-Key": settings.api_key})
    assert resp.status_code == 404
    assert _envelope(resp.json())["code"] == "E_NOT_FOUND"
