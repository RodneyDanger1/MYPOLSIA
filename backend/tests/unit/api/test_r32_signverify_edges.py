"""R32 — route-level: rotated multi-`v1=` deliveries + the daily-report DB-fault refusal.

Two R32 items, exercised through the REAL app:

* the webhook route with the SDK forced absent (the stdlib fallback is the code under test;
  with the SDK present the same cases would flow through `stripe.Webhook.construct_event` and
  prove nothing about the fallback) — a rotated header whose matching v1 is NOT last must now
  verify, all-invalid must still refuse by name, single-v1 stays unchanged;
* the same accept cases with the SDK present (both production configurations accept rotation);
* `GET /api/v1/dashboard/reports/daily` on a DB fault (the `daily_reports` table is dropped —
  a real driver failure, no mocks): a named `E_DB`/503 refusal with the fix hint reachable,
  never the codeless 500 R28 measured (receipt REQUESTS #4).  Both raw-execute sites in
  `report_service` are covered: `get_daily_report` (via the route and directly) and
  `get_latest_daily_report` (directly).

Every signature is stdlib HMAC over `"<t>.<raw body>"` — the same construction the code
verifies; no live Stripe call, no invented header shape.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError

SECRET = "whsec_r32_edges"
OLD_SECRET = "whsec_r32_edges_old"  # the pre-rotation secret
NOW = int(time.time())


def _digest(body: bytes, *, ts: int = NOW, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _sign(body: bytes, *, ts: int = NOW, secret: str = SECRET) -> str:
    return f"t={ts},v1={_digest(body, ts=ts, secret=secret)}"


def _body(event_id: str) -> bytes:
    return json.dumps({"id": event_id, "type": "charge.succeeded"}).encode("utf-8")


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    return SECRET


@pytest.fixture
def sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented no-SDK production state: `import stripe` raises ImportError."""
    monkeypatch.setitem(sys.modules, "stripe", None)


async def _post_webhook(client, body: bytes, header: str):
    # No X-API-Key: this route is unauthenticated by design (Stripe cannot send one).
    return await client.post(
        "/api/v1/finance/stripe/webhook",
        content=body,
        headers={"stripe-signature": header},
    )


async def _stored_ids(client, auth_headers) -> list[str]:
    resp = await client.get("/api/v1/finance/events?limit=500", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return sorted(row["stripe_event_id"] for row in resp.json())


def _rotated_header(body: bytes, shape: str) -> str:
    """The task matrix: a matching v1 surrounded by stale/garbage/old-secret candidates."""
    stale = _digest(body, ts=NOW - 3600)  # right secret, timestamp outside any window
    old = _digest(body, ts=NOW, secret=OLD_SECRET)  # the pre-rotation secret
    fresh = _digest(body)
    return {
        "stale_invalid_then_valid": f"t={NOW},v1={stale},v1={fresh}",
        "valid_then_garbage": f"t={NOW},v1={fresh},v1=deadbeef",
        "garbage_then_valid": f"t={NOW},v1=deadbeef,v1={fresh}",
        "valid_then_stale": f"t={NOW},v1={fresh},v1={stale}",
        "valid_then_old_secret": f"t={NOW},v1={fresh},v1={old}",
        "old_secret_then_valid": f"t={NOW},v1={old},v1={fresh}",
    }[shape]


# ── 1. the webhook route with the stdlib fallback ────────────────────────────────


@pytest.mark.parametrize(
    "shape",
    [
        "stale_invalid_then_valid",
        "valid_then_garbage",
        "garbage_then_valid",
        "valid_then_stale",
        "valid_then_old_secret",
        "old_secret_then_valid",
    ],
)
@pytest.mark.asyncio
async def test_rotated_multi_v1_delivery_verifies_without_the_sdk(
    api_client, webhook_secret, sdk_absent, auth_headers, shape
):
    """Old fallback: only the LAST v1 was tried — `valid_then_garbage`/`valid_then_stale`/
    `valid_then_old_secret` were refused (R28 F-7, `raw/probe_signature_rotation_before.txt`).
    Now: any matching candidate verifies and the event is stored."""
    event_id = f"evt_r32_{shape}"
    body = _body(event_id)
    resp = await _post_webhook(api_client, body, _rotated_header(body, shape))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}
    assert await _stored_ids(api_client, auth_headers) == [event_id]


@pytest.mark.asyncio
async def test_all_invalid_v1s_still_refuse_and_nothing_is_stored(
    api_client, webhook_secret, sdk_absent, auth_headers
):
    body = _body("evt_r32_all_invalid")
    resp = await _post_webhook(api_client, body, f"t={NOW},v1=deadbeef,v1=cafebabe")
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Invalid Stripe signature"}
    assert await _stored_ids(api_client, auth_headers) == []


@pytest.mark.asyncio
async def test_single_v1_delivery_is_unchanged(api_client, webhook_secret, sdk_absent, auth_headers):
    body = _body("evt_r32_single")
    ok = await _post_webhook(api_client, body, _sign(body))
    assert ok.status_code == 200, ok.text
    assert await _stored_ids(api_client, auth_headers) == ["evt_r32_single"]

    bad = await _post_webhook(
        api_client, _body("evt_r32_single_bad"), f"t={NOW},v1=deadbeef"
    )
    assert bad.status_code == 400, bad.text
    assert await _stored_ids(api_client, auth_headers) == ["evt_r32_single"]


@pytest.mark.asyncio
async def test_rotated_delivery_also_verifies_with_the_sdk_present(
    api_client, webhook_secret, auth_headers
):
    """The default path in this venv (`stripe.Webhook.construct_event`, 11.3.0) accepts a
    match in ANY v1 — the behavior the fallback now mirrors for parity."""
    body = _body("evt_r32_sdk_rotated")
    resp = await _post_webhook(api_client, body, _rotated_header(body, "valid_then_garbage"))
    assert resp.status_code == 200, resp.text
    assert await _stored_ids(api_client, auth_headers) == ["evt_r32_sdk_rotated"]


# ── 2. the daily-report read path on a DB fault ──────────────────────────────────


async def _drop(db, table: str) -> None:
    await db.execute(text(f"DROP TABLE {table}"))
    await db.commit()


@pytest.mark.asyncio
async def test_daily_report_route_refuses_with_a_named_db_error(
    api_client, auth_headers, async_db_session
):
    """Before R32-fix: a bare 500 with no code (`raw/probe_report_db_failure_before.txt`)."""
    await _drop(async_db_session, "daily_reports")
    resp = await api_client.get(
        "/api/v1/dashboard/reports/daily?report_date=2026-01-02", headers=auth_headers
    )
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("X-Polsia-Error-Code") == E_DB
    body = resp.json()
    assert body.get("ok") is False, body
    error = body["error"]
    assert error["code"] == E_DB
    assert error["message"].startswith("could not read daily report 2026-01-02:")
    assert "OperationalError" in error["message"], "the driver failure is named, not hidden"
    assert error["how_to_fix"] == ERROR_HINTS[E_DB], "the fix hint must reach the wire"
    assert error["context"] == {"query": "daily report 2026-01-02"}


@pytest.mark.asyncio
async def test_report_service_reads_refuse_by_name_on_a_db_fault(async_db_session):
    """Both raw-execute sites in `report_service` (the file had exactly two) translate their
    driver failure through `query_helpers`, chained, with the same `db_refusal` envelope."""
    from app.services.report_service import get_daily_report, get_latest_daily_report

    await _drop(async_db_session, "daily_reports")

    with pytest.raises(PolsiaError) as excinfo:
        await get_daily_report(async_db_session, date(2026, 1, 2))
    assert excinfo.value.code == E_DB
    assert excinfo.value.context == {"query": "daily report 2026-01-02"}
    assert isinstance(excinfo.value.__cause__, SQLAlchemyError)

    await async_db_session.rollback()
    with pytest.raises(PolsiaError) as excinfo:
        await get_latest_daily_report(async_db_session)
    assert excinfo.value.code == E_DB
    assert excinfo.value.context == {"query": "latest daily report"}
    assert isinstance(excinfo.value.__cause__, SQLAlchemyError)


@pytest.mark.asyncio
async def test_report_service_reads_still_return_rows_and_none(async_db_session):
    """The fix must not change the happy paths: `None` stays `None`, rows come back whole."""
    from app.services.report_service import (
        get_daily_report,
        get_latest_daily_report,
        save_morning_plan,
    )

    day = date(2026, 1, 2)
    other = day + timedelta(days=1)
    assert await get_daily_report(async_db_session, day) is None
    assert await get_latest_daily_report(async_db_session) is None

    await save_morning_plan(async_db_session, day, "R32 plan", tasks_planned=2)
    await save_morning_plan(async_db_session, other, "R32 newer plan", tasks_planned=1)
    await async_db_session.commit()

    found = await get_daily_report(async_db_session, day)
    assert found is not None and found.morning_plan == "R32 plan" and found.tasks_planned == 2
    latest = await get_latest_daily_report(async_db_session)
    assert latest is not None and latest.report_date == other
