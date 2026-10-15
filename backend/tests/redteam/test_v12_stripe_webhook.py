"""V12 red-team — the unauthenticated Stripe webhook under attack.

Surface: `POST /api/v1/finance/stripe/webhook` (`app/api/v1/finance.py`) + the stdlib verifier
(`_stdlib_verify` — the documented production path on a host without the SDK; the SDK itself is
installed in this venv, so the endpoint legs exercise whichever branch the tree actually runs).

Attack list:
  1. missing signature header
  2. wrong signing secret
  3. NO secret configured (fail-closed: 500, nothing processed)
  4. tampered body carrying a valid-shape signature (computed over DIFFERENT bytes)
  5. replayed event id (no double row, no overwrite of the first delivery)
  6. unknown event types (recorded-not-acted)
  7. stale timestamp outside the tolerance (a captured request cannot replay forever)
  8. missing event id (cannot be made idempotent -> refused by name)
  9. stdlib-verifier legs: rotation accept-any, garbage JSON, tolerance edges
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import finance as finance_module
from app.config import settings
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent

SECRET = "whsec_redteam_v12"
WEBHOOK = "/api/v1/finance/stripe/webhook"


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: Any):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)


def _body(**over: Any) -> bytes:
    event: dict[str, Any] = {
        "id": "evt_redteam_1",
        "type": "charge.succeeded",
        "data": {"object": {"amount": 4200, "customer": "cus_redteam",
                            "currency": "usd", "payment_intent": "pi_redteam_1"}},
    }
    event.update(over)
    return json.dumps(event).encode()


def _header(secret: str, body: bytes, *, t: int | None = None, extra_v1: list[str] | None = None) -> str:
    """A Stripe-shaped `Stripe-Signature` header for `body` (the scheme both verifiers implement)."""
    ts = str(int(time.time()) if t is None else t)
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    parts = [f"t={ts}"] + [f"v1={value}" for value in ([*extra_v1, sig] if extra_v1 else [sig])]
    return ",".join(parts)


async def _event_rows(db: AsyncSession) -> list[StripeEvent]:
    rows = (await db.execute(select(StripeEvent))).scalars().all()
    return list(rows)


async def test_missing_signature_header_is_a_400_and_stores_nothing(api_client, async_db_session):
    resp = await api_client.post(WEBHOOK, content=_body())
    assert resp.status_code == 400
    assert len(await _event_rows(async_db_session)) == 0


async def test_wrong_secret_is_a_400_and_stores_nothing(api_client, async_db_session):
    """ATTACK: sign with a secret the server does not hold (a guessed/leaked whitespace variant)."""
    body = _body()
    resp = await api_client.post(
        WEBHOOK, content=body, headers={"stripe-signature": _header("wrong-secret", body)}
    )
    assert resp.status_code == 400
    assert len(await _event_rows(async_db_session)) == 0


async def test_no_secret_configured_fails_closed_before_processing(api_client, async_db_session, monkeypatch):
    """ATTACK: hit the webhook on an install with no secret — must be refused, not accepted blind."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    body = _body()
    resp = await api_client.post(
        WEBHOOK, content=body, headers={"stripe-signature": _header(SECRET, body)}
    )
    assert resp.status_code == 500                      # the documented fail-closed posture
    assert "secret not configured" in resp.text
    assert len(await _event_rows(async_db_session)) == 0


async def test_tampered_body_with_a_valid_signature_shape_is_a_400(api_client, async_db_session):
    """ATTACK: intercept a delivery and flip the amount; the signature stays valid-looking.

    The signature is over the RAW bytes, so any mutation of the body must fail verification —
    the stored amount can never be attacker-chosen.
    """
    signed = _body()
    tampered = signed.replace(b"4200", b"999999")
    assert tampered != signed
    resp = await api_client.post(
        WEBHOOK, content=tampered, headers={"stripe-signature": _header(SECRET, signed)}
    )
    assert resp.status_code == 400
    assert len(await _event_rows(async_db_session)) == 0


async def test_replayed_event_id_records_one_row_and_never_overwrites(api_client, async_db_session):
    """ATTACK: deliver the same event id twice (Stripe's own retry), then once with changed bytes.

    The insert is `ON CONFLICT (stripe_event_id) DO NOTHING`: exactly ONE row survives the
    replay, a second delivery cannot double-count, and a changed re-delivery cannot rewrite the
    first record's money.
    """
    first = _body()
    second = _body()
    assert (
        await api_client.post(WEBHOOK, content=first, headers={"stripe-signature": _header(SECRET, first)})
    ).status_code == 200
    assert (
        await api_client.post(WEBHOOK, content=second, headers={"stripe-signature": _header(SECRET, second)})
    ).status_code == 200

    changed = _body(**{"data": {"object": {"amount": 1, "customer": "cus_attacker", "currency": "usd"}}})
    resp = await api_client.post(
        WEBHOOK, content=changed, headers={"stripe-signature": _header(SECRET, changed)}
    )
    assert resp.status_code == 200

    rows = await _event_rows(async_db_session)
    assert len(rows) == 1, "a replayed event id must not create a second row"
    assert rows[0].stripe_event_id == "evt_redteam_1"
    assert rows[0].amount_cents == 4200, "the first delivery's money must survive the replay"
    assert rows[0].customer_id == "cus_redteam"


async def test_unknown_event_type_is_recorded_not_acted_on(api_client, async_db_session):
    """ATTACK: fire an event type the tree has never heard of — recorded verbatim, nothing flips."""
    body = _body(id="evt_unknown_1", type="customer.subscription.turtles")
    resp = await api_client.post(
        WEBHOOK, content=body, headers={"stripe-signature": _header(SECRET, body)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True}

    rows = await _event_rows(async_db_session)
    assert [row.event_type for row in rows] == ["customer.subscription.turtles"]
    # recorded-not-acted: no money artifact was produced by the unknown type.
    assert (await async_db_session.execute(select(func.count()).select_from(RevenueSnapshot))).scalar_one() == 0
    assert (await async_db_session.execute(select(func.count()).select_from(ExpenseRecord))).scalar_one() == 0


async def test_a_stale_timestamp_is_refused_by_the_tolerance(api_client, async_db_session):
    """ATTACK: replay a captured delivery an hour later — outside the 300s window it must 400."""
    body = _body(id="evt_stale_1")
    resp = await api_client.post(
        WEBHOOK, content=body, headers={"stripe-signature": _header(SECRET, body, t=int(time.time()) - 4000)}
    )
    assert resp.status_code == 400
    assert len(await _event_rows(async_db_session)) == 0


async def test_an_event_without_an_id_is_refused_by_name(api_client, async_db_session):
    """ATTACK: an id-less event cannot be deduplicated — storing it would double-count on retry."""
    body = _body(id="")
    resp = await api_client.post(
        WEBHOOK, content=body, headers={"stripe-signature": _header(SECRET, body)}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert len(await _event_rows(async_db_session)) == 0


# ── the stdlib verifier (the no-SDK production path) ─────────────────────────────────────────


def test_stdlib_verifier_accepts_a_match_anywhere_in_a_rotated_header():
    """Secret ROTATION: several `v1=` values in one header; a match with ANY must verify (R32).

    Before R32 only the LAST candidate was kept, so a rotated header whose matching signature
    was not last was refused.  Both orders are pinned here.
    """
    body = _body()
    ts = str(int(time.time()))
    good = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    for header in (
        f"t={ts},v1=deadbeef,v1={good}",
        f"t={ts},v1={good},v1=deadbeef",
    ):
        payload = finance_module._stdlib_verify(body, header, SECRET, 300)
        assert payload["id"] == "evt_redteam_1", header


def test_stdlib_verifier_refuses_wrong_secret_garbage_json_and_tolerance():
    """The fallback's own edges: wrong secret, non-JSON body, stale timestamp — all 400."""
    body = _body()
    ts = str(int(time.time()))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as wrong:
        finance_module._stdlib_verify(body, f"t={ts},v1=deadbeef", SECRET, 300)
    assert wrong.value.status_code == 400

    garbage = b"{not json"
    gts = str(int(time.time()))
    gsig = hmac.new(SECRET.encode(), f"{gts}.".encode() + garbage, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as bad_json:
        finance_module._stdlib_verify(garbage, f"t={gts},v1={gsig}", SECRET, 300)
    assert bad_json.value.status_code == 400
    assert "JSON" in bad_json.value.detail

    with pytest.raises(HTTPException) as stale:
        finance_module._stdlib_verify(body, f"t={int(time.time()) - 4000},v1={ts}", SECRET, 300)
    assert stale.value.status_code == 400
    assert "tolerance" in stale.value.detail
