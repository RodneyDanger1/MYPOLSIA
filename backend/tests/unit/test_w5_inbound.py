"""W5 — inbound support-inbox ingestion (the provider-agnostic email webhook).

WHAT THIS FILE PINS
    `POST /api/v1/support/inbox/inbound` is the route a real inbound-email provider (Postmark /
    SendGrid-style JSON) calls to create a support-inbox item.  It is the missing producer half of
    the chain the R17 owner route starts: the 3h `email-inbox-sweep` attaches PENDING items to
    `customer_support` tasks (`support_inbox_service.attach_pending`), and until this route existed
    the inbox could only be filled by the owner's own `POST /api/v1/support/inbox`
    (`recon-2026-10-28/our-abilities/OUR-ABILITIES.md` §3 item 11).

    Seven contracts are exercised END TO END against the real app, the real ORM and the real
    service (no doubles — only Redis, which is the suite's standard mock):
      1. auth — a dedicated shared secret (`X-Polsia-Inbound-Secret`), never the owner API key;
         unset `SUPPORT_INBOUND_SECRET` is the NAMED `E_CONFIG` refusal, never an open endpoint;
      2. validation — a malformed payload is `E_VALIDATION` with `context.field`, never the
         framework's uncoded 422 and never an echoed customer body;
      3. happy path — the item lands `pending` with the SAME fields the owner-POST path writes;
      4. idempotency — a re-delivery returns the stored item (`created: false`) and writes no
         second item, task or overwrite;
      5. audit — one stable action name per delivery, and the secret/body never reach it;
      6. attach — the shipped sweep helper picks the ingested item up and creates ONE deduped
         `customer_support` task (and a later re-delivery cannot create a second one);
      7. the owner routes keep EXACTLY the auth they had before the route was added (moving a
         router-level dependency down to per-route must not open the owner surface).

RED LEG
    Written and run BEFORE the route existed: every endpoint leg answered 404 (route absent) —
    see `coders-2026-10-28/W5/raw/red-prefix.txt`.  The auth legs were additionally re-planted
    against the finished code (a constant `True` secret check) — `raw/red-planted-auth.txt`.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models.report import ActivityLog
from app.models.support import SupportInboxItem
from app.models.task import Task

INBOUND_PATH = "/api/v1/support/inbox/inbound"
SECRET_HEADER = "X-Polsia-Inbound-Secret"
SECRET = "w5-inbound-shared-secret"
BODY_MARKER = "Please refund order #1042; it arrived broken."

PAYLOAD = {
    "provider_message_id": "w5-msg-1",
    "from": "customer@example.com",
    "subject": "order #1042 arrived broken",
    "body": BODY_MARKER,
}


@pytest.fixture
def configured_secret(monkeypatch):
    """The operator has set SUPPORT_INBOUND_SECRET (the shipped `.env` value is blank)."""
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)
    return SECRET


@pytest.fixture
def secret_headers(configured_secret):
    return {SECRET_HEADER: configured_secret}


async def _rows(db) -> list[SupportInboxItem]:
    return list(
        (await db.execute(select(SupportInboxItem).order_by(SupportInboxItem.id.asc())))
        .scalars()
        .all()
    )


async def _tasks(db) -> list[Task]:
    return list((await db.execute(select(Task).order_by(Task.id.asc()))).scalars().all())


async def _count(db, model) -> int:
    return int((await db.scalar(select(func.count()).select_from(model))) or 0)


async def _audit(db, action: str) -> list[ActivityLog]:
    return list(
        (
            await db.execute(
                select(ActivityLog)
                .where(ActivityLog.action == action)
                .order_by(ActivityLog.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _error(response) -> dict:
    body = response.json()
    assert body.get("ok") is False, f"not the project refusal envelope: {body}"
    return body["error"]


# ── 1. auth: the dedicated shared secret ──────────────────────────────────────────────────────
async def test_the_inbound_route_exists_and_requires_the_shared_secret(api_client, secret_headers):
    """No credential -> the NAMED 401 (a 404 here means the route was never published)."""
    response = await api_client.post(INBOUND_PATH, json=PAYLOAD)
    assert response.status_code == 401, (
        f"{INBOUND_PATH} answered {response.status_code}: {response.text[:400]}"
    )
    error = _error(response)
    assert error["code"] == "E_AUTH"
    assert error["how_to_fix"], "a named refusal must say how to fix it"
    assert response.headers.get("X-Polsia-Error-Code") == "E_AUTH"
    assert SECRET not in response.text and BODY_MARKER not in response.text


async def test_a_missing_secret_header_is_a_named_401_and_stores_nothing(
    api_client, async_db_session, configured_secret
):
    response = await api_client.post(INBOUND_PATH, json=PAYLOAD)
    assert response.status_code == 401, response.text[:400]
    assert _error(response)["code"] == "E_AUTH"
    assert await _count(async_db_session, SupportInboxItem) == 0
    assert SECRET not in response.text and BODY_MARKER not in response.text


async def test_a_wrong_secret_is_a_named_401_and_stores_nothing(
    api_client, async_db_session, configured_secret
):
    wrong = f"not-{configured_secret}"
    response = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers={SECRET_HEADER: wrong})
    assert response.status_code == 401, response.text[:400]
    error = _error(response)
    assert error["code"] == "E_AUTH"
    assert response.headers.get("X-Polsia-Error-Code") == "E_AUTH"
    assert await _count(async_db_session, SupportInboxItem) == 0
    # neither the configured value nor the presented one is echoed, nor the customer body
    assert configured_secret not in response.text
    assert wrong not in response.text
    assert BODY_MARKER not in response.text


async def test_a_non_ascii_secret_header_is_a_401_not_a_500(
    api_client, configured_secret
):
    """same hostile input the API-key compare had to survive (`security.py:29-33`)."""
    response = await api_client.post(
        INBOUND_PATH,
        json=PAYLOAD,
        headers=[(b"x-polsia-inbound-secret", "w5-s\u00e9cret".encode("latin-1"))],
    )
    assert response.status_code == 401, response.text[:400]
    assert _error(response)["code"] == "E_AUTH"


async def test_an_unset_secret_is_a_named_config_refusal_that_never_opens(
    api_client, async_db_session, monkeypatch
):
    """The forgotten-configuration case: E_CONFIG, NOT an open inbox."""
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", "", raising=False)
    response = await api_client.post(
        INBOUND_PATH, json=PAYLOAD, headers={SECRET_HEADER: "anything"}
    )
    assert response.status_code == 503, response.text[:400]
    error = _error(response)
    assert error["code"] == "E_CONFIG"
    assert "SUPPORT_INBOUND_SECRET" in json.dumps(error), (
        "the refusal must name the setting an operator has to set"
    )
    assert await _count(async_db_session, SupportInboxItem) == 0
    assert BODY_MARKER not in response.text


async def test_the_owner_api_key_is_not_the_inbound_secret(
    api_client, auth_headers, configured_secret
):
    """The dashboard key must not stand in for the provider secret (two different trust models)."""
    response = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=auth_headers)
    assert response.status_code == 401, response.text[:400]
    assert _error(response)["code"] == "E_AUTH"


async def test_the_owner_routes_keep_the_owner_key(api_client, auth_headers):
    """Moving the router dependency down to the two owner routes must not open the owner surface."""
    read = await api_client.get("/api/v1/support/inbox")
    assert read.status_code == 401
    assert _error(read)["code"] == "E_AUTH"
    write = await api_client.post(
        "/api/v1/support/inbox",
        json={
            "provider_message_id": "w5-owner-1",
            "from_address": "customer@example.com",
            "subject": "s",
            "body": "b",
        },
    )
    assert write.status_code == 401
    assert _error(write)["code"] == "E_AUTH"

    created = await api_client.post(
        "/api/v1/support/inbox",
        headers=auth_headers,
        json={
            "provider_message_id": "w5-owner-1",
            "from_address": "customer@example.com",
            "subject": "s",
            "body": "b",
        },
    )
    assert created.status_code == 201, created.text[:400]


# ── 2. happy path ─────────────────────────────────────────────────────────────────────────────
async def test_a_signed_payload_ingests_a_pending_item_and_audits_the_ingest(
    api_client, async_db_session, auth_headers, secret_headers, configured_secret
):
    from app.services.support_inbox_service import INBOUND_AUDIT_ACTION

    response = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=secret_headers)
    assert response.status_code == 201, response.text[:600]
    data = response.json()
    assert data["created"] is True
    assert data["item_id"] == data["item"]["id"] > 0
    assert data["provider"] is None

    item = data["item"]
    assert item["provider_message_id"] == PAYLOAD["provider_message_id"]
    assert item["from_address"] == PAYLOAD["from"]
    assert item["subject"] == PAYLOAD["subject"]
    assert item["body"] == PAYLOAD["body"], "the customer's message is stored byte-exact"
    assert item["status"] == "pending", "the attach sweep must find it"
    assert item["task_id"] is None and item["received_at"] is None
    assert item["created_at"]

    rows = await _rows(async_db_session)
    assert len(rows) == 1
    assert rows[0].status == "pending" and rows[0].task_id is None
    assert rows[0].provider_message_id == PAYLOAD["provider_message_id"]

    # the owner read sees the SAME row the provider wrote (one inbox, two producers)
    listed = await api_client.get("/api/v1/support/inbox", headers=auth_headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [item["id"]]

    audit = await _audit(async_db_session, INBOUND_AUDIT_ACTION)
    assert len(audit) == 1, "one audit row per delivery"
    entry = audit[0]
    assert entry.agent_type == "system"
    assert entry.detail["item_id"] == item["id"]
    assert entry.detail["created"] is True
    assert entry.detail["provider_message_id"] == PAYLOAD["provider_message_id"]
    text = f"{entry.summary} {json.dumps(entry.detail)}"
    assert configured_secret not in text, "the shared secret never reaches the ledger"
    assert BODY_MARKER not in text, "the customer body never reaches the ledger"


async def test_an_offset_received_at_is_stored_and_served_as_utc(api_client, secret_headers):
    response = await api_client.post(
        INBOUND_PATH,
        json={
            **PAYLOAD,
            "provider_message_id": "w5-msg-tz",
            "received_at": "2026-10-28T09:30:00-05:00",
        },
        headers=secret_headers,
    )
    assert response.status_code == 201, response.text[:400]
    assert response.json()["item"]["received_at"] == "2026-10-28T14:30:00Z"


async def test_provider_field_aliases_and_extra_keys_are_tolerated(
    api_client, secret_headers
):
    """A Postmark-shaped body works as-is; unknown provider keys are ignored, not refused."""
    response = await api_client.post(
        INBOUND_PATH,
        json={
            "MessageID": "w5-postmark-1",
            "From": "Customer <customer@example.com>",
            "Subject": "order #7",
            "TextBody": BODY_MARKER,
            "Attachments": [],
            "SpamScore": 0.1,
        },
        headers=secret_headers,
    )
    assert response.status_code == 201, response.text[:600]
    item = response.json()["item"]
    assert item["provider_message_id"] == "w5-postmark-1"
    assert item["from_address"] == "Customer <customer@example.com>"
    assert item["subject"] == "order #7"
    assert item["body"] == BODY_MARKER


async def test_the_provider_is_recorded_on_the_audit_row_not_invented_in_the_row(
    api_client, async_db_session, secret_headers
):
    from app.services.support_inbox_service import INBOUND_AUDIT_ACTION

    response = await api_client.post(
        INBOUND_PATH, json={**PAYLOAD, "provider": "postmark"}, headers=secret_headers
    )
    assert response.status_code == 201, response.text[:400]
    assert response.json()["provider"] == "postmark"
    audit = await _audit(async_db_session, INBOUND_AUDIT_ACTION)
    assert audit[0].detail["provider"] == "postmark"


# ── 3. idempotency ───────────────────────────────────────────────────────────────────────────
async def test_a_redelivery_returns_the_existing_item_and_creates_nothing(
    api_client, async_db_session, secret_headers
):
    first = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=secret_headers)
    assert first.status_code == 201
    first_body = first.json()

    # an at-least-once provider re-posts the SAME id, possibly with changed text (an edit)
    again = await api_client.post(
        INBOUND_PATH,
        json={**PAYLOAD, "subject": "edited", "body": "REPLACEMENT TEXT"},
        headers=secret_headers,
    )
    assert again.status_code == 200, again.text[:400]
    again_body = again.json()
    assert again_body["created"] is False
    assert again_body["item_id"] == first_body["item_id"]
    assert again_body["item"]["body"] == BODY_MARKER, "stored evidence is never overwritten"
    assert again_body["item"]["subject"] == PAYLOAD["subject"]

    assert await _count(async_db_session, SupportInboxItem) == 1
    assert await _count(async_db_session, Task) == 0, "a re-delivery must not create a task"


async def test_a_redelivery_after_the_attach_sweep_creates_no_second_task(
    api_client, async_db_session, secret_headers
):
    from app.services.support_inbox_service import attach_pending

    assert (await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=secret_headers)).status_code == 201
    attached = await attach_pending(async_db_session)
    assert len(attached) == 1 and attached[0]["task_created"] is True

    again = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=secret_headers)
    assert again.status_code == 200 and again.json()["created"] is False

    assert await _count(async_db_session, SupportInboxItem) == 1
    assert await _count(async_db_session, Task) == 1, "one item must stay one task"


# ── 4. validation: named E_VALIDATION with context.field ─────────────────────────────────────
MALFORMED = [
    pytest.param(
        {k: v for k, v in PAYLOAD.items() if k != "provider_message_id"},
        "provider_message_id",
        id="missing-provider-message-id",
    ),
    pytest.param({**PAYLOAD, "from": "   "}, "from_address", id="blank-from"),
    pytest.param({**PAYLOAD, "subject": ""}, "subject", id="blank-subject"),
    pytest.param({**PAYLOAD, "body": "\t \n"}, "body", id="blank-body"),
    pytest.param(
        {**PAYLOAD, "received_at": "2026-10-28T09:30:00"},
        "received_at",
        id="timezone-less-received-at",
    ),
    pytest.param({**PAYLOAD, "received_at": "not-a-date"}, "received_at", id="bad-received-at"),
    pytest.param({**PAYLOAD, "body": "x" * 65_537}, "body", id="oversize-body"),
    pytest.param({**PAYLOAD, "provider": "p" * 65}, "provider", id="oversize-provider"),
    pytest.param({**PAYLOAD, "subject": 123}, "subject", id="wrong-type-subject"),
]


@pytest.mark.parametrize(("payload", "field"), MALFORMED)
async def test_a_malformed_payload_is_a_named_422_with_the_field(
    api_client, async_db_session, secret_headers, payload, field
):
    response = await api_client.post(INBOUND_PATH, json=payload, headers=secret_headers)
    assert response.status_code == 422, response.text[:400]
    error = _error(response)
    assert error["code"] == "E_VALIDATION"
    assert error["how_to_fix"]
    assert error["context"]["field"] == field, error
    assert response.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"
    assert await _count(async_db_session, SupportInboxItem) == 0
    assert len(response.text) < 4_000, "a refusal must never echo the payload back"
    assert BODY_MARKER not in response.text


@pytest.mark.parametrize("body", [[1, 2, 3], "just a string", 42], ids=["list", "string", "number"])
async def test_a_non_object_body_is_a_named_validation_refusal(
    api_client, async_db_session, secret_headers, body
):
    response = await api_client.post(INBOUND_PATH, json=body, headers=secret_headers)
    assert response.status_code == 422, response.text[:400]
    error = _error(response)
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["field"] == "payload"
    assert await _count(async_db_session, SupportInboxItem) == 0


async def test_an_absent_body_is_a_named_validation_refusal(
    api_client, async_db_session, secret_headers
):
    response = await api_client.post(INBOUND_PATH, headers=secret_headers)
    assert response.status_code == 422, response.text[:400]
    assert _error(response)["code"] == "E_VALIDATION"
    assert _error(response)["context"]["field"] == "payload"
    assert await _count(async_db_session, SupportInboxItem) == 0


# ── 5. the shipped attach sweep consumes what this route writes ──────────────────────────────
async def test_the_attach_sweep_picks_the_ingested_item_up(
    api_client, async_db_session, secret_headers
):
    from app.services.support_inbox_service import attach_pending, inbox_task_dedupe_key

    response = await api_client.post(INBOUND_PATH, json=PAYLOAD, headers=secret_headers)
    assert response.status_code == 201

    attached = await attach_pending(async_db_session)
    assert len(attached) == 1
    assert attached[0]["provider_message_id"] == PAYLOAD["provider_message_id"]
    assert attached[0]["task_created"] is True

    tasks = await _tasks(async_db_session)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.agent_type == "customer_support"
    assert task.dedupe_key == inbox_task_dedupe_key(PAYLOAD["provider_message_id"])
    assert task.status == "pending"
    assert task.metadata_json == {
        "inbox_item": {
            "id": PAYLOAD["provider_message_id"],
            "from": PAYLOAD["from"],
            "subject": PAYLOAD["subject"],
            "body": PAYLOAD["body"],
        }
    }, "the exact RD12 consumer contract the support agent reads"

    stored = (await _rows(async_db_session))[0]
    assert stored.status == "dispatched"
    assert stored.task_id == task.id
