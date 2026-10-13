"""RD4 item 4 (D5) — one wire shape for a refusal: coded `HTTPException`s reach the envelope.

Audit §2.3: the envelope `{"ok": false, "error": {code, message, how_to_fix, context}}`
(`errors.to_http_detail`) was emitted by the app's `PolsiaError` handler, while a CODED
`HTTPException` detail — what `refusal_guard()` (deps.py) and the direct raises in tasks.py put on
the wire — was serialized by FastAPI's default handler as `{"detail": {...}}`.  The frontend's
`isRefusal` parses the envelope only, so those fully-named refusals surfaced as raw JSON.

Pinned here, through the REAL app:
  * a coded detail (task 404, stripe event without id) → the envelope;
  * a codeless detail (plain string, validation array, route miss) → UNCHANGED;
  * the `PolsiaError` path (auth) → unchanged, envelope + challenge, no double-wrap.

RED leg (measured this session; lane receipt build/RD4-unwired/RECEIPT.md): neutralising the
`StarletteHTTPException` handler registration makes `test_a_coded_http_exception_is_wrapped...` fail
(the body is `{"detail": {...}}` again).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from app.core.errors import E_NOT_FOUND, E_VALIDATION, ERROR_HINTS
from app.main import coded_detail_envelope

TRY_ENVELOPE_KEYS = {"ok", "error"}


# ── the helper, in isolation ───────────────────────────────────────────────────────────────────
def test_a_coded_detail_becomes_the_envelope():
    envelope = coded_detail_envelope({
        "code": E_NOT_FOUND,
        "message": "task 4 does not exist",
        "how_to_fix": "check the id",
        "context": {"task_id": 4},
    })
    assert envelope == {
        "ok": False,
        "error": {
            "code": E_NOT_FOUND,
            "message": "task 4 does not exist",
            "how_to_fix": "check the id",
            "context": {"task_id": 4},
        },
    }


def test_an_empty_hint_falls_back_to_the_code_s_hint():
    envelope = coded_detail_envelope({"code": E_VALIDATION, "message": "bad input"})
    assert envelope["error"]["how_to_fix"] == ERROR_HINTS[E_VALIDATION]
    assert envelope["error"]["context"] == {}


@pytest.mark.parametrize("detail", [
    "Invalid Stripe signature",          # a plain string detail (finance.py:118)
    ["body.field: missing"],             # a validation array
    {"detail": "not coded"},             # a dict without a code
    {"code": ""},                        # an empty code is not a code
    {"code": 42},                        # a non-string code is not a code
    None,
])
def test_every_uncoded_detail_is_left_alone(detail):
    assert coded_detail_envelope(detail) is None


# ── through the real app: coded path ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_coded_http_exception_is_wrapped_into_the_envelope(api_client, auth_headers):
    resp = await api_client.get("/api/v1/tasks/999999", headers=auth_headers)

    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == TRY_ENVELOPE_KEYS, f"still the legacy shape: {body}"
    assert body["ok"] is False
    assert body["error"]["code"] == E_NOT_FOUND
    assert body["error"]["message"]
    assert body["error"]["how_to_fix"]
    assert body["error"]["context"]["task_id"] == 999999


def _stripe_signature(secret: str, body: bytes) -> str:
    timestamp = int(time.time())
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.mark.asyncio
async def test_a_coded_raise_inside_the_webhook_route_is_wrapped(api_client, monkeypatch):
    """The second coded-raise family (finance.py's `E_VALIDATION`), reached with a VALID signature."""
    from app.config import settings

    secret = "whsec_rd4_test"
    monkeypatch.setattr(settings, "stripe_webhook_secret", secret)
    body = json.dumps({
        "type": "charge.succeeded",
        "data": {"object": {"amount": 100, "currency": "usd"}},
    }).encode("utf-8")

    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook",
        content=body,
        headers={"stripe-signature": _stripe_signature(secret, body)},
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == E_VALIDATION


# ── through the real app: the codeless paths must be byte-identical to before ──────────────────
@pytest.mark.asyncio
async def test_a_plain_string_detail_passes_through_unchanged(api_client, monkeypatch):
    """An UNCODED string detail keeps FastAPI's default `{"detail": "<string>"}` body.

    The exact wording belongs to the stripe verification path (it is not ours to pin); what this
    test pins is the SHAPE: no envelope, nothing added, the string preserved verbatim in `detail`.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_rd4_test")
    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook",
        content=b'{"id":"evt_x"}',
        headers={"stripe-signature": "t=1,v1=badsig"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == {"detail"}, f"an uncoded detail must not be wrapped: {body}"
    assert isinstance(body["detail"], str) and "signature" in body["detail"]


@pytest.mark.asyncio
async def test_a_routed_miss_keeps_the_default_shape(api_client):
    """Starlette's own 404 (no code anywhere) must stay exactly as FastAPI rendered it."""
    resp = await api_client.get("/api/v1/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_a_validation_error_keeps_its_detail_array(api_client, auth_headers):
    resp = await api_client.post("/api/v1/tasks", headers=auth_headers, json={"nothing": "here"})
    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body["detail"], list) and body["detail"], "pydantic's array must survive"
    assert "ok" not in body


@pytest.mark.asyncio
async def test_the_polsiaerror_path_is_not_double_wrapped(api_client):
    """The named-refusal path keeps its own handler: envelope + challenge, no nesting."""
    resp = await api_client.get("/api/v1/config", headers={"X-API-Key": "definitely-wrong"})

    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "X-API-Key"
    body = resp.json()
    assert set(body) == TRY_ENVELOPE_KEYS
    assert body["error"]["code"] == "E_AUTH"
    assert "error" not in body["error"], "the envelope must not be wrapped twice"
