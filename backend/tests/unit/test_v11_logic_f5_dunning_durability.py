"""V11/W2 F5 — dunning durability: the crash window between send and ledger commit.

Audit finding F5: `dunning.py:179` SENT before the ledger commit at `:215-244`.  A crash
between the two left NO ledger row, so the next tick re-sent the same step — a duplicate
customer email.  The fix pre-writes a CLAIM ledger row (`invoice_dunning_claim`, state
"pending") before the send; the step counts as consumed from that commit on; the claim is
finalized ("sent") after a confirmed delivery, or released ("refused") when the send is
refused BY NAME (a refusal consumes nothing — the existing contract).

Pinned here:
1. crash between the claim commit and the send -> the step is NEVER re-sent (the durable
   claim blocks it), and the unfinalized claim is NAMED in the next tick's result — T1;
2. a confirmed send finalizes the claim ("sent" + message_id) and the pre-existing single
   `invoice_dunning` ledger row still lands, byte-compatible — T2;
3. a refused send RELEASES the claim ("refused") so the same step can fire later, and the
   `invoice_dunning` ledger stays empty (the shipped V9 pin) — T3.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import E_SANDBOX_BLOCKED

DUE = date(2026, 11, 10)
LINK = "https://buy.stripe.com/test_v11f5"
NUMBER = "INV-2026-V11F5"


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def _run(queue_db, day: date):
    from celery_app.tasks.dunning import _run_dunning

    return queue_db.run(_run_dunning(now=_at(day)))


def _seed(queue_db) -> int:
    from app.models.invoice import Invoice, InvoiceStatus

    async def _go() -> int:
        async with queue_db.factory() as db:
            invoice = Invoice(
                number=NUMBER,
                status=InvoiceStatus.SENT,
                issue_date=DUE - timedelta(days=30),
                due_date=DUE,
                net_days=30,
                currency="USD",
                total=Decimal("250.00"),
                payment_link=LINK,
                bill_to={"name": "ACME", "email": "billing@acme.example"},
            )
            db.add(invoice)
            await db.flush()
            invoice_id = int(invoice.id)
            await db.commit()
            return invoice_id

    return queue_db.run(_go())


def _ledger(queue_db) -> list[dict]:
    import json

    rows = queue_db.query(
        "SELECT summary, detail FROM activity_log WHERE action = 'invoice_dunning' ORDER BY id"
    )
    return [{**row, "detail": json.loads(row["detail"]) if row["detail"] else {}} for row in rows]


def _claims(queue_db) -> list[dict]:
    import json

    rows = queue_db.query(
        "SELECT summary, detail FROM activity_log WHERE action = 'invoice_dunning_claim' "
        "ORDER BY id"
    )
    return [{**row, "detail": json.loads(row["detail"]) if row["detail"] else {}} for row in rows]


@pytest.fixture
def mail_seam(monkeypatch):
    """The V9 boundary set: provider edge + footer seam + sandbox policy (verbatim pattern)."""
    from app.agents import integrations
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)

    async def footer(db) -> str:
        return "ACME LLC · 1 Main St, Testville · unsubscribe: reply STOP"

    monkeypatch.setattr(invoice_service, "_compliance_footer", footer)

    calls: list[dict] = []

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": f"sg-f5-{len(calls)}"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)
    return calls


def test_crash_between_claim_and_send_never_resends(queue_db, mock_redis, monkeypatch):
    """T1 — the crash window: claim committed, send died mid-flight, next tick must NOT resend."""
    from app.services import invoice_service

    invoice_id = _seed(queue_db)

    async def _crash(*args, **kwargs):
        raise RuntimeError("worker crashed mid-send (v11-f5 test)")

    monkeypatch.setattr(invoice_service, "deliver_invoice_email", _crash)
    # V18/L1 (L3-07) DELIBERATE RE-PIN: this RuntimeError used to escape the tick, aborting every
    # remaining invoice.  The contract is now "record the step's named failure, keep processing"
    # (`dunning._dun_one`'s uncoded-exception branch).  The crash-window contract THIS test exists
    # for — the claim stays pending, the step is never re-sent, the window is named — is unchanged.
    crashed = _run(queue_db, date(2026, 11, 7))  # due - 3: the pre_due step is owed
    (failure,) = crashed["errors"]
    assert failure["step"] == "pre_due" and failure["invoice_id"] == invoice_id
    assert failure["code"] == "E_DUNNING_STEP_FAILED"

    (claim,) = _claims(queue_db)
    assert claim["detail"]["state"] == "pending"
    assert claim["detail"]["invoice_id"] == invoice_id
    assert claim["detail"]["step"] == "pre_due"

    # The next tick (healthy send path restored) must see the claim as CONSUMED.
    sent_calls: list[dict] = []

    async def _healthy(db, invoice, *, subject, body):
        sent_calls.append({"subject": subject})
        return {"ok": True, "result": {"message_id": "sg-f5-x"}}

    monkeypatch.setattr(invoice_service, "deliver_invoice_email", _healthy)
    result = _run(queue_db, date(2026, 11, 7))

    assert sent_calls == [], "the claimed step must never be re-sent after a crash window"
    assert result["sent"] == []
    assert any(
        entry["step"] == "pre_due" and entry["invoice_id"] == invoice_id
        for entry in result["unfinalized_claims"]
    ), "the stuck claim is NAMED in the tick result, never silent"
    assert _ledger(queue_db) == []


def test_a_confirmed_send_finalizes_the_claim(queue_db, mock_redis, mail_seam):
    """T2 — the healthy path: claim finalizes to 'sent'; the shipped single ledger row stays."""
    invoice_id = _seed(queue_db)

    result = _run(queue_db, date(2026, 11, 7))

    assert len(mail_seam) == 1
    (claim,) = _claims(queue_db)
    assert claim["detail"]["state"] == "sent"
    assert claim["detail"]["message_id"] == "sg-f5-1"
    assert result["unfinalized_claims"] == []
    (row,) = _ledger(queue_db)  # byte-compatible with the V9 pins
    assert row["detail"]["step"] == "pre_due" and row["detail"]["invoice_id"] == invoice_id


def test_a_refused_send_releases_the_claim_and_the_step_can_fire_later(
    queue_db, mock_redis, monkeypatch, mail_seam
):
    """T3 — refusal semantics: the claim is released; the same step fires once the send is legal."""
    from app.config import settings

    _seed(queue_db)  # seeds the queue DB; this test asserts on claims/refusals, not the id

    # 1) Sandbox ON: the REAL firewall refuses the send by name (V9 pattern).
    monkeypatch.setattr(settings, "sandbox_mode", True)
    result = _run(queue_db, date(2026, 11, 7))
    assert result["refused"] and result["refused"][0]["code"] == E_SANDBOX_BLOCKED
    (claim,) = _claims(queue_db)
    assert claim["detail"]["state"] == "refused"
    assert _ledger(queue_db) == [], "a refused send consumes nothing"
    assert mail_seam == []

    # 2) Sandbox OFF: the same step is still owed and now sends.
    monkeypatch.setattr(settings, "sandbox_mode", False)
    result = _run(queue_db, date(2026, 11, 7))
    assert len(mail_seam) == 1, "the released step must be able to fire"
    assert result["sent"] and result["sent"][0]["step"] == "pre_due"
    assert len(_claims(queue_db)) == 2  # the refused claim + the new one
    assert _ledger(queue_db)[0]["detail"]["step"] == "pre_due"
