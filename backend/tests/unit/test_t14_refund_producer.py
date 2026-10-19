"""T14 / D21 — the refund producer: the `pi_…` id is extracted, persisted and RECEIVED.

The audited gap (fix-5 §8 / fix-3 §1, L3-10): the webhook persisted each Stripe delivery and the
refundable `PaymentIntent` id sat inside `raw_payload` — but *no code path read it into the
finance agent's context*, so the model could only ever emit an invented id and the adapter's
fail-closed `pi_`-only guard was the only thing the refund path could reach.  This lane closes
both halves:

  * PRODUCER — `stripe_webhook` extracts the id from the delivery's SHAPE (charge/invoice carry
    `payment_intent`; a `payment_intent` object IS one) via `finance_service.
    extract_payment_intent_id` and persists it on the event row as
    `raw_payload["_polsia"]["payment_intent_id"]` (a measured `null` for a shape that carries
    none — never `""`, never a substitute id);
  * READ — `finance_service.recent_failed_payments` turns those rows into records;
  * DELIVERY — the morning cycle attaches them to the finance task's `metadata.failed_payments`,
    and the finance agent renders them into the VOLATILE `extra_context` layer (the RD12
    `inbox_item_extra_context` pattern), so the run RECEIVES the id.

Every leg below drives the REAL route / service / agent (the mock CLI is the sanctioned agent
seam) with CONSTRUCTED SIGNED Stripe events, reusing the R28 stdlib-HMAC signing pattern
(`tests/unit/api/test_finance.py::_stripe_signature`).

RED leg (measured, receipt §RED): drop `payment_intent_id = extract_payment_intent_id(event)`
from the webhook -> the persisted annotation is `null` for every shape and the reader/prompt legs
fail.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.errors import E_INTEGRATION, E_VALIDATION, PolsiaError

SECRET = "whsec_t14_producer"
NOW = int(time.time())

PI_FROM_CHARGE = "pi_3MtwBwLkdIwHu7ix28a3tqPa"
PI_FROM_INVOICE = "pi_3MtwBwLkdIwHu7ix28a3tqPb"
PI_FROM_INTENT = "pi_3MtwBwLkdIwHu7ix28a3tqPc"

CHARGE_SHAPE = {
    "id": "ch_3MmlLrLkdIwHu7ix0snN0B15",
    "object": "charge",
    "amount": 1500,
    "currency": "usd",
    "customer": "cus_t14",
    "payment_intent": PI_FROM_CHARGE,
    "status": "failed",
}
INVOICE_SHAPE = {
    "id": "in_1MtwBwLkdIwHu7ix28a3tqPa",
    "object": "invoice",
    "amount_due": 2500,
    "currency": "usd",
    "customer": "cus_t14",
    "payment_intent": PI_FROM_INVOICE,
}
INTENT_SHAPE = {
    "id": PI_FROM_INTENT,
    "object": "payment_intent",
    "amount": 900,
    "currency": "usd",
    "customer": "cus_t14",
    "status": "requires_payment_method",
}
NO_INTENT_SHAPE = {
    "id": "ch_3MmlLrLkdIwHu7ix0snN0B99",
    "object": "charge",
    "amount": 700,
    "currency": "usd",
    "payment_intent": None,          # Stripe's own docs: "if one exists"
    "status": "failed",
}


def _sign(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> str:
    # Stamp at CALL time, never the module-import constant: pytest imports every test module at
    # collection and this test runs minutes later inside a full suite — a module-level NOW aged
    # past Stripe's 300 s tolerance and the app answered 400 "Timestamp outside the tolerance
    # zone" (the V15->V19 flake: standalone green, long gates red; verified 2026-10-19 02:38Z).
    stamp = int(time.time()) if ts is None else ts
    digest = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={stamp},v1={digest}"


def _event(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps({
        "id": event_id, "type": event_type, "data": {"object": obj},
    }).encode("utf-8")


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    return SECRET


async def _post(client, body: bytes):
    return await client.post(
        "/api/v1/finance/stripe/webhook",
        content=body,
        headers={"stripe-signature": _sign(body)},
    )


# ── 1. the shape-driven extraction ─────────────────────────────────────────────────────────────


def test_every_shape_answers_honestly():
    from app.services.finance_service import extract_payment_intent_id

    assert extract_payment_intent_id({"data": {"object": CHARGE_SHAPE}}) == PI_FROM_CHARGE
    assert extract_payment_intent_id({"data": {"object": INVOICE_SHAPE}}) == PI_FROM_INVOICE
    assert extract_payment_intent_id({"data": {"object": INTENT_SHAPE}}) == PI_FROM_INTENT
    # a shape that carries none is None — never the charge id, never the event id, never ""
    assert extract_payment_intent_id({"data": {"object": NO_INTENT_SHAPE}}) is None
    # defensive edges: none of these may raise or invent an id
    assert extract_payment_intent_id({}) is None
    assert extract_payment_intent_id({"data": None}) is None
    assert extract_payment_intent_id({"data": {"object": "not-an-object"}}) is None
    assert extract_payment_intent_id({"data": {"object": {"payment_intent": 123}}}) is None
    assert extract_payment_intent_id({"data": {"object": {"payment_intent": "   "}}}) is None
    assert extract_payment_intent_id({"data": {"object": {"object": "payment_intent"}}}) is None
    assert extract_payment_intent_id(None) is None
    # an unrelated object whose own id is NOT a payment intent is not promoted
    assert extract_payment_intent_id({"data": {"object": {"id": "ch_x", "object": "charge"}}}) is None


# ── 2. the producer: signed deliveries persist the extracted id ────────────────────────────────


@pytest.mark.asyncio
async def test_signed_deliveries_persist_the_payment_intent_id(
    api_client, webhook_secret, async_db_session
):
    from app.models.finance import StripeEvent

    deliveries = [
        ("evt_t14_charge", "charge.failed", CHARGE_SHAPE, PI_FROM_CHARGE),
        ("evt_t14_invoice", "invoice.payment_failed", INVOICE_SHAPE, PI_FROM_INVOICE),
        ("evt_t14_intent", "payment_intent.payment_failed", INTENT_SHAPE, PI_FROM_INTENT),
        ("evt_t14_none", "charge.failed", NO_INTENT_SHAPE, None),
    ]
    for event_id, event_type, obj, _expected in deliveries:
        resp = await _post(api_client, _event(event_id, event_type, obj))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"received": True}

    rows = {
        row.stripe_event_id: row
        for row in (await async_db_session.execute(select(StripeEvent))).scalars()
    }
    assert set(rows) == {event_id for event_id, *_ in deliveries}
    for event_id, _type, _obj, expected in deliveries:
        payload = rows[event_id].raw_payload or {}
        assert payload["id"] == event_id, "the verbatim event is stored"
        annotation = payload.get("_polsia") or {}
        if expected is None:
            assert "payment_intent_id" not in annotation, (
                "a shape with no PaymentIntent records NO extracted id — the verbatim event "
                "(payment_intent: null) is the honest record, never a fabricated or empty id"
            )
        else:
            assert annotation.get("payment_intent_id") == expected, event_id

    # …and the READ path answers honestly for every one of them: the three that carry an id
    # return it, the shape that carries none returns None (never an id from another field).
    from app.services.finance_service import recent_failed_payments

    records = {record["event_id"]: record for record in await recent_failed_payments(async_db_session)}
    assert records["evt_t14_charge"]["payment_intent_id"] == PI_FROM_CHARGE
    assert records["evt_t14_invoice"]["payment_intent_id"] == PI_FROM_INVOICE
    assert records["evt_t14_intent"]["payment_intent_id"] == PI_FROM_INTENT
    assert records["evt_t14_none"]["payment_intent_id"] is None


# ── 3. the reader: failed-payment records carrying the id ─────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_failed_payments_returns_records_with_the_ids(async_db_session):
    from app.services.finance_service import recent_failed_payments, record_stripe_event

    await record_stripe_event(
        async_db_session, stripe_event_id="evt_t14_r1", event_type="charge.failed",
        raw_payload={"id": "evt_t14_r1", "data": {"object": CHARGE_SHAPE}},
        customer_id="cus_t14", amount_cents=1500, currency="usd",
        payment_intent_id=PI_FROM_CHARGE,
    )
    # a row written WITHOUT the annotation (raw-SQL/ops, pre-T14 writer): the verbatim event is
    # re-read through the same extractor — the id is durably there (fix-5 §8)
    await record_stripe_event(
        async_db_session, stripe_event_id="evt_t14_r2", event_type="invoice.payment_failed",
        raw_payload={"id": "evt_t14_r2", "data": {"object": INVOICE_SHAPE}},
        amount_cents=2500, currency="usd",
    )
    # a delivery that is NOT a failed payment must not appear
    await record_stripe_event(
        async_db_session, stripe_event_id="evt_t14_ok", event_type="charge.succeeded",
        raw_payload={"id": "evt_t14_ok", "data": {"object": CHARGE_SHAPE}},
        amount_cents=1500, currency="usd", payment_intent_id=PI_FROM_CHARGE,
    )
    await async_db_session.flush()

    records = await recent_failed_payments(async_db_session)
    assert [record["event_id"] for record in records] == ["evt_t14_r2", "evt_t14_r1"], "newest first"
    by_event = {record["event_id"]: record for record in records}
    assert by_event["evt_t14_r1"]["payment_intent_id"] == PI_FROM_CHARGE
    assert by_event["evt_t14_r2"]["payment_intent_id"] == PI_FROM_INVOICE, "fallback extraction"
    assert by_event["evt_t14_r1"]["customer_id"] == "cus_t14"
    assert by_event["evt_t14_r2"]["amount_cents"] == 2500


# ── 4. the delivery: the finance RUN receives the id ──────────────────────────────────────────

RECORDS = [{
    "event_id": "evt_t14_charge",
    "event_type": "charge.failed",
    "payment_intent_id": PI_FROM_CHARGE,
    "amount_cents": 1500,
    "currency": "usd",
    "customer_id": "cus_t14",
    "occurred_at": "2026-10-13T12:00:00+00:00",
}]


def _run_finance(task: dict) -> tuple[dict, dict]:
    """Drive the REAL `FinanceAgent.run()`; capture what it passed and what the model saw."""
    from app.agents.finance.agent import FinanceAgent

    agent = FinanceAgent()
    captured: dict[str, Any] = {}
    real_collect = agent.collect_structured
    real_call = agent.call_claude_structured

    def collect(task_, context_, **kw):
        captured["extra_context"] = kw.get("extra_context")
        payload, assembled = real_collect(task_, context_, **kw)
        captured["assembled"] = assembled
        return payload, assembled

    def call(prompt, **kw):
        captured["prompt"] = prompt
        captured["system"] = kw.get("system_prompt_static", "")
        return real_call(prompt, **kw)

    agent.collect_structured = collect
    agent.call_claude_structured = call
    return agent.run(task, {}), captured


def test_the_finance_run_receives_the_pi_id_in_its_volatile_context(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": json.dumps({"summary": "saw the failed payment"})}),
    )
    task = {"id": 1401, "title": "Daily revenue snapshot",
            "metadata": {"failed_payments": RECORDS}}

    _payload, captured = _run_finance(task)

    assert captured["extra_context"], "the records must reach the prompt's extra_context layer"
    assert PI_FROM_CHARGE in captured["extra_context"]
    # quarantined as DATA: a real untrusted block, in the VOLATILE suffix only
    assembled = captured["assembled"]
    assert '<untrusted source="stripe"' in assembled.volatile_suffix
    assert PI_FROM_CHARGE in captured["prompt"]
    assert PI_FROM_CHARGE not in captured["system"], "per-run material must not enter the prefix"


def test_an_attached_shape_with_no_id_says_so_instead_of_inventing_one(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": json.dumps({"summary": "ok"})})
    )
    records = [dict(RECORDS[0], payment_intent_id=None)]
    task = {"id": 1402, "title": "Daily revenue snapshot",
            "metadata": {"failed_payments": records}}

    _payload, captured = _run_finance(task)
    assert "payment_intent_id=absent" in captured["extra_context"]
    assert PI_FROM_CHARGE not in captured["extra_context"]


def test_no_attachment_means_no_extra_context(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": json.dumps({"summary": "ok"})})
    )
    _payload, captured = _run_finance({"id": 1403, "title": "Daily revenue snapshot"})
    assert captured["extra_context"] is None
    assert "FAILED STRIPE PAYMENTS" not in captured["prompt"]


def test_a_malformed_attachment_is_a_named_refusal_before_any_model_call():
    from app.agents.finance.agent import FinanceAgent, failed_payments_extra_context

    with pytest.raises(PolsiaError) as excinfo:
        failed_payments_extra_context({"metadata": {"failed_payments": "not-a-list"}})
    assert excinfo.value.code == E_VALIDATION
    assert "failed_payments" in excinfo.value.message

    # …and the refusal fires BEFORE the model seam (nothing is called with a broken attachment)
    agent = FinanceAgent()
    called: list = []
    agent.collect_structured = lambda *a, **kw: called.append(kw) or ({}, None)  # type: ignore[func-returns-value]
    with pytest.raises(PolsiaError):
        agent.run({"metadata": {"failed_payments": {"nope": True}}}, {})
    assert called == []


def test_the_persisted_id_clears_the_refund_adapter_guard():
    """The end-to-end point of the id: `refund_payment` accepts it as a refund target.

    With no STRIPE_SECRET_KEY configured the adapter refuses on CREDENTIALS — the honest proof
    that the identifier itself passed the fail-closed `pi_`-only guard (an `evt_…`/invented id
    answers E_VALIDATION here, one layer earlier).
    """
    from app.agents import integrations

    result = integrations.refund_payment(payment_id=PI_FROM_CHARGE, amount_cents=1500)
    assert result["ok"] is False
    assert result["error"]["code"] == E_INTEGRATION, result
    assert "STRIPE_SECRET_KEY" in result["error"]["message"]

    refused = integrations.refund_payment(payment_id="evt_t14_charge", amount_cents=1500)
    assert refused["error"]["code"] == E_VALIDATION


# ── 5. the producer the cycle runs: metadata attach through the real morning cycle ────────────


def test_the_morning_cycle_attaches_the_failed_payments_to_the_finance_task(
    queue_db, monkeypatch
):
    """The delivery seam, end to end: webhook record -> cycle attach -> finance task metadata."""
    from app.config import settings
    from app.services.finance_service import record_stripe_event

    async def _seed() -> None:
        async with queue_db.factory() as db:
            await record_stripe_event(
                db, stripe_event_id="evt_t14_cycle", event_type="charge.failed",
                raw_payload={"id": "evt_t14_cycle", "data": {"object": CHARGE_SHAPE}},
                amount_cents=1500, currency="usd", payment_intent_id=PI_FROM_CHARGE,
            )
            await db.commit()

    queue_db.run(_seed())

    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "morning_cycle_hour", 6)
    monkeypatch.setattr(daily_cycle_module(), "now_utc", lambda: datetime(2026, 10, 13, 6, 30, tzinfo=UTC))
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(
        crew_factory, "run_agent_for_task",
        lambda agent_type, task_dict, context: {"summary": "plan", "tasks": []},
    )

    class _Group:
        def __call__(self, sigs):
            return self

        def apply_async(self) -> None:
            return None

    monkeypatch.setattr("celery.group", _Group())

    result = daily_cycle_module().run_morning_cycle()
    assert result["status"] == "completed", result

    rows = queue_db.query(
        "SELECT agent_type, metadata FROM tasks WHERE agent_type = 'finance'"
    )
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata"]) if rows[0]["metadata"] else {}
    attached = metadata.get("failed_payments")
    assert attached and attached[0]["payment_intent_id"] == PI_FROM_CHARGE
    assert attached[0]["event_id"] == "evt_t14_cycle"
    # the OTHER fixed tasks carry no such attachment (only the finance run consumes them)
    others = queue_db.query(
        "SELECT agent_type, metadata FROM tasks WHERE agent_type != 'finance'"
    )
    assert all(row["metadata"] in (None, "null") for row in others), others


def daily_cycle_module():
    from celery_app.tasks import daily_cycle

    return daily_cycle
