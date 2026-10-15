"""V11/W2 F2 (money path) — a broken invoice flip must not poison the webhook's session.

Audit finding F2: `finance_service._flip_invoices_safely` (finance_service.py:863-888)
swallows exceptions WITHOUT a savepoint.  When the swallowed failure is a DB-layer error
(a failed flush: constraint, serialization failure, locked database), SQLAlchemy leaves the
session in "pending rollback" — the webhook's post-handler commit (`core/database.py:106-120`)
then raises `PendingRollbackError` and the delivery 5xxs, which is exactly what triggers
Stripe's retry storm.

The contract this file pins:
1. a DB error inside the flip is CONTAINED (the savepoint rolls back; the request's own
   commit still lands) — `test_db_error_inside_flip_does_not_poison_the_webhook_commit`;
2. the failure is still RECORDED, never silent — same test (activity row);
3. recorded/acted-on semantics on the healthy path are UNCHANGED — a matched SENT invoice
   still flips sent -> overdue with its ledger row — `test_healthy_flip_still_flips_and_records`;
4. even a failure of the ERROR-RECORDING write itself is contained (both writes are
   savepointed) — `test_flip_error_record_failure_is_also_contained`.

The injected fault is REAL SQLite (a trigger that aborts the ledger INSERT), not a mocked
call path: the exception SQLAlchemy sees is exactly the class production hits.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from app.models.finance import StripeEvent
from app.models.invoice import Invoice, InvoiceStatus
from app.models.report import ActivityLog
from app.services.finance_service import record_stripe_event

LINK = "https://buy.stripe.com/test_v11f2"
NUMBER = "INV-2026-V11F2"
EVENT_ID = "evt_v11f2_001"

#: Aborts the flip's ledger INSERT with a REAL sqlite3.IntegrityError -> a failed flush.
BLOCK_FLIP_SQL = """
CREATE TRIGGER v11_f2_block_flip_ledger
BEFORE INSERT ON activity_log
WHEN NEW.action = 'invoice_payment_failed'
BEGIN
  SELECT RAISE(ABORT, 'v11-f2 forced flip failure');
END
"""

#: Aborts BOTH the flip ledger row and the flip-error row (the hostile case).
BLOCK_BOTH_SQL = """
CREATE TRIGGER v11_f2_block_flip_and_error
BEFORE INSERT ON activity_log
WHEN NEW.action IN ('invoice_payment_failed', 'invoice_payment_failed_flip_error')
BEGIN
  SELECT RAISE(ABORT, 'v11-f2 forced failure (flip + error record)');
END
"""


def _payload() -> dict:
    return {
        "id": EVENT_ID,
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "payment_link": LINK,
                "amount": 2500,
                "currency": "usd",
            }
        },
    }


async def _seed_sent_invoice(db) -> None:
    db.add(
        Invoice(
            number=NUMBER,
            status=InvoiceStatus.SENT,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 10, 1),
            net_days=30,
            currency="USD",
            total=Decimal("250.00"),
            payment_link=LINK,
            bill_to={"name": "ACME", "email": "billing@acme.example"},
        )
    )
    await db.flush()


async def _actions(db) -> list[str]:
    return list(
        (await db.execute(select(ActivityLog.action).order_by(ActivityLog.id))).scalars().all()
    )


async def _deliver(db) -> None:
    """The webhook's own call sequence: record (flip included), then the post-handler commit."""
    await record_stripe_event(
        db,
        stripe_event_id=EVENT_ID,
        event_type="invoice.payment_failed",
        raw_payload=_payload(),
        customer_id=None,
        amount_cents=2500,
        currency="usd",
        payment_intent_id=None,
    )


async def test_db_error_inside_flip_does_not_poison_the_webhook_commit(async_db_session):
    db = async_db_session
    await _seed_sent_invoice(db)
    await db.commit()
    await db.execute(text(BLOCK_FLIP_SQL))

    await _deliver(db)

    # THE FIX: the post-handler commit of `core/database.py:106-120` must still land.
    await db.commit()

    # (2) the failure is NOT silent.
    assert "invoice_payment_failed_flip_error" in await _actions(db)
    # the contained flip rolled back to its savepoint: the invoice is still SENT.
    invoice = (
        await db.execute(select(Invoice).where(Invoice.number == NUMBER))
    ).scalar_one()
    assert invoice.status == InvoiceStatus.SENT


async def test_flip_error_record_failure_is_also_contained(async_db_session):
    db = async_db_session
    await _seed_sent_invoice(db)
    await db.commit()
    await db.execute(text(BLOCK_BOTH_SQL))

    await _deliver(db)

    # Even when the error-recording write fails too, the delivery still commits (the event
    # row is the durable record; the failure lives in the worker log).
    await db.commit()
    # The durable artifact of the delivery is the stripe_events row — it must be there.
    event = (
        await db.execute(select(StripeEvent).where(StripeEvent.stripe_event_id == EVENT_ID))
    ).scalar_one()
    assert event.event_type == "invoice.payment_failed"


async def test_healthy_flip_still_flips_and_records(async_db_session):
    db = async_db_session
    await _seed_sent_invoice(db)
    await db.commit()

    await _deliver(db)
    await db.commit()

    actions = await _actions(db)
    assert "invoice_payment_failed" in actions
    invoice = (
        await db.execute(select(Invoice).where(Invoice.number == NUMBER))
    ).scalar_one()
    assert invoice.status == InvoiceStatus.OVERDUE
