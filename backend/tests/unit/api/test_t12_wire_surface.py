"""T12 / D20 — the dead wire surface: two models WIRED, four REMOVED, parity consistent.

Wired:
  * `StripeWebhookResponse` is the `response_model` of `POST /finance/stripe/webhook`.  The
    schema was left TRUE to the handler (`{"received": true}`) — per FastAPI's own semantics the
    model now VALIDATES that body and publishes it in the OpenAPI document, so the documented and
    the actual response can no longer drift apart.
  * `TaskStatusUpdate` is the typed body of the NEW `PATCH /api/v1/tasks/{task_id}/status`,
    which calls the SAME `task_service.update_task_status` the worker uses — the service's
    `VALID_TASK_STATUSES` stays the single authority, so the route adds no vocabulary of its own.
    `tests/parity/test_api_surface_parity.py::EXPECTED_ROUTES` gained
    `("/api/v1/tasks/{task_id}/status", ("PATCH",))` in the same change (old surface: no such
    route).

Removed (fresh zero-consumer grep, receipt §evidence — zero refs incl. `__all__`, frontend
types, e2e and docs): `ExpenseCreate`, `MessageResponse`, `TasksTodaySummary`,
`SocialEngagementResponse`.

RED legs (measured, receipt §RED): (a) drop `response_model=` from the webhook decorator ->
`test_the_webhook_declares_its_response_model` fails; (b) remove the PATCH route -> the route
tests and the parity suite fail.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

SECRET = "whsec_t12_wire"
NOW = int(time.time())


def _sign(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> str:
    stamp = NOW if ts is None else ts
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    return SECRET


def _routes() -> list:
    from app.main import create_app

    return list(create_app().routes)


def _find(path: str, method: str):
    for route in _routes():
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or ()):
            return route
    raise AssertionError(f"route {method} {path} is not published")


# ── (a) the webhook declares its response model ────────────────────────────────────────────────


def test_the_webhook_declares_its_response_model():
    from app.schemas.finance import StripeWebhookResponse

    route = _find("/api/v1/finance/stripe/webhook", "POST")
    assert getattr(route, "response_model", None) is StripeWebhookResponse, (
        "the webhook must declare response_model=StripeWebhookResponse (D20)"
    )


@pytest.mark.asyncio
async def test_the_webhook_body_is_the_declared_shape(api_client, webhook_secret, auth_headers):
    body = json.dumps({
        "id": "evt_t12_wire",
        "type": "charge.succeeded",
        "data": {"object": {"amount": 100, "currency": "usd"}},
    }).encode("utf-8")
    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook",
        content=body,
        headers={"stripe-signature": _sign(body)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}
    events = await api_client.get("/api/v1/finance/events", headers=auth_headers)
    assert [row["stripe_event_id"] for row in events.json()] == ["evt_t12_wire"]


# ── (b) the typed status route ─────────────────────────────────────────────────────────────────


def test_the_status_route_exists_with_the_pinned_method():
    route = _find("/api/v1/tasks/{task_id}/status", "PATCH")
    from app.schemas.task import TaskStatusUpdate

    assert route.body_field is not None, "the route must take a typed body"
    # the body model the route validates against is exactly TaskStatusUpdate
    body_model = route.body_field.field_info.annotation
    assert body_model is TaskStatusUpdate


@pytest.mark.asyncio
async def test_the_status_route_writes_a_valid_status(api_client, auth_headers, async_db_session):
    from datetime import datetime

    from app.models.task import Task

    task = Task(title="status probe", agent_type="finance", status="pending",
                created_at=datetime(2026, 10, 13, 12, 0, 0))
    async_db_session.add(task)
    await async_db_session.flush()

    resp = await api_client.patch(
        f"/api/v1/tasks/{task.id}/status",
        headers=auth_headers,
        json={"status": "completed", "result_summary": "done by hand"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == task.id and body["status"] == "completed"
    assert body["result_summary"] == "done by hand"


@pytest.mark.asyncio
async def test_an_unknown_status_is_a_named_refusal_and_writes_nothing(
    api_client, auth_headers, async_db_session
):
    from datetime import datetime

    from app.models.task import Task

    task = Task(title="status refusal", agent_type="finance", status="pending",
                created_at=datetime(2026, 10, 13, 12, 0, 0))
    async_db_session.add(task)
    await async_db_session.flush()

    resp = await api_client.patch(
        f"/api/v1/tasks/{task.id}/status", headers=auth_headers, json={"status": "not_a_status"}
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert "not_a_status" in error["message"]
    assert "pending" in str(error["context"].get("valid")), "the refusal names the accepted set"
    assert task.status == "pending", "nothing may be written under a refused value"


@pytest.mark.asyncio
async def test_a_missing_task_is_the_not_found_envelope(api_client, auth_headers):
    resp = await api_client.patch(
        "/api/v1/tasks/987654/status", headers=auth_headers, json={"status": "cancelled"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_the_status_route_requires_auth(api_client):
    resp = await api_client.patch("/api/v1/tasks/1/status", json={"status": "completed"})
    assert resp.status_code == 401


# ── (c) the four removals ──────────────────────────────────────────────────────────────────────


def test_the_removed_models_are_gone():
    import app.schemas.common as common
    import app.schemas.finance as finance
    import app.schemas.social as social
    import app.schemas.task as task

    assert not hasattr(finance, "ExpenseCreate")
    assert not hasattr(common, "MessageResponse")
    assert not hasattr(task, "TasksTodaySummary")
    assert not hasattr(social, "SocialEngagementResponse")


# ── (d) the schemas polish (fix-3 §7) ──────────────────────────────────────────────────────────


def test_the_polish_landed():
    from app.schemas.agent_outputs import ReplyItem, SocialMediaOutput

    description = ReplyItem.model_fields["reply_id"].description or ""
    assert "sandbox" in description.lower() and "not sent" in description.lower(), description
    doc = SocialMediaOutput.__doc__ or ""
    for value in ("draft", "post", "reply", "noop"):
        assert value in doc, f"SocialMediaOutput.action documents {value!r}"
