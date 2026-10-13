"""Test GET /api/v1/finance/* endpoints."""
import hashlib
import hmac
import json
import re
import time
from datetime import date

import pytest


@pytest.mark.asyncio
async def test_finance_summary_empty(api_client, auth_headers):
    """Returns explicit nulls — not zeros — when nothing was measured.

    REPAIRED (W3-refusals, T6): this test previously pinned the collapsed state —
    old: `assert data["mrr_cents"] == 0` (and arr_cents/active_subscribers) for an
    empty database; new: every money field is `None`.  No snapshot means "not
    measured", and the API no longer dresses that up as a measured zero.
    """
    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    # Precisely, key by key: each of the six money fields is JSON null, and
    # `last_snapshot_date` is null.  The regexes below prove the RAW BODY agrees —
    # for exactly those keys, no `"<key>": 0` / `0.0` hides anywhere.
    assert data == {
        "mrr_cents": None,
        "arr_cents": None,
        "active_subscribers": None,
        "total_ad_spend_usd": None,
        "total_expenses_month_cents": None,
        "stripe_balance_cents": None,
        "last_snapshot_date": None,
    }
    for key in (
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "total_ad_spend_usd",
        "total_expenses_month_cents",
        "stripe_balance_cents",
    ):
        assert not re.search(rf'"{key}"\s*:\s*0', resp.text), (key, resp.text)


@pytest.mark.asyncio
async def test_finance_summary_with_snapshot(api_client, auth_headers, async_db_session):
    """Returns values from the latest revenue snapshot."""
    from app.models.finance import RevenueSnapshot

    snap = RevenueSnapshot(
        snapshot_date=date.today(),
        mrr_cents=500000,
        arr_cents=6000000,
        active_subscribers=42,
        stripe_balance_cents=150000,
    )
    async_db_session.add(snap)
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["mrr_cents"] == 500000
    assert data["arr_cents"] == 6000000
    assert data["active_subscribers"] == 42


@pytest.mark.asyncio
async def test_finance_summary_requires_auth(api_client):
    resp = await api_client.get("/api/v1/finance/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_finance_revenue_empty(api_client, auth_headers):
    resp = await api_client.get("/api/v1/finance/revenue", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_finance_revenue_returns_snapshots(api_client, auth_headers, async_db_session):
    from datetime import timedelta

    from app.models.finance import RevenueSnapshot

    for i in range(3):
        snap = RevenueSnapshot(
            snapshot_date=date.today() - timedelta(days=i),
            mrr_cents=100000 * (3 - i),   # today=300k, yesterday=200k, 2 days ago=100k
            arr_cents=1200000 * (3 - i),
        )
        async_db_session.add(snap)
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/revenue?limit=10", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    # Most recent first
    assert data[0]["mrr_cents"] >= data[1]["mrr_cents"]


@pytest.mark.asyncio
async def test_finance_expenses_empty(api_client, auth_headers):
    resp = await api_client.get("/api/v1/finance/expenses", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_finance_expenses_filter_by_category(api_client, auth_headers, async_db_session):
    from app.models.finance import ExpenseRecord

    async_db_session.add(ExpenseRecord(category="ads", vendor="Google", amount_cents=5000, currency="usd", date=date.today()))
    async_db_session.add(ExpenseRecord(category="software", vendor="GitHub", amount_cents=1000, currency="usd", date=date.today()))
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/expenses?category=ads", headers=auth_headers)
    data = resp.json()
    assert len(data) == 1
    assert data[0]["category"] == "ads"


@pytest.mark.asyncio
async def test_finance_stripe_events_empty(api_client, auth_headers):
    resp = await api_client.get("/api/v1/finance/events", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_stripe_webhook_missing_secret(api_client):
    """Returns 500 when webhook secret is not configured."""
    from app.config import settings
    original = settings.stripe_webhook_secret
    settings.stripe_webhook_secret = ""
    try:
        resp = await api_client.post(
            "/api/v1/finance/stripe/webhook",
            content=b'{"id":"evt_test"}',
            headers={"stripe-signature": "t=1,v1=abc"},
        )
        assert resp.status_code == 500
    finally:
        settings.stripe_webhook_secret = original


@pytest.mark.asyncio
async def test_stripe_webhook_invalid_signature(api_client):
    """Returns 400 on bad Stripe signature."""
    from app.config import settings
    settings.stripe_webhook_secret = "whsec_test"
    try:
        resp = await api_client.post(
            "/api/v1/finance/stripe/webhook",
            content=b'{"id":"evt_test","type":"payment_intent.succeeded"}',
            headers={"stripe-signature": "t=1,v1=badsig"},
        )
        assert resp.status_code == 400
    finally:
        settings.stripe_webhook_secret = ""


def _stripe_signature(secret: str, body: bytes) -> str:
    """The documented `t=...,v1=...` header: HMAC-SHA256 over `"<t>.<body>"`."""
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.mark.asyncio
async def test_stripe_webhook_amount_zero_is_stored_not_null(api_client, auth_headers):
    """F-11: `amount: 0` is a MEASURED zero and must survive as 0.

    Old code (`amount or amount_paid`) turned a 0 amount into "absent" and stored
    NULL — a measured zero collapsed into "not measured".  Pinned here: a zero-amount
    event reads back as 0, `amount_paid` is consulted only when `amount` is really
    missing, and with neither field the column is NULL (never 0).
    """
    from app.config import settings
    secret = "whsec_test"
    settings.stripe_webhook_secret = secret
    try:
        zero = json.dumps({
            "id": "evt_zero_amount",
            "type": "charge.succeeded",
            "data": {"object": {"amount": 0, "currency": "usd"}},
        }).encode("utf-8")
        resp = await api_client.post(
            "/api/v1/finance/stripe/webhook",
            content=zero,
            headers={"stripe-signature": _stripe_signature(secret, zero)},
        )
        assert resp.status_code == 200, resp.text

        fallback = json.dumps({
            "id": "evt_amount_paid",
            "type": "charge.succeeded",
            "data": {"object": {"amount_paid": 250, "currency": "usd"}},
        }).encode("utf-8")
        resp = await api_client.post(
            "/api/v1/finance/stripe/webhook",
            content=fallback,
            headers={"stripe-signature": _stripe_signature(secret, fallback)},
        )
        assert resp.status_code == 200, resp.text

        absent = json.dumps({
            "id": "evt_no_amount",
            "type": "charge.succeeded",
            "data": {"object": {"currency": "usd"}},
        }).encode("utf-8")
        resp = await api_client.post(
            "/api/v1/finance/stripe/webhook",
            content=absent,
            headers={"stripe-signature": _stripe_signature(secret, absent)},
        )
        assert resp.status_code == 200, resp.text
    finally:
        settings.stripe_webhook_secret = ""

    resp = await api_client.get("/api/v1/finance/events", headers=auth_headers)
    assert resp.status_code == 200
    by_id = {row["stripe_event_id"]: row for row in resp.json()}
    assert by_id["evt_zero_amount"]["amount_cents"] == 0      # a measured zero stays zero
    assert by_id["evt_amount_paid"]["amount_cents"] == 250    # fallback still works
    assert by_id["evt_no_amount"]["amount_cents"] is None     # absent stays absent
