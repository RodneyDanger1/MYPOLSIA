"""R28 — route-level edges for the finance webhook / tasks / dashboard surfaces.

What is pinned here that nothing pinned before (reals were read first — `test_finance.py`,
`test_finance_ai_spend.py`, `test_tasks.py`, `test_dashboard.py` are not duplicated):

* the whole webhook ladder through the REAL route, in BOTH production configurations: with the
  `stripe` SDK (present in this venv, 11.3.0) and with the documented absent-SDK fallback
  (`sys.modules["stripe"] = None` makes `import stripe` raise ImportError, exactly what a host
  without the SDK does) — every malformed class, an expired/future timestamp, a valid signature
  over a swapped body, non-JSON/non-UTF8 bodies, non-object JSON, an empty body, a missing id;
* duplicate deliveries (one row; first-write-wins is the documented ON CONFLICT DO NOTHING);
* the webhook's 500-class crashes on signature-valid payloads that do not match Stripe's shape
  (non-object `data.object`, NaN/Infinity/flag amounts, an expanded `customer` object, a non-text
  `currency`) — measured 500s before the R28 fixes (`raw/probe_webhook_shapes.txt`);
* `E_DB` refusals with the hint reachable, at the route boundary, for finance / dashboard / tasks;
* the tasks refusal envelopes (auth 401, unknown agent_type 422, unknown id 404, paging bounds);
* the dashboard rollup parse, day-window exclusion, company KPI/timezone-mismatch warning,
  last_report_date, daily-report-by-date, activity cursor, and the row-None fault.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.errors import (
    E_AGENT_UNKNOWN,
    E_DB,
    E_NOT_FOUND,
    E_VALIDATION,
    ERROR_HINTS,
)
from app.core.timeutil import day_window

SECRET = "whsec_r28_edges"
NOW = int(time.time())


def _sign(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> str:
    stamp = NOW if ts is None else ts
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


def _body(**payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _envelope(resp) -> dict:
    """The standard refusal envelope; asserts it is NOT the bare {"detail": ...} shape."""
    body = resp.json()
    assert body.get("ok") is False, body
    assert isinstance(body.get("error"), dict), body
    return body["error"]


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    return SECRET


@pytest.fixture
def sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented no-SDK production state: `import stripe` raises ImportError."""
    monkeypatch.setitem(sys.modules, "stripe", None)


async def _post_webhook(client, body: bytes, header: str | None = None):
    # No X-API-Key: the route is unauthenticated by design (Stripe cannot send one).
    headers = {} if header is None else {"stripe-signature": header}
    return await client.post("/api/v1/finance/stripe/webhook", content=body, headers=headers)


async def _events_by_id(client, auth_headers) -> dict[str, dict]:
    resp = await client.get("/api/v1/finance/events?limit=500", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return {row["stripe_event_id"]: row for row in resp.json()}


# ── 1. the webhook ladder (route level) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_rejects_a_missing_signature_header(api_client, webhook_secret, auth_headers):
    resp = await _post_webhook(api_client, _body(id="evt_r28_nohdr"))
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Missing stripe-signature header"}
    assert await _events_by_id(api_client, auth_headers) == {}, "nothing may be stored"


@pytest.mark.parametrize(
    ("header", "needle"),
    [
        pytest.param("v1=abc", "Invalid Stripe signature header", id="no-timestamp"),
        pytest.param("t=12345", "Invalid Stripe signature header", id="no-signature"),
        pytest.param("t=notanint,v1=abc", "Invalid Stripe signature timestamp", id="bad-timestamp"),
        pytest.param(f"t={NOW},v1=deadbeef", "Invalid Stripe signature", id="wrong-hex"),
    ],
)
@pytest.mark.asyncio
async def test_webhook_malformed_headers_without_the_sdk(
    api_client, webhook_secret, sdk_absent, auth_headers, header, needle
):
    body = _body(id="evt_r28_malformed", type="probe")
    resp = await _post_webhook(api_client, body, header)
    assert resp.status_code == 400, resp.text
    assert needle in resp.json()["detail"], resp.json()
    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
async def test_webhook_rejects_stale_future_and_wrong_body_without_the_sdk(
    api_client, webhook_secret, sdk_absent, auth_headers
):
    body = _body(id="evt_r28_aged", type="probe")
    stale = await _post_webhook(api_client, body, _sign(body, ts=NOW - 3600))
    assert stale.status_code == 400, stale.text
    assert "outside the 300s tolerance" in stale.json()["detail"]

    future = await _post_webhook(api_client, body, _sign(body, ts=NOW + 3600))
    assert future.status_code == 400, future.text
    assert "outside the 300s tolerance" in future.json()["detail"]

    swapped = await _post_webhook(api_client, body, _sign(_body(id="evt_other", type="x")))
    assert swapped.status_code == 400, swapped.text
    assert "Invalid Stripe signature" in swapped.json()["detail"]

    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
async def test_webhook_malformed_deliveries_are_refused_with_the_sdk_present(
    api_client, webhook_secret, auth_headers
):
    """The default path in this venv: `stripe.Webhook.construct_event` is the verifier
    (finance.py:179-185) and any failure is the same named 400."""
    body = _body(id="evt_r28_sdk_bad", type="probe")
    garbage = await _post_webhook(api_client, body, f"t={NOW},v1=badsig")
    assert garbage.status_code == 400, garbage.text
    assert str(garbage.json()["detail"]).startswith("Invalid Stripe signature")

    stale = await _post_webhook(api_client, body, _sign(body, ts=NOW - 3600))
    assert stale.status_code == 400, stale.text
    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
async def test_webhook_accepts_a_sdk_verified_delivery_and_stores_it(
    api_client, auth_headers, webhook_secret
):
    body = _body(
        id="evt_r28_sdk_ok",
        type="charge.succeeded",
        data={"object": {"amount": 500, "currency": "USD", "customer": "cus_r28"}},
    )
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}

    row = (await _events_by_id(api_client, auth_headers))["evt_r28_sdk_ok"]
    assert row["event_type"] == "charge.succeeded"
    assert row["amount_cents"] == 500
    assert row["currency"] == "usd", "the column stores the lower-cased code"
    assert row["customer_id"] == "cus_r28"
    assert row["status"] == "processed"


@pytest.mark.asyncio
async def test_webhook_accepts_the_stdlib_signed_delivery_without_the_sdk(
    api_client, auth_headers, webhook_secret, sdk_absent
):
    """The whole ladder end-to-end on the documented fallback (finance.py:177-178)."""
    body = _body(id="evt_r28_fallback_ok", type="payment_intent.succeeded",
                 data={"object": {"amount_paid": 1250, "currency": "eur"}})
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 200, resp.text
    row = (await _events_by_id(api_client, auth_headers))["evt_r28_fallback_ok"]
    assert row["amount_cents"] == 1250
    assert row["currency"] == "eur"


@pytest.mark.asyncio
async def test_webhook_refuses_a_signed_non_json_body(api_client, webhook_secret, sdk_absent,
                                                      auth_headers):
    body = b"not json at all"
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Invalid JSON body"}
    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
async def test_webhook_refuses_signed_json_that_is_not_an_object(api_client, webhook_secret,
                                                                 sdk_absent, auth_headers):
    body = b"[1, 2, 3]"
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Invalid event payload"}
    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
async def test_webhook_refuses_an_empty_signed_body(api_client, webhook_secret, sdk_absent,
                                                    auth_headers):
    """An empty body parses to `{}` and is then refused for the honest reason: no id."""
    resp = await _post_webhook(api_client, b"", _sign(b""))
    assert resp.status_code == 400, resp.text
    error = _envelope(resp)
    assert error["code"] == E_VALIDATION
    assert "no id" in error["message"]
    assert await _events_by_id(api_client, auth_headers) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "event_type"),
    [(None, "charge.succeeded"), ("   ", "charge.succeeded")],
)
async def test_webhook_refuses_an_event_without_an_id_and_names_the_fix(
    api_client, webhook_secret, event_id, event_type
):
    """`id` missing/blank -> 400 E_VALIDATION envelope (never an empty-id row, which would
    double-count every replay)."""
    payload: dict = {"data": {"object": {"amount": 1}}}
    if event_id is not None:
        payload["id"] = event_id
    if event_type is not None:
        payload["type"] = event_type
    body = _body(**payload)
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 400, resp.text
    error = _envelope(resp)
    assert error["code"] == E_VALIDATION
    assert "no id" in error["message"]
    assert error["how_to_fix"] == ERROR_HINTS[E_VALIDATION]
    assert error["context"] == {"event_type": event_type}, "the context names what was seen"


@pytest.mark.asyncio
async def test_webhook_defaults_the_event_type_to_unknown(api_client, auth_headers, webhook_secret):
    body = _body(id="evt_r28_notype")
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 200, resp.text
    assert (await _events_by_id(api_client, auth_headers))["evt_r28_notype"]["event_type"] == "unknown"


@pytest.mark.asyncio
async def test_webhook_duplicate_delivery_is_idempotent(api_client, auth_headers, webhook_secret):
    """Stripe re-delivers: ONE row, both deliveries answered 200 (a 5xx here would trigger
    Stripe's retry storm, which is what the ON CONFLICT statement exists to prevent)."""
    first = _body(id="evt_r28_dup", type="charge.succeeded",
                  data={"object": {"amount": 700, "currency": "usd"}})
    for _ in range(2):
        resp = await _post_webhook(api_client, first, _sign(first))
        assert resp.status_code == 200, resp.text

    again = _body(id="evt_r28_dup", type="charge.refunded",
                  data={"object": {"amount": 999, "currency": "usd"}})
    resp = await _post_webhook(api_client, again, _sign(again))
    assert resp.status_code == 200, resp.text

    rows = await _events_by_id(api_client, auth_headers)
    assert list(rows) == ["evt_r28_dup"], "one row, no duplicate and no empty-id twin"
    # First-write-wins, pinned deliberately: `record_stripe_event` is
    # `ON CONFLICT DO NOTHING RETURNING id` and returns the EXISTING row (finance_service.py:521-563).
    assert rows["evt_r28_dup"]["amount_cents"] == 700
    assert rows["evt_r28_dup"]["event_type"] == "charge.succeeded"


@pytest.mark.asyncio
async def test_webhook_tolerance_setting_is_live(api_client, webhook_secret, monkeypatch):
    """`stripe_signature_tolerance` is honoured at the route boundary (finance.py:188-189, :208):
    a delivery accepted at the 300 s default is refused once the window is 60 s."""
    from app.config import settings

    body = _body(id="evt_r28_tol", type="probe", data={"object": {"amount": 1}})
    header = _sign(body, ts=NOW - 120)

    accepted = await _post_webhook(api_client, body, header)
    assert accepted.status_code == 200, accepted.text

    # A pydantic Settings model refuses setattr for an undeclared field; stage the configured
    # value where a declared field's value lives (the instance dict) — the getattr hook is what
    # the route must honour (finance.py:188-189, :208).
    monkeypatch.setitem(settings.__dict__, "stripe_signature_tolerance", 60)
    refused = await _post_webhook(api_client, body, header)
    assert refused.status_code == 400, refused.text


# ── 2. signature-valid payloads that do not match Stripe's shape (R28 fixes) ─────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data_object",
    [
        pytest.param("not-an-object", id="string"),
        pytest.param([1, 2], id="list"),
        pytest.param(7, id="number"),
    ],
)
async def test_webhook_non_object_data_object_is_not_a_crash(
    api_client, auth_headers, webhook_secret, data_object
):
    """Before R28 this raised AttributeError -> 500 -> Stripe retries the same poison payload
    forever (measured: raw/probe_webhook_shapes.txt).  Now the event is stored with no readable
    fields (NULLs = "not measured"), which is the honest reading."""
    body = _body(id=f"evt_r28_dobj_{type(data_object).__name__}", type="probe",
                 data={"object": data_object})
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 200, resp.text
    row = (await _events_by_id(api_client, auth_headers))[
        f"evt_r28_dobj_{type(data_object).__name__}"
    ]
    assert row["amount_cents"] is None
    assert row["customer_id"] is None
    assert row["currency"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "raw"),
    [
        pytest.param("nan", b'{"id": "evt_r28_nan", "type": "probe",'
                           b' "data": {"object": {"amount": NaN}}}', id="nan"),
        pytest.param("inf", b'{"id": "evt_r28_inf", "type": "probe",'
                           b' "data": {"object": {"amount": 1e400}}}', id="infinity"),
        pytest.param("bool", json.dumps({"id": "evt_r28_bool", "type": "probe",
                                         "data": {"object": {"amount": True}}}).encode(),
                     id="flag"),
        pytest.param("str", json.dumps({"id": "evt_r28_str", "type": "probe",
                                        "data": {"object": {"amount": "250"}}}).encode(),
                     id="string"),
    ],
)
async def test_webhook_amounts_that_are_not_measurements_stay_null(
    api_client, auth_headers, webhook_secret, name, raw
):
    """`int(NaN)`/`int(inf)` 500'd the delivery; `True` was stored as a one-cent measurement.
    None of the three is a measured amount (accounting.py:117: "a flag is not a count")."""
    resp = await _post_webhook(api_client, raw, _sign(raw))
    assert resp.status_code == 200, resp.text
    row = (await _events_by_id(api_client, auth_headers))[f"evt_r28_{name}"]
    assert row["amount_cents"] is None


@pytest.mark.asyncio
async def test_webhook_expanded_customer_object_stores_its_id(
    api_client, auth_headers, webhook_secret
):
    """Stripe types `customer` as `string | object (expandable)`; a dict bound to the TEXT column
    500'd the delivery (raw/probe_webhook_shapes.txt).  The id is recovered; anything that is
    neither text nor an object-with-id is honestly NULL."""
    body = _body(id="evt_r28_cus_exp", type="payment_intent.succeeded",
                 data={"object": {"amount": 100,
                                  "customer": {"id": "cus_r28_expanded", "object": "customer"}}})
    assert (await _post_webhook(api_client, body, _sign(body))).status_code == 200

    body2 = _body(id="evt_r28_cus_num", type="probe", data={"object": {"customer": 12345}})
    assert (await _post_webhook(api_client, body2, _sign(body2))).status_code == 200

    body3 = _body(id="evt_r28_cus_noid", type="probe",
                  data={"object": {"customer": {"object": "customer"}}})
    assert (await _post_webhook(api_client, body3, _sign(body3))).status_code == 200

    rows = await _events_by_id(api_client, auth_headers)
    assert rows["evt_r28_cus_exp"]["customer_id"] == "cus_r28_expanded"
    assert rows["evt_r28_cus_num"]["customer_id"] is None
    assert rows["evt_r28_cus_noid"]["customer_id"] is None


@pytest.mark.asyncio
async def test_webhook_non_text_currency_is_not_bound(api_client, auth_headers, webhook_secret):
    """A numeric currency reached `record_stripe_event`'s `.lower()` and 500'd; the boundary now
    passes text or nothing."""
    body = _body(id="evt_r28_cur_num", type="probe", data={"object": {"currency": 123}})
    resp = await _post_webhook(api_client, body, _sign(body))
    assert resp.status_code == 200, resp.text
    assert (await _events_by_id(api_client, auth_headers))["evt_r28_cur_num"]["currency"] is None


# ── 3. the DB-failure refusals (E_DB) and the hint reachability ──────────────────


async def _drop(db, table: str) -> None:
    await db.execute(text(f"DROP TABLE {table}"))
    await db.commit()


@pytest.mark.asyncio
async def test_finance_summary_refuses_with_a_named_db_error(api_client, auth_headers,
                                                            async_db_session):
    await _drop(async_db_session, "revenue_snapshots")
    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("X-Polsia-Error-Code") == E_DB
    error = _envelope(resp)
    assert error["code"] == E_DB
    assert "could not read finance summary" in error["message"]
    assert error["how_to_fix"] == ERROR_HINTS[E_DB], "the fix hint must reach the wire"
    assert error["context"] == {"query": "finance summary"}


@pytest.mark.asyncio
async def test_dashboard_summary_refuses_with_a_named_db_error(api_client, auth_headers,
                                                              async_db_session):
    await _drop(async_db_session, "tasks")
    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("X-Polsia-Error-Code") == E_DB
    error = _envelope(resp)
    assert error["code"] == E_DB
    assert "could not read dashboard summary" in error["message"]
    assert error["how_to_fix"] == ERROR_HINTS[E_DB]


@pytest.mark.asyncio
async def test_tasks_routes_refuse_with_a_named_db_error(api_client, auth_headers,
                                                        async_db_session):
    """Before R28-fix both answers were a bare 500 with no code (measured:
    raw/probe_tasks_db_failure.txt) while every other read surface refuses by name."""
    await _drop(async_db_session, "tasks")

    listed = await api_client.get("/api/v1/tasks", headers=auth_headers)
    assert listed.status_code == 503, listed.text
    error = _envelope(listed)
    assert error["code"] == E_DB
    assert "could not read tasks" in error["message"]
    assert error["how_to_fix"] == ERROR_HINTS[E_DB]
    assert error["context"] == {"query": "tasks"}

    single = await api_client.get("/api/v1/tasks/7", headers=auth_headers)
    assert single.status_code == 503, single.text
    error = _envelope(single)
    assert error["code"] == E_DB
    assert "could not read task 7" in error["message"]


# ── 4. the tasks envelopes ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_accepts_every_documented_field(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "title": "R28 full payload",
            "agent_type": "competitor_research",
            "description": "sweep the finance/tasks/query surfaces",
            "priority": 1,
            "source": "r28-lane",
            "scheduled_date": "2026-10-13T06:00:00",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["title"] == "R28 full payload"
    assert data["description"] == "sweep the finance/tasks/query surfaces"
    assert data["priority"] == 1
    assert data["source"] == "r28-lane"
    assert data["scheduled_date"].startswith("2026-10-13T06:00:00")
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_task_unknown_agent_type_is_a_named_422(api_client, auth_headers):
    resp = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "bad agent", "agent_type": "definitely_not_an_agent"},
    )
    assert resp.status_code == 422, resp.text
    error = _envelope(resp)
    assert error["code"] == E_AGENT_UNKNOWN
    assert "definitely_not_an_agent" in error["message"]
    assert "VALID_AGENT_TYPES" in error["how_to_fix"]
    assert error["context"] == {"agent_type": "definitely_not_an_agent"}


@pytest.mark.asyncio
async def test_get_task_unknown_id_is_a_named_404(api_client, auth_headers):
    resp = await api_client.get("/api/v1/tasks/987654", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    error = _envelope(resp)
    assert error["code"] == E_NOT_FOUND
    assert "987654" in error["message"]
    assert error["how_to_fix"] == ERROR_HINTS[E_NOT_FOUND]
    assert error["context"] == {"task_id": 987654}


@pytest.mark.asyncio
async def test_task_list_filters_and_paging_bounds(api_client, auth_headers, async_db_session):
    from app.models.task import Task

    async_db_session.add_all(
        [
            Task(title="r28-done", agent_type="finance", status="completed"),
            Task(title="r28-pending", agent_type="finance", status="pending"),
            Task(title="r28-social", agent_type="social_media", status="pending"),
        ]
    )
    await async_db_session.commit()

    done = await api_client.get("/api/v1/tasks?status=completed", headers=auth_headers)
    assert done.status_code == 200
    assert [row["title"] for row in done.json()] == ["r28-done"]

    finance = await api_client.get("/api/v1/tasks?agent_type=finance", headers=auth_headers)
    assert {row["title"] for row in finance.json()} == {"r28-done", "r28-pending"}

    page = await api_client.get("/api/v1/tasks?limit=2", headers=auth_headers)
    assert page.status_code == 200
    assert [row["id"] for row in page.json()] == [3, 2], "newest first (created_at DESC, id DESC)"

    offset = await api_client.get("/api/v1/tasks?limit=2&offset=2", headers=auth_headers)
    assert [row["id"] for row in offset.json()] == [1]

    for query in ("limit=0", "limit=501", "offset=-1"):
        bad = await api_client.get(f"/api/v1/tasks?{query}", headers=auth_headers)
        assert bad.status_code == 422, (query, bad.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/tasks"),
        ("post", "/api/v1/tasks"),
        ("get", "/api/v1/tasks/1"),
        ("get", "/api/v1/dashboard/summary"),
        ("get", "/api/v1/finance/summary"),
    ],
)
async def test_lane_routes_require_the_api_key(api_client, method, path):
    client_call = getattr(api_client, method)
    resp = await (client_call(path) if method == "get" else client_call(path, json={}))
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("WWW-Authenticate") == "X-API-Key"


# ── 5. the dashboard edges ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_summary_counts_the_local_day_and_parses_the_rollup(
    api_client, auth_headers, async_db_session
):
    from app.models.task import Task

    start, _end = day_window(async_db_session)
    inside = start + timedelta(hours=1)
    async_db_session.add_all(
        [
            Task(title="r28-c", agent_type="finance", status="completed", created_at=inside),
            Task(title="r28-p", agent_type="social_media", status="pending", created_at=inside),
            Task(title="r28-f", agent_type="finance", status="failed", created_at=inside),
            # One minute before the company-local midnight: outside "today" by contract.
            Task(title="r28-old", agent_type="finance", status="pending",
                 created_at=start - timedelta(minutes=1)),
        ]
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tasks_today_total"] == 3
    assert data["tasks_today_completed"] == 1
    assert data["tasks_today_pending"] == 1
    assert data["tasks_today_failed"] == 1
    assert data["active_agents"] == ["finance", "social_media"], "sorted + deduped rollup parse"
    assert data["kpis"] == {}
    assert data["last_report_date"] is None


@pytest.mark.asyncio
async def test_dashboard_summary_reports_company_kpis_and_warns_on_tz_mismatch(
    api_client, auth_headers, async_db_session, caplog, monkeypatch
):
    from app.config import settings
    from app.models.company import CompanyConfig

    monkeypatch.setattr(settings, "company_timezone", "UTC")
    async_db_session.add(
        CompanyConfig(name="R28 Co", kpis={"mrr_usd": 5000}, timezone="Asia/Tokyo",
                      daily_cycle_hour=6)
    )
    await async_db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.api.v1.dashboard"):
        resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kpis"] == {"mrr_usd": 5000}

    messages = [record.getMessage() for record in caplog.records]
    assert any("Asia/Tokyo" in message and "COMPANY_TIMEZONE=UTC" in message for message in messages), (
        "the window is not silently re-computed; the mismatch is logged with both values",
        messages,
    )


@pytest.mark.asyncio
async def test_dashboard_summary_reports_the_last_report_date(api_client, auth_headers,
                                                             async_db_session):
    from app.models.report import DailyReport

    async_db_session.add(DailyReport(report_date=date(2026, 1, 2)))
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_report_date"] == "2026-01-02"


@pytest.mark.asyncio
async def test_dashboard_daily_report_by_date_and_a_missing_day(api_client, auth_headers,
                                                               async_db_session):
    from app.models.report import DailyReport

    async_db_session.add(DailyReport(report_date=date(2026, 1, 2), evening_summary="r28 day"))
    await async_db_session.commit()

    found = await api_client.get("/api/v1/dashboard/reports/daily?report_date=2026-01-02",
                                 headers=auth_headers)
    assert found.status_code == 200, found.text
    assert found.json()["report_date"] == "2026-01-02"
    assert found.json()["evening_summary"] == "r28 day"

    absent = await api_client.get("/api/v1/dashboard/reports/daily?report_date=2026-02-02",
                                  headers=auth_headers)
    assert absent.status_code == 200 and absent.json() is None, "null, not 404"

    invalid = await api_client.get("/api/v1/dashboard/reports/daily?report_date=not-a-date",
                                   headers=auth_headers)
    assert invalid.status_code == 422, invalid.text


@pytest.mark.asyncio
async def test_dashboard_activity_cursor_paginates(api_client, auth_headers, async_db_session):
    from app.models.report import ActivityLog

    base = datetime(2026, 1, 1, 12, 0)
    async_db_session.add_all(
        [
            ActivityLog(agent_type="finance", action="r28", summary=f"entry {i}",
                        created_at=base + timedelta(minutes=i))
            for i in range(3)
        ]
    )
    await async_db_session.commit()

    first = await api_client.get("/api/v1/dashboard/activity?limit=2", headers=auth_headers)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [row["summary"] for row in page] == ["entry 2", "entry 1"], "newest first"

    second = await api_client.get(
        f"/api/v1/dashboard/activity?limit=2&before={page[-1]['id']}", headers=auth_headers
    )
    assert [row["summary"] for row in second.json()] == ["entry 0"]

    bad = await api_client.get("/api/v1/dashboard/activity?limit=0", headers=auth_headers)
    assert bad.status_code == 422, bad.text


@pytest.mark.asyncio
async def test_dashboard_summary_row_none_is_a_fault_not_a_default(
    api_client, auth_headers, monkeypatch
):
    """The aggregate always yields exactly one row, so `None` here is a real fault; the route must
    refuse, never answer a plausible all-zero dashboard.  (The double replaces only the statement
    read; a test-only seam, no production path is mocked.)

    DELIBERATE RE-PIN (V20/L1, card L2-09): this test used to expect a bare `RuntimeError` — i.e.
    an UNCODED 500 on the wire, which is exactly the defect the card names.  The route now answers
    the sibling `E_DB` refusal (`run_service.count_runs`'s pattern): 503, the named envelope and
    the machine-readable code header.  The invariant this test exists for — "a fault is never an
    all-zero dashboard" — is unchanged and asserted harder (no body fields at all).
    """

    async def _no_row(db, stmt, *, what):
        return None

    monkeypatch.setattr("app.api.v1.dashboard.fetch_one", _no_row)
    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == E_DB
    assert resp.headers.get("X-Polsia-Error-Code") == E_DB
    assert "dashboard summary statement returned no row" in body["error"]["message"]
    assert "tasks_today_total" not in body, "a fault must never look like an all-zero dashboard"
