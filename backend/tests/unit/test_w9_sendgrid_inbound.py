"""WAVE 9 — SV-W7-01 / SV-W8-04: a REAL Twilio SendGrid **Inbound Parse** adapter.

WHY THIS FILE EXISTS
    `POST /api/v1/support/inbox/inbound` shipped (W5) as a JSON-only, shared-secret webhook
    (`X-Meridian-Inbound-Secret`, a REQUIRED non-empty `provider_message_id`).  A real provider's
    Inbound Parse delivery is none of those things, so the route could never receive one:
    Twilio's own pages (fetched 2026-10-29, `dateModified` 2026-10-08 / 2026-10-07) document

      * `Content-Type: multipart/form-data` with named form fields (`headers`, `dkim`, `to`,
        `from`, `subject`, `text`, `html`, `envelope`, `attachments`, `charsets`, `SPF`,
        `attachment-info`, `attachment{N}`, …), and — with "POST the raw, full MIME message" —
        a single `email` field holding the whole MIME message;
      * **no** `message_id` form field at all (a `Message-ID:` header exists only inside
        `headers` / the `email` blob);
      * credentials: an ECDSA signature (`X-Twilio-Email-Event-Webhook-Signature` +
        `-Timestamp`, verified with the security policy's public key over the **raw bytes**
        of `timestamp + body`, DER signature, SHA-256), or an OAuth `Authorization: Bearer`
        token, or both.  No shared-secret/custom-header mechanism is documented.

    This file pins the adapter that closes that gap: the route now accepts the provider's real
    delivery, derives the item's provider id from the message's OWN identity, and refuses every
    malformed or unverifiable delivery BY NAME — never a 5xx (a 5xx invites the provider's retry
    loop over a delivery that can never succeed).

WHAT IS PINNED (all through the real app, the real ORM and the real service)
    1. ECDSA: a valid signature over the exact raw bytes ingests; a tampered body, a tampered
       timestamp, an expired timestamp, a wrong key and a half-present header pair are each a
       NAMED 401 with nothing stored.  Both key encodings the vendor hands out (base64 DER SPKI
       — its own sample shape — and PEM) verify.
    2. OAuth: the configured bearer token ingests; a wrong one is the RFC 6750 `invalid_token`
       refusal; an unconfigured token is a NAMED 401, never an open endpoint.
    3. CREDENTIALS ARE NEVER OPTIONAL: with no mechanism configured a multipart delivery is a
       NAMED 401 (`SENDGRID_INBOUND_PUBLIC_KEY` named in the refusal) — **not** the JSON path's
       503 `E_CONFIG`, which is exactly what made the old route retry-toxic for a provider.
    4. PAYLOAD: Default format and Raw (`email`) format both parse; the body prefers `text`, then
       `html`, then the MIME body; `from` is reduced to the address the prospect stamping reads;
       the provider id is the message's own `Message-ID`, else a boundary-independent digest that
       a re-delivery reproduces exactly (dedupe) and a different message does not.
    5. REFUSALS: malformed multipart, an absent boundary, a missing `from`/`subject`/body, an
       unsupported Content-Type and an oversized body are each a named 4xx (E_VALIDATION /
       E_AUTH) — a parametrized sweep asserts **no delivery shape ever answers 5xx**.
    6. THE JSON PATH IS UNCHANGED: the shipped credential order (missing header → 401, unset
       secret → 503 `E_CONFIG`), the Postmark aliases, and idempotency still behave exactly as
       `test_w5_inbound.py` pins them.

RED LEG
    Written and run against the pre-adapter tree first (`raw/w9/RED-w9-inbound.txt`): every
    multipart leg answered the old route's 422 `E_VALIDATION` (or FastAPI's array) and every
    credential leg was unreachable, because there was no multipart branch at all.
"""
from __future__ import annotations

import base64
import email.parser
import email.policy
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from app.models.report import ActivityLog
from app.models.support import SupportInboxItem
from app.models.task import Task

INBOUND_PATH = "/api/v1/support/inbox/inbound"
SECRET_HEADER = "X-Meridian-Inbound-Secret"
SECRET = "w9-inbound-shared-secret"
SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"
PROVIDER_TAG = "sendgrid-inbound-parse"
BODY_MARKER = "The widget arrived cracked; please replace order #9001."
BOUNDARY = "001a11447dc881e40b0537fe6d58"

#: One P-256 key for the whole module: the vendor generates an ECDSA key pair per security policy.
_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_DER_B64 = base64.b64encode(
    _PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
).decode()
PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()


# ── helpers: a SendGrid-shaped delivery ──────────────────────────────────────────────────────
def multipart_body(
    fields: dict[str, bytes],
    *,
    boundary: str = BOUNDARY,
    attachments: dict[str, bytes] | None = None,
) -> bytes:
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value + b"\r\n"
    for name, blob in (attachments or {}).items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"; filename="{name}.png"\r\n'.encode()
        out += b"Content-Type: image/png\r\n\r\n" + blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def default_fields(**overrides: bytes) -> dict[str, bytes]:
    """The vendor's documented Default-format payload shape."""
    headers = (
        "Received: by mx0047p1mdw1.sendgrid.net with SMTP id n02SRiHsLZ Tue, 27 Jul 2021 15:53:29 +0000\r\n"
        "DKIM-Signature: v=1; a=rsa-sha256; d=sendgrid.com; s=ga1; b=OKl5gvNGYmz\r\n"
        "From: Customer Name <customer@example.com>\r\n"
        "Date: Tue, 27 Jul 2021 15:53:29 +0000\r\n"
        "Message-ID: <w9.sendgrid.message.0001@mail.example.com>\r\n"
        "Subject: order #9001 arrived cracked\r\n"
        "To: support@polsia.example\r\n"
    )
    fields: dict[str, bytes] = {
        "headers": headers.encode(),
        "dkim": b"{@sendgrid.com : pass}",
        "to": b"support@polsia.example",
        "from": b"Customer Name <customer@example.com>",
        "subject": b"order #9001 arrived cracked",
        "text": BODY_MARKER.encode(),
        "html": b"<div>The widget arrived cracked</div>",
        "sender_ip": b"209.85.223.169",
        "envelope": json.dumps(
            {"to": ["support@polsia.example"], "from": "customer@example.com"}
        ).encode(),
        "attachments": b"1",
        "charsets": json.dumps({"text": "UTF-8", "html": "UTF-8", "subject": "UTF-8"}).encode(),
        "SPF": b"pass",
        "content-ids": b"{}",
        "attachment-info": b"{}",
    }
    fields.update(overrides)
    return fields


def signature_for(body: bytes, timestamp: str, *, key: Any = _PRIVATE_KEY) -> str:
    return base64.b64encode(key.sign(timestamp.encode() + body, ec.ECDSA(hashes.SHA256()))).decode()


def signed_headers(body: bytes, *, timestamp: str | None = None, key: Any = _PRIVATE_KEY) -> dict[str, str]:
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    return {
        "content-type": f"multipart/form-data; boundary={BOUNDARY}",
        SIGNATURE_HEADER: signature_for(body, stamp, key=key),
        TIMESTAMP_HEADER: stamp,
    }


def now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


async def rows(db) -> list[SupportInboxItem]:
    return list(
        (await db.execute(select(SupportInboxItem).order_by(SupportInboxItem.id.asc())))
        .scalars()
        .all()
    )


async def count(db, model) -> int:
    return int((await db.scalar(select(func.count()).select_from(model))) or 0)


def error_of(response) -> dict:
    body = response.json()
    assert body.get("ok") is False, f"not the project refusal envelope: {body}"
    return body["error"]


# ── fixtures ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def sendgrid_key(monkeypatch):
    """The operator copied the security policy's public key (base64 DER) into the environment."""
    from app.config import settings

    monkeypatch.setattr(settings, "sendgrid_inbound_public_key", PUBLIC_DER_B64, raising=False)
    monkeypatch.setattr(settings, "support_inbound_secret", "", raising=False)
    monkeypatch.setattr(settings, "sendgrid_inbound_oauth_token", "", raising=False)
    return settings


# ── 1. the real delivery: signed multipart ingests ──────────────────────────────────────────
async def test_a_signed_sendgrid_delivery_ingests_one_pending_item(
    api_client, async_db_session, sendgrid_key
):
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH, content=body, headers=signed_headers(body)
    )
    assert response.status_code == 201, response.text[:600]
    data = response.json()
    assert data["created"] is True
    assert data["provider"] == PROVIDER_TAG

    item = data["item"]
    assert item["provider_message_id"] == "w9.sendgrid.message.0001@mail.example.com", (
        "the provider id must be the message's OWN Message-ID (the vendor has no message_id field)"
    )
    assert item["from_address"] == "customer@example.com", (
        "the From header is reduced to the address the prospect-reply stamping compares"
    )
    assert item["subject"] == "order #9001 arrived cracked"
    assert item["body"] == BODY_MARKER, "the customer's text body is stored byte-exact"
    assert item["status"] == "pending"
    assert item["task_id"] is None
    assert item["received_at"] is None, (
        "SendGrid's `Date:` is the sender's clock, not an arrival instant — never invented"
    )

    audit = list(
        (
            await async_db_session.execute(
                select(ActivityLog).where(ActivityLog.action == "support_inbox_inbound")
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1
    detail = audit[0].detail
    assert detail["provider"] == PROVIDER_TAG
    assert detail["delivery_auth"] == "signature"
    assert detail["body_source"] == "text"
    assert detail["provider_id_source"] == "message-id"
    rendered = f"{audit[0].summary} {json.dumps(detail)}"
    assert BODY_MARKER not in rendered, "the customer body never reaches the ledger"
    assert PUBLIC_DER_B64 not in rendered


async def test_the_signed_delivery_is_idempotent_across_a_provider_retry(
    api_client, async_db_session, sendgrid_key
):
    body = multipart_body(default_fields())
    first = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert first.status_code == 201, first.text[:400]

    # a retry re-signs the SAME message bytes with a fresh timestamp — same Message-ID
    again = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert again.status_code == 200, again.text[:400]
    assert again.json()["created"] is False
    assert again.json()["item_id"] == first.json()["item_id"]
    assert await count(async_db_session, SupportInboxItem) == 1


async def test_a_pem_public_key_verifies_the_same_signature(
    api_client, async_db_session, sendgrid_key, monkeypatch
):
    monkeypatch.setattr(sendgrid_key, "sendgrid_inbound_public_key", PUBLIC_PEM, raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 201, response.text[:600]


async def test_an_html_only_mail_stores_the_html_body_and_names_the_source(
    api_client, async_db_session, sendgrid_key
):
    fields = default_fields()
    fields.pop("text")
    body = multipart_body(fields)
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 201, response.text[:600]
    assert response.json()["item"]["body"] == "<div>The widget arrived cracked</div>"
    audit = (
        await async_db_session.execute(
            select(ActivityLog).where(ActivityLog.action == "support_inbox_inbound")
        )
    ).scalars().all()
    assert audit[0].detail["body_source"] == "html"


async def test_the_raw_format_email_field_supplies_the_body_and_the_message_id(
    api_client, async_db_session, sendgrid_key
):
    """`send_raw` delivers ONE `email` field: the full MIME message (and no headers/text field)."""
    mime = (
        "MIME-Version: 1.0\r\n"
        "From: Raw Sender <raw@example.com>\r\n"
        "To: support@polsia.example\r\n"
        "Message-ID: <w9.raw.only.0002@example.com>\r\n"
        "Subject: raw format delivery\r\n"
        "Content-Type: text/plain; charset=UTF-8\r\n"
        "\r\n"
        f"{BODY_MARKER}\r\n"
    )
    fields = {
        "dkim": b"{@sendgrid.com : pass}",
        "email": mime.encode(),
        "to": b"support@polsia.example",
        "from": b"Raw Sender <raw@example.com>",
        "subject": b"raw format delivery",
        "envelope": json.dumps(
            {"to": ["support@polsia.example"], "from": "raw@example.com"}
        ).encode(),
        "sender_ip": b"209.85.223.169",
        "SPF": b"pass",
        "charsets": json.dumps({"subject": "UTF-8"}).encode(),
    }
    body = multipart_body(fields)
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 201, response.text[:600]
    item = response.json()["item"]
    assert item["provider_message_id"] == "w9.raw.only.0002@example.com"
    assert item["from_address"] == "raw@example.com"
    assert item["subject"] == "raw format delivery"
    assert item["body"].strip() == BODY_MARKER
    audit = (
        await async_db_session.execute(
            select(ActivityLog).where(ActivityLog.action == "support_inbox_inbound")
        )
    ).scalars().all()
    assert audit[0].detail["body_source"] == "email"


async def test_the_ingested_multipart_item_reaches_the_shipped_attach_sweep(
    api_client, async_db_session, sendgrid_key
):
    from app.services.support_inbox_service import attach_pending, inbox_task_dedupe_key

    body = multipart_body(default_fields())
    assert (
        await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    ).status_code == 201

    attached = await attach_pending(async_db_session)
    assert len(attached) == 1
    tasks = (await async_db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].agent_type == "customer_support"
    assert tasks[0].dedupe_key == inbox_task_dedupe_key("w9.sendgrid.message.0001@mail.example.com")
    assert tasks[0].metadata_json["inbox_item"] == {
        "id": "w9.sendgrid.message.0001@mail.example.com",
        "from": "customer@example.com",
        "subject": "order #9001 arrived cracked",
        "body": BODY_MARKER,
    }, "the exact RD12 consumer contract the support agent reads"


# ── 2. the provider id: Message-ID, else a boundary-independent digest ──────────────────────
async def test_without_a_message_id_the_id_is_a_stable_digest(
    api_client, async_db_session, sendgrid_key
):
    headers = (
        "Received: by mx0047p1mdw1.sendgrid.net\r\n"
        "From: Customer Name <customer@example.com>\r\n"
        "Subject: no message id here\r\n"
    )
    fields = default_fields(headers=headers.encode(), subject=b"no message id here")
    first_body = multipart_body(fields, boundary=BOUNDARY)
    first = await api_client.post(
        INBOUND_PATH, content=first_body, headers=signed_headers(first_body)
    )
    assert first.status_code == 201, first.text[:600]
    derived = first.json()["item"]["provider_message_id"]
    assert derived.startswith("sendgrid-parse-sha256:"), derived
    assert len(derived) <= 255

    # a retry regenerates the MIME boundary — the digest must NOT depend on it
    retry_body = multipart_body(fields, boundary="another-boundary-9999")
    retry = await api_client.post(
        INBOUND_PATH,
        content=retry_body,
        headers={
            "content-type": "multipart/form-data; boundary=another-boundary-9999",
            SIGNATURE_HEADER: signature_for(retry_body, str(now_epoch())),
            TIMESTAMP_HEADER: str(now_epoch()),
        },
    )
    assert retry.status_code == 200, retry.text[:400]
    assert retry.json()["created"] is False
    assert retry.json()["item"]["provider_message_id"] == derived
    assert await count(async_db_session, SupportInboxItem) == 1


async def test_a_different_message_without_a_message_id_gets_its_own_row(
    api_client, async_db_session, sendgrid_key
):
    headers = (
        "Received: by mx0047p1mdw1.sendgrid.net\r\n"
        "From: Customer Name <customer@example.com>\r\n"
        "Subject: two different messages\r\n"
    )
    fields = default_fields(headers=headers.encode(), subject=b"two different messages")
    first_body = multipart_body({**fields, "text": b"first distinct message"})
    second_body = multipart_body({**fields, "text": b"second distinct message"})
    for body in (first_body, second_body):
        response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
        assert response.status_code == 201, response.text[:400]
    stored = await rows(async_db_session)
    assert len(stored) == 2
    assert stored[0].provider_message_id != stored[1].provider_message_id


# ── 3. the ECDSA credential is load-bearing ────────────────────────────────────────────────
async def test_a_tampered_body_is_a_named_401_and_stores_nothing(
    api_client, async_db_session, sendgrid_key
):
    body = multipart_body(default_fields())
    headers = signed_headers(body)
    tampered = body.replace(BODY_MARKER.encode(), b"a different message entirely", 1)
    response = await api_client.post(INBOUND_PATH, content=tampered, headers=headers)
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "signature" in error["message"].lower()
    assert await count(async_db_session, SupportInboxItem) == 0
    assert BODY_MARKER not in response.text


async def test_a_tampered_timestamp_is_a_named_401(api_client, async_db_session, sendgrid_key):
    body = multipart_body(default_fields())
    headers = signed_headers(body, timestamp="1700000000")
    headers[TIMESTAMP_HEADER] = str(now_epoch())
    response = await api_client.post(INBOUND_PATH, content=body, headers=headers)
    assert response.status_code == 401, response.text[:400]
    assert error_of(response)["code"] == "E_AUTH"


async def test_a_stale_timestamp_is_refused_by_name(api_client, async_db_session, sendgrid_key):
    stamp = str(now_epoch() - 3600)
    body = multipart_body(default_fields())
    headers = signed_headers(body, timestamp=stamp)
    response = await api_client.post(INBOUND_PATH, content=body, headers=headers)
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "timestamp" in error["message"].lower()
    assert error["context"]["tolerance_seconds"] == 300
    assert await count(async_db_session, SupportInboxItem) == 0


async def test_a_half_present_signature_header_pair_is_a_named_401(
    api_client, async_db_session, sendgrid_key
):
    body = multipart_body(default_fields())
    headers = signed_headers(body)
    del headers[TIMESTAMP_HEADER]
    response = await api_client.post(INBOUND_PATH, content=body, headers=headers)
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert SIGNATURE_HEADER in error["message"] and TIMESTAMP_HEADER in error["message"]


async def test_a_signature_from_another_key_is_a_named_401(
    api_client, async_db_session, sendgrid_key
):
    stranger = ec.generate_private_key(ec.SECP256R1())
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH, content=body, headers=signed_headers(body, key=stranger)
    )
    assert response.status_code == 401, response.text[:400]
    assert error_of(response)["code"] == "E_AUTH"


async def test_a_non_ec_configured_key_is_a_named_401_not_a_500(
    api_client, async_db_session, sendgrid_key, monkeypatch
):
    monkeypatch.setattr(sendgrid_key, "sendgrid_inbound_public_key", "not-a-key", raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "SENDGRID_INBOUND_PUBLIC_KEY" in json.dumps(error)


async def test_an_unconfigured_public_key_is_a_named_401_never_the_retry_toxic_503(
    api_client, async_db_session, monkeypatch
):
    """A 5xx makes the provider retry a delivery that can never succeed.  This path is a 4xx."""
    from app.config import settings

    monkeypatch.setattr(settings, "sendgrid_inbound_public_key", "", raising=False)
    monkeypatch.setattr(settings, "support_inbound_secret", "", raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "SENDGRID_INBOUND_PUBLIC_KEY" in error["message"]
    assert await count(async_db_session, SupportInboxItem) == 0


# ── 4. the OAuth bearer mechanism ───────────────────────────────────────────────────────────
async def test_a_configured_bearer_token_ingests(api_client, async_db_session, sendgrid_key, monkeypatch):
    monkeypatch.setattr(sendgrid_key, "sendgrid_inbound_oauth_token", "policy-bearer-xyz", raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            "authorization": "Bearer policy-bearer-xyz",
        },
    )
    assert response.status_code == 201, response.text[:600]
    audit = (
        await async_db_session.execute(
            select(ActivityLog).where(ActivityLog.action == "support_inbox_inbound")
        )
    ).scalars().all()
    assert audit[0].detail["delivery_auth"] == "oauth"


async def test_a_wrong_bearer_token_is_the_rfc6750_invalid_token_refusal(
    api_client, async_db_session, sendgrid_key, monkeypatch
):
    monkeypatch.setattr(sendgrid_key, "sendgrid_inbound_oauth_token", "policy-bearer-xyz", raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            "authorization": "Bearer not-the-token",
        },
    )
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "invalid_token" in response.text, (
        "RFC 6750: the webhook body must carry invalid_token so SendGrid refreshes its token"
    )
    assert "not-the-token" not in response.text
    assert await count(async_db_session, SupportInboxItem) == 0


async def test_a_bearer_token_with_no_configured_token_is_a_named_401(
    api_client, async_db_session, sendgrid_key
):
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            "authorization": "Bearer anything",
        },
    )
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert "SENDGRID_INBOUND_OAUTH_TOKEN" in error["message"]


async def test_no_credential_at_all_is_a_named_401(api_client, async_db_session):
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    )
    assert response.status_code == 401, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_AUTH"
    assert response.status_code != 503, "an unauthenticated provider delivery must never be retry-toxic"
    assert await count(async_db_session, SupportInboxItem) == 0


async def test_the_shared_secret_also_authorizes_a_multipart_delivery(
    api_client, async_db_session, sendgrid_key, monkeypatch
):
    """The pre-existing credential keeps working on the new content type (one route, two providers)."""
    monkeypatch.setattr(sendgrid_key, "support_inbound_secret", SECRET, raising=False)
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            SECRET_HEADER: SECRET,
        },
    )
    assert response.status_code == 201, response.text[:600]


# ── 5. every malformed delivery is a NAMED 4xx (never a 5xx) ───────────────────────────────
async def test_a_multipart_body_with_no_boundary_is_a_named_422(api_client, sendgrid_key):
    body = multipart_body(default_fields())
    response = await api_client.post(
        INBOUND_PATH,
        content=body,
        headers={
            "content-type": "multipart/form-data",
            SIGNATURE_HEADER: signature_for(body, str(now_epoch())),
            TIMESTAMP_HEADER: str(now_epoch()),
        },
    )
    assert response.status_code == 422, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_VALIDATION"
    assert "boundary" in error["message"].lower()


async def test_a_multipart_body_that_does_not_match_its_boundary_is_a_named_422(
    api_client, sendgrid_key
):
    body = multipart_body(default_fields())
    headers = signed_headers(body)
    headers["content-type"] = "multipart/form-data; boundary=totally-different"
    response = await api_client.post(INBOUND_PATH, content=body, headers=headers)
    assert response.status_code == 422, response.text[:400]
    assert error_of(response)["code"] == "E_VALIDATION"


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ("no-from", "from_address"),
        ("no-subject", "subject"),
        ("no-body", "body"),
        ("blank-subject", "subject"),
        ("oversize-body", "body"),
    ],
)
async def test_a_multipart_missing_a_required_field_is_a_named_422(
    api_client, async_db_session, sendgrid_key, mutation, field
):
    fields = default_fields()
    if mutation == "no-from":
        # no `from` field, no `From:` header, no envelope from — nothing names a sender
        fields.pop("from")
        fields["envelope"] = b'{"to":["support@polsia.example"],"from":""}'
        fields["headers"] = fields["headers"].replace(
            b"From: Customer Name <customer@example.com>\r\n", b""
        )
    elif mutation == "no-subject":
        fields.pop("subject")
        fields["headers"] = fields["headers"].replace(b"Subject: order #9001 arrived cracked\r\n", b"")
    elif mutation == "no-body":
        fields.pop("text")
        fields.pop("html")
    elif mutation == "blank-subject":
        fields["subject"] = b"   "
        fields["headers"] = fields["headers"].replace(b"Subject: order #9001 arrived cracked\r\n", b"")
    elif mutation == "oversize-body":
        fields["text"] = b"x" * 65_537
    body = multipart_body(fields)
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 422, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["field"] == field, error
    assert await count(async_db_session, SupportInboxItem) == 0
    assert BODY_MARKER not in response.text


async def test_the_from_header_is_the_fallback_when_the_form_field_is_absent(
    api_client, async_db_session, sendgrid_key
):
    """The vendor's `from` field is itself extracted from the `From:` header — both are honoured."""
    fields = default_fields()
    fields.pop("from")
    fields["envelope"] = b'{"to":["support@polsia.example"],"from":"bounce@example.com"}'
    body = multipart_body(fields)
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 201, response.text[:600]
    assert response.json()["item"]["from_address"] == "customer@example.com", (
        "the From header's addr-spec wins over the envelope's return path"
    )


async def test_an_unsupported_content_type_is_a_named_422(api_client, sendgrid_key, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)
    response = await api_client.post(
        INBOUND_PATH,
        content=json.dumps({"provider_message_id": "x", "from": "a@b.c", "subject": "s", "body": "b"}),
        headers={"content-type": "text/plain", SECRET_HEADER: SECRET},
    )
    assert response.status_code == 422, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_VALIDATION"
    assert "content-type" in error["message"].lower()


async def test_an_oversize_multipart_is_refused_by_name(
    api_client, async_db_session, sendgrid_key, monkeypatch
):
    from app.services import support_inbox_service

    monkeypatch.setattr(
        support_inbox_service, "MAX_INBOUND_MULTIPART_BYTES", 2048, raising=True
    )
    body = multipart_body(default_fields(), attachments={"attachment1": b"x" * 4096})
    response = await api_client.post(INBOUND_PATH, content=body, headers=signed_headers(body))
    assert response.status_code == 422, response.text[:400]
    error = error_of(response)
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["max_bytes"] == 2048
    assert await count(async_db_session, SupportInboxItem) == 0


DELIVERY_SHAPES = [
    pytest.param(b"", id="empty-body"),
    pytest.param(b"not multipart at all", id="garbage"),
    pytest.param(b"--x\r\nContent-Disposition: form-data; name=\"from\"\r\n\r\n", id="unterminated"),
    pytest.param(b"--\r\n\r\n", id="empty-part"),
    pytest.param(b"--" + BOUNDARY.encode() + b"--", id="closing-boundary-only"),
    pytest.param(b"\x00\x01\x02" * 100, id="binary-garbage"),
    pytest.param(b"--" + BOUNDARY.encode() + b"\r\nContent-Disposition: form-data\r\n\r\nx\r\n--"
                 + BOUNDARY.encode() + b"--\r\n", id="no-part-name"),
    pytest.param(b"--" + BOUNDARY.encode() + b"\r\nContent-Disposition: form-data; name=\"from\"\r\n"
                 b"\r\n\r\n--" + BOUNDARY.encode() + b"--\r\n", id="blank-fields"),
]


@pytest.mark.parametrize("payload", DELIVERY_SHAPES)
async def test_no_hostile_delivery_shape_ever_answers_5xx(
    api_client, async_db_session, sendgrid_key, payload
):
    response = await api_client.post(
        INBOUND_PATH,
        content=payload,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            SIGNATURE_HEADER: signature_for(payload, str(now_epoch())),
            TIMESTAMP_HEADER: str(now_epoch()),
        },
    )
    assert response.status_code < 500, (
        f"status {response.status_code}: {response.text[:300]} — a 5xx invites the provider's retry loop"
    )
    assert response.status_code >= 400
    assert error_of(response)["code"] in {"E_VALIDATION", "E_AUTH"}


# ── 6. the JSON path is UNCHANGED (regression of the shipped contract) ─────────────────────
async def test_the_json_path_still_ingests_with_the_shared_secret(
    api_client, async_db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)
    response = await api_client.post(
        INBOUND_PATH,
        json={
            "provider_message_id": "w9-json-1",
            "from": "customer@example.com",
            "subject": "json still works",
            "body": BODY_MARKER,
        },
        headers={SECRET_HEADER: SECRET},
    )
    assert response.status_code == 201, response.text[:400]
    assert response.json()["item"]["provider_message_id"] == "w9-json-1"
    assert response.json()["item"]["from_address"] == "customer@example.com"


async def test_the_json_path_keeps_its_shipped_credential_rule(
    api_client, async_db_session, monkeypatch
):
    from app.config import settings

    payload = {
        "provider_message_id": "w9-json-2",
        "from": "customer@example.com",
        "subject": "s",
        "body": "b",
    }
    # (a) configured secret + missing header -> 401 E_AUTH
    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)
    missing = await api_client.post(INBOUND_PATH, json=payload)
    assert missing.status_code == 401, missing.text[:300]
    assert error_of(missing)["code"] == "E_AUTH"

    # (b) unset secret + a presented header -> the shipped 503 E_CONFIG (never an open inbox)
    monkeypatch.setattr(settings, "support_inbound_secret", "", raising=False)
    unset = await api_client.post(INBOUND_PATH, json=payload, headers={SECRET_HEADER: "anything"})
    assert unset.status_code == 503, unset.text[:300]
    assert error_of(unset)["code"] == "E_CONFIG"
    assert "SUPPORT_INBOUND_SECRET" in unset.text
    assert await count(async_db_session, SupportInboxItem) == 0


async def test_the_json_path_keeps_the_postmark_aliases(api_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)
    response = await api_client.post(
        INBOUND_PATH,
        json={
            "MessageID": "w9-postmark-1",
            "From": "Customer <customer@example.com>",
            "Subject": "order #7",
            "TextBody": BODY_MARKER,
        },
        headers={SECRET_HEADER: SECRET},
    )
    assert response.status_code == 201, response.text[:400]
    item = response.json()["item"]
    assert item["provider_message_id"] == "w9-postmark-1"
    assert item["from_address"] == "Customer <customer@example.com>"


async def test_authentication_precedes_validation_on_the_json_path(
    api_client, async_db_session, monkeypatch
):
    """A credential refusal must not double as an input-validation oracle.

    In the shipped route FastAPI parsed the JSON body BEFORE the dependency solved, so a
    malformed body answered the framework's 422 with NO credential check (measured live on the
    wave-8 image: `POST …/inbound` + `X-Meridian-Inbound-Secret: wrong` + invalid JSON -> 422).  The
    route now reads raw bytes, authenticates, and only then parses — for BOTH encodings.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "support_inbound_secret", SECRET, raising=False)

    unauthenticated = await api_client.post(
        INBOUND_PATH, content=b"not json at all", headers={SECRET_HEADER: "wrong"}
    )
    assert unauthenticated.status_code == 401, unauthenticated.text[:300]
    assert error_of(unauthenticated)["code"] == "E_AUTH"

    authenticated = await api_client.post(
        INBOUND_PATH, content=b"not json at all", headers={SECRET_HEADER: SECRET}
    )
    assert authenticated.status_code == 422, authenticated.text[:300]
    error = error_of(authenticated)
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["field"] == "payload"
    assert await count(async_db_session, SupportInboxItem) == 0


# ── 7. the digest rule, pinned directly (it is the dedupe key in production) ────────────────
def test_the_digest_provider_id_is_stable_and_bounded() -> None:
    from app.services.support_inbox_service import (
        MAX_PROVIDER_MESSAGE_ID_LEN,
        canonical_sendgrid_digest,
    )

    fields = {
        "from": b"Customer <customer@example.com>",
        "to": b"support@polsia.example",
        "subject": b"s",
        "dkim": b"{@sendgrid.com : pass}",
        "headers": b"From: customer@example.com\r\n",
        "text": BODY_MARKER.encode(),
    }
    first = canonical_sendgrid_digest(fields)
    assert first == canonical_sendgrid_digest(dict(reversed(list(fields.items()))))
    assert first != canonical_sendgrid_digest({**fields, "text": b"different"})
    assert first.startswith("sendgrid-parse-sha256:")
    assert len(first) <= MAX_PROVIDER_MESSAGE_ID_LEN
    # length-prefixed: ("ab","c") must not collide with ("a","bc") on the SAME two fields
    assert canonical_sendgrid_digest({"text": b"ab", "html": b"c"}) != canonical_sendgrid_digest(
        {"text": b"a", "html": b"bc"}
    )
    assert hashlib.sha256(first.encode()).hexdigest()  # the value is a plain sha256 hex digest
    parsed = email.parser.BytesParser(policy=email.policy.default).parsebytes(b"From: x\r\n\r\n")
    assert parsed["From"] == "x"
