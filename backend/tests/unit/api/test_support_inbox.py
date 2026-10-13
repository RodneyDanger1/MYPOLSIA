"""R17 — /api/v1/support/inbox: owner auth, submit -> list round-trip, named refusals.

The route pair is the ONLY way a customer message enters the system (the sweep then attaches
it); every refusal asserted here is the service's own named `E_VALIDATION`, translated by the
standard `refusal_guard` bridge (`app/api/deps.py:95-125`) — not a bespoke error shape.
"""
from __future__ import annotations

import pytest

ITEM = {
    "provider_message_id": "msg-api-1",
    "from_address": "customer@example.com",
    "subject": "order #1042",
    "body": "Where is my order #1042? It was due Tuesday.",
}


@pytest.mark.asyncio
async def test_inbox_verbs_require_the_owner_key(api_client, auth_headers):
    """No key -> the standard E_AUTH refusal on BOTH verbs (these are customer emails)."""
    for method, payload in (("get", None), ("post", ITEM)):
        request = getattr(api_client, method)
        kwargs = {"json": payload} if payload is not None else {}
        resp = await request("/api/v1/support/inbox", **kwargs)
        assert resp.status_code == 401, f"{method} without a key must be 401"
        body = resp.json()
        assert body["ok"] is False and body["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_submit_then_list_round_trips(api_client, auth_headers):
    created = await api_client.post("/api/v1/support/inbox", headers=auth_headers, json=ITEM)
    assert created.status_code == 201
    data = created.json()
    assert data["provider_message_id"] == "msg-api-1"
    assert data["status"] == "pending"
    assert data["task_id"] is None and data["received_at"] is None
    assert data["created_at"] is not None

    listed = await api_client.get("/api/v1/support/inbox", headers=auth_headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["provider_message_id"] for row in rows] == ["msg-api-1"]
    assert rows[0]["body"] == ITEM["body"], "the customer's message round-trips byte-exact"

    pending = await api_client.get(
        "/api/v1/support/inbox?status=pending", headers=auth_headers
    )
    assert len(pending.json()) == 1
    dispatched = await api_client.get(
        "/api/v1/support/inbox?status=dispatched", headers=auth_headers
    )
    assert dispatched.json() == []


@pytest.mark.asyncio
async def test_resubmitting_a_known_provider_id_keeps_exactly_one_row(api_client, auth_headers):
    first = await api_client.post("/api/v1/support/inbox", headers=auth_headers, json=ITEM)
    assert first.status_code == 201
    again = await api_client.post(
        "/api/v1/support/inbox",
        headers=auth_headers,
        json={**ITEM, "body": "REPLACEMENT TEXT"},
    )
    assert again.status_code == 201

    rows = (await api_client.get("/api/v1/support/inbox", headers=auth_headers)).json()
    assert len(rows) == 1
    assert rows[0]["body"] == ITEM["body"], "the stored message is never overwritten"
    assert again.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_a_blank_body_is_the_services_named_refusal(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/support/inbox", headers=auth_headers, json={**ITEM, "body": "   "}
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert "body" in error["message"]
    assert error["how_to_fix"], "a named refusal must say how to fix it"


@pytest.mark.asyncio
async def test_an_unknown_status_filter_refuses(api_client, auth_headers):
    resp = await api_client.get("/api/v1/support/inbox?status=nope", headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "E_VALIDATION"


@pytest.mark.asyncio
async def test_the_list_paginates_like_its_siblings(api_client, auth_headers):
    for n in range(3):
        resp = await api_client.post(
            "/api/v1/support/inbox",
            headers=auth_headers,
            json={**ITEM, "provider_message_id": f"msg-api-{n}"},
        )
        assert resp.status_code == 201

    first_page = (await api_client.get("/api/v1/support/inbox?limit=2", headers=auth_headers)).json()
    assert len(first_page) == 2
    second_page = (
        await api_client.get("/api/v1/support/inbox?limit=2&offset=2", headers=auth_headers)
    ).json()
    assert len(second_page) == 1
    seen = {row["provider_message_id"] for row in first_page + second_page}
    assert seen == {"msg-api-0", "msg-api-1", "msg-api-2"}, "pages must not overlap or skip"
