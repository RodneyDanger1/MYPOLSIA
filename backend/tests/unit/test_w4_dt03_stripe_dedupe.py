"""W4 / DT-03 — one Stripe payment is counted ONCE in the 30-day cash block.

THE DEFECT (data-hunter DT-03, P1).  `report_service.build_cash_block` summed every row whose
`event_type` is money-in.  One card payment arrives as up to four deliveries — `charge.succeeded`,
`payment_intent.succeeded`, `invoice.paid`, `invoice.payment_succeeded` — so a single $100.00
payment could read as $400.00 of money received.  The webhook already persists the PaymentIntent id
on each event row (`raw_payload["_polsia"]["payment_intent_id"]`); the report never used it.

THE FIX under test: the window's events are read as rows and grouped by that id before summing
(`report_service._payment_groups`); an event with NO PaymentIntent cannot be proven to repeat
another, so it is its own group and is counted exactly once.  The group's amount is the one the
highest-precedence family states (`charge.succeeded` first); conflicting amounts are counted once
and NAMED in `reasons["in_30d.amount_conflict"]`.  Duplicates collapsed are NAMED in
`reasons["in_30d.dedupe"]`.

RED leg: pre-fix, the first test reads 400.0 (four rows summed), not 100.0.
"""
from __future__ import annotations

import pytest

from app.models.finance import StripeEvent
from app.services import finance_service
from app.services.report_service import build_cash_block

PAYMENT_A = "pi_W4_A"
PAYMENT_B = "pi_W4_B"


def _payload(event_id: str, event_type: str, *, payment_intent: str | None) -> dict:
    """A delivery shaped like Stripe's: `data.object` carries `payment_intent` (or not)."""
    obj: dict = {"id": f"obj_{event_id}", "object": event_type.split(".")[0]}
    if payment_intent is not None:
        obj["payment_intent"] = payment_intent
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


async def _record(
    db,
    event_id: str,
    event_type: str,
    *,
    payment_intent: str | None,
    amount_cents: int | None,
    currency: str = "usd",
) -> None:
    """Persist the delivery through the REAL webhook writer (annotation included)."""
    await finance_service.record_stripe_event(
        db,
        stripe_event_id=event_id,
        event_type=event_type,
        raw_payload=_payload(event_id, event_type, payment_intent=payment_intent),
        amount_cents=amount_cents,
        currency=currency,
        payment_intent_id=payment_intent,
    )
    await db.flush()


async def _one_payment_four_families(db) -> None:
    for family in (
        "charge.succeeded",
        "payment_intent.succeeded",
        "invoice.paid",
        "invoice.payment_succeeded",
    ):
        await _record(
            db,
            f"evt_w4_{family}",
            family,
            payment_intent=PAYMENT_A,
            amount_cents=10_000,  # $100.00 in each of the four deliveries
        )


@pytest.mark.asyncio
async def test_one_payment_counted_once_across_four_event_families(async_db_session):
    """DT-03's named test: charge + intent + invoice.paid + invoice.payment_succeeded -> 100.0."""
    await _one_payment_four_families(async_db_session)

    block = await build_cash_block(async_db_session)

    assert block["in_30d"] == 100.0, (
        f"one $100.00 payment was counted as {block['in_30d']!r} of money in; the four event "
        f"families are ONE payment (same payment_intent id)"
    )
    assert "in_30d.dedupe" in block["reasons"], (
        "the collapse must be NAMED: an owner has to be able to see that 4 deliveries were 1 payment"
    )
    assert "4" in block["reasons"]["in_30d.dedupe"]
    assert "in_30d.amount_conflict" not in block["reasons"]


@pytest.mark.asyncio
async def test_two_distinct_payments_are_both_counted(async_db_session):
    """The guard on the fix: dedupe must not swallow a second real payment."""
    await _one_payment_four_families(async_db_session)
    await _record(
        async_db_session,
        "evt_w4_payment_b",
        "charge.succeeded",
        payment_intent=PAYMENT_B,
        amount_cents=10_000,
    )

    block = await build_cash_block(async_db_session)

    assert block["in_30d"] == 200.0, "two distinct payments are $200.00, not $100.00"


@pytest.mark.asyncio
async def test_an_event_without_a_payment_intent_is_counted_exactly_once(async_db_session):
    """No id means no PROOF of identity: the event is its own payment, counted once (never 0)."""
    await _record(
        async_db_session,
        "evt_w4_no_id_a",
        "charge.succeeded",
        payment_intent=None,
        amount_cents=4_200,
    )

    block = await build_cash_block(async_db_session)
    assert block["in_30d"] == 42.0

    # A second id-less delivery is a DIFFERENT event whose identity cannot be proven either way:
    # it is counted once too (the block never guesses two events are one payment without an id).
    await _record(
        async_db_session,
        "evt_w4_no_id_b",
        "invoice.paid",
        payment_intent=None,
        amount_cents=4_200,
    )
    block = await build_cash_block(async_db_session)
    assert block["in_30d"] == 84.0
    assert "in_30d.dedupe" not in block["reasons"], "no duplicate was collapsed in this window"


@pytest.mark.asyncio
async def test_a_conflict_is_counted_once_at_the_charge_amount_and_named(async_db_session):
    """Two families disagreeing about the amount: count the charge's, NAME the disagreement."""
    await _record(
        async_db_session,
        "evt_w4_conflict_charge",
        "charge.succeeded",
        payment_intent=PAYMENT_A,
        amount_cents=10_000,
    )
    await _record(
        async_db_session,
        "evt_w4_conflict_invoice",
        "invoice.paid",
        payment_intent=PAYMENT_A,
        amount_cents=12_000,
    )

    block = await build_cash_block(async_db_session)

    assert block["in_30d"] == 100.0, (
        "the settled charge states the amount; the invoice's disagreeing figure must not be added"
    )
    assert "in_30d.amount_conflict" in block["reasons"]
    assert "charge.succeeded" in block["reasons"]["in_30d.amount_conflict"]


@pytest.mark.asyncio
async def test_a_payment_no_family_priced_is_still_unmeasured_not_zero(async_db_session):
    """The A2A7-02 rule survives the dedupe: a genuinely unpriced payment makes the total unknown."""
    await _record(
        async_db_session,
        "evt_w4_unpriced",
        "charge.succeeded",
        payment_intent=PAYMENT_A,
        amount_cents=None,
    )

    block = await build_cash_block(async_db_session)
    assert block["in_30d"] is None
    assert "no measured amount" in block["reasons"]["in_30d"]


@pytest.mark.asyncio
async def test_a_payment_priced_by_any_family_is_measured(async_db_session):
    """The other direction: the charge reporting no amount is not "unknown" when the invoice did."""
    await _record(
        async_db_session,
        "evt_w4_half_priced_charge",
        "charge.succeeded",
        payment_intent=PAYMENT_A,
        amount_cents=None,
    )
    await _record(
        async_db_session,
        "evt_w4_half_priced_invoice",
        "invoice.paid",
        payment_intent=PAYMENT_A,
        amount_cents=7_500,
    )

    block = await build_cash_block(async_db_session)
    assert block["in_30d"] == 75.0, (
        "the payment's amount IS measured (the invoice states it); refusing would be wrong"
    )


@pytest.mark.asyncio
async def test_the_existing_currency_and_measured_zero_rules_still_hold(async_db_session):
    """Regression guards on the same read: another currency is excluded; no money-in is a measured 0."""
    await _record(
        async_db_session,
        "evt_w4_eur",
        "charge.succeeded",
        payment_intent="pi_W4_EUR",
        amount_cents=50_000,
        currency="eur",
    )
    block = await build_cash_block(async_db_session)
    assert block["in_30d"] is None, (
        "the window's only money-in delivery is in another currency — the USD inflow is unmeasured "
        "(nothing is converted), never a fabricated 0"
    )
    assert "another currency" in block["reasons"]["in_30d"]

    await _record(
        async_db_session,
        "evt_w4_failed",
        "charge.failed",
        payment_intent=None,
        amount_cents=10_000,
    )
    block = await build_cash_block(async_db_session)
    assert block["in_30d"] is None, (
        "telemetry exists (a failed charge) but no USD money-in succeeded: the USD inflow is a "
        "MEASURED zero... the EUR charge is still excluded from it"
    )


@pytest.mark.asyncio
async def test_the_stored_payment_intent_annotation_is_what_groups_the_rows(async_db_session):
    """The grouping reads the row's OWN stored annotation (the writer's contract), not a guess."""
    await _one_payment_four_families(async_db_session)
    rows = (await async_db_session.execute(StripeEvent.__table__.select())).fetchall()
    assert len(rows) == 4
    for row in rows:
        payload = row.raw_payload
        assert payload["_polsia"]["payment_intent_id"] == PAYMENT_A
