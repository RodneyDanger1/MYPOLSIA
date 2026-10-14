"""V9/B4 — the dunning ladder: frozen-clock steps, EACH STEP AT MOST ONCE (activity-row ledger).

The plan's own ruling (frozen plan §9 B4, quoted in the receipt): ladder state lives in the
ACTIVITY ROWS — "send each step at most once by checking the activity/email_logs for that
invoice+step — honest and column-free".  NO new columns; the invoices schema is frozen (0008).

The ladder (frozen clock = the `now` argument, a real parameter of the task body):
pre-due −3 days, due day, +5, +15, +30 (final).  Every email carries the invoice number, the
amount (or an honest "amount not recorded" — money never fabricates zeros), the due date, the
SAME payment_link as the invoice row, and ONE ask.  Once a step's row is in the ledger it can
never fire again; a step only enters the ledger after a REAL delivery.

TEST DOUBLES at their boundaries only (`app.agents.integrations.send_email` = the provider edge;
`invoice_service._compliance_footer` = the parallel-seat b3 module; `settings.sandbox_mode` =
deployment policy).  The refused-send test runs the REAL firewall path (sandbox on => the real
E_SANDBOX_BLOCKED refusal, nothing sent, nothing consumed from the ladder).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import E_SANDBOX_BLOCKED, E_VALIDATION
from app.models.invoice import Invoice, InvoiceStatus

DUE = date(2026, 11, 10)
LINK = "https://buy.stripe.com/test_v9dun"
NUMBER = "INV-2026-500"


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def _run(queue_db, day: date) -> dict:
    """Drive the REAL task body with a frozen clock (the `now` seam the body documents)."""
    from celery_app.tasks.dunning import _run_dunning

    return queue_db.run(_run_dunning(now=_at(day)))


def _seed(queue_db, *, due: date = DUE, status: InvoiceStatus = InvoiceStatus.SENT,
          link: str | None = LINK, total: str | None = "250.00",
          number: str = NUMBER, bill_to: dict | None = None) -> int:
    async def _go() -> int:
        async with queue_db.factory() as db:
            invoice = Invoice(
                number=number,
                status=status,
                issue_date=due - timedelta(days=30),
                due_date=due,
                net_days=30,
                currency="USD",
                total=Decimal(total) if total is not None else None,
                payment_link=link,
                bill_to=bill_to if bill_to is not None else
                {"name": "ACME", "email": "billing@acme.example"},
            )
            db.add(invoice)
            await db.flush()
            invoice_id = int(invoice.id)
            await db.commit()
            return invoice_id

    return queue_db.run(_go())


def _ledger(queue_db) -> list[dict]:
    rows = queue_db.query(
        "SELECT summary, detail FROM activity_log WHERE action = 'invoice_dunning' ORDER BY id"
    )
    return [{**row, "detail": json.loads(row["detail"]) if row["detail"] else {}} for row in rows]


def _invoice_status(queue_db, invoice_id: int) -> str:
    return queue_db.query("SELECT status FROM invoices WHERE id = ?", (invoice_id,))[0]["status"]


@pytest.fixture
def mail_seam(monkeypatch):
    """The provider edge + the parallel-seat footer + the sandbox policy, as the other email
    suites pin them: `integrations.send_email` is the external boundary; nothing else is faked."""
    from app.agents import integrations
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)

    async def footer(db) -> str:
        return "ACME LLC · 1 Main St, Testville · unsubscribe: reply STOP\nAI assistance disclosure: on."

    monkeypatch.setattr(invoice_service, "_compliance_footer", footer)

    calls: list[dict] = []

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": f"sg-dun-{len(calls)}"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)
    return calls


# ── the frozen-clock steps ───────────────────────────────────────────────────────────────────


def test_pre_due_step_fires_at_minus_three_and_never_twice(queue_db, mock_redis, mail_seam):
    invoice_id = _seed(queue_db)

    result = _run(queue_db, date(2026, 11, 7))  # due − 3

    assert result["status"] == "completed"
    (fired,) = result["sent"]
    assert fired["step"] == "pre_due" and fired["invoice_id"] == invoice_id
    (call,) = mail_seam
    assert call["to"] == "billing@acme.example"
    assert NUMBER in call["subject"] and "250.00" in call["subject"]
    body = call["body"]
    assert NUMBER in body
    assert "250.00" in body, "the amount is carried"
    assert "2026-11-10" in body, "the due date is carried"
    assert LINK in body, "the SAME payment_link is carried"
    assert body.count("Please pay") == 1, "exactly ONE ask"

    rows = _ledger(queue_db)
    assert len(rows) == 1 and rows[0]["detail"]["step"] == "pre_due"
    assert rows[0]["detail"]["invoice_id"] == invoice_id

    again = _run(queue_db, date(2026, 11, 7))  # a second tick, same day
    assert again["sent"] == [] and again["refused"] == []
    assert len(mail_seam) == 1, "EACH STEP AT MOST ONCE: no second email"
    assert len(_ledger(queue_db)) == 1


def test_due_day_fires_the_due_step_and_names_superseded_ones(queue_db, mock_redis, mail_seam):
    invoice_id = _seed(queue_db)

    result = _run(queue_db, DUE)

    (fired,) = result["sent"]
    assert fired["step"] == "due"
    assert fired["superseded"] == ["pre_due"], "a skipped earlier step is NAMED, never silent"
    assert len(mail_seam) == 1 and mail_seam[0]["body"].count("Please pay") == 1
    (row,) = _ledger(queue_db)
    assert row["detail"]["step"] == "due" and row["detail"]["superseded"] == ["pre_due"]
    assert _invoice_status(queue_db, invoice_id) == "sent", "the due-day step does not flip status"


def test_due_plus_five_fires_and_flips_sent_to_overdue(queue_db, mock_redis, mail_seam):
    invoice_id = _seed(queue_db)

    result = _run(queue_db, DUE + timedelta(days=5))

    (fired,) = result["sent"]
    assert fired["step"] == "due_plus_5"
    assert _invoice_status(queue_db, invoice_id) == "overdue", "post-due: the row is factually overdue"
    (row,) = _ledger(queue_db)
    assert row["detail"]["status_flipped_to"] == "overdue"
    assert len(mail_seam) == 1


def test_every_step_fires_at_most_once_across_the_whole_ladder(queue_db, mock_redis, mail_seam):
    invoice_id = _seed(queue_db)
    days = [
        (date(2026, 11, 7), "pre_due"),
        (DUE, "due"),
        (DUE + timedelta(days=5), "due_plus_5"),
        (DUE + timedelta(days=15), "due_plus_15"),
        (DUE + timedelta(days=30), "due_plus_30"),
    ]
    for day, expected in days:
        result = _run(queue_db, day)
        assert [f["step"] for f in result["sent"]] == [expected], day

    assert len(mail_seam) == 5, "one email per step, five steps"
    assert "Final notice" in mail_seam[-1]["body"]

    # after the final step: nothing, ever again — and a replayed tick consumes nothing
    after = _run(queue_db, date(2026, 12, 20))
    assert after["status"] == "no_due" and after["sent"] == []
    assert len(mail_seam) == 5
    steps = [row["detail"]["step"] for row in _ledger(queue_db)]
    assert steps == ["pre_due", "due", "due_plus_5", "due_plus_15", "due_plus_30"]
    # the invoice went overdue at the first post-due step and stayed on the ladder
    assert _invoice_status(queue_db, invoice_id) == "overdue"


def test_nothing_due_is_a_bare_no_op(queue_db, mock_redis, mail_seam):
    _seed(queue_db, due=date(2026, 12, 20), number="INV-2026-503")

    result = _run(queue_db, date(2026, 11, 7))

    assert result["status"] == "no_due"
    assert result["sent"] == [] and result["refused"] == []
    assert mail_seam == [] and _ledger(queue_db) == []


def test_paid_and_void_invoices_never_get_a_dunning_mail(queue_db, mock_redis, mail_seam):
    _seed(queue_db, status=InvoiceStatus.PAID, number="INV-2026-501")
    _seed(queue_db, status=InvoiceStatus.VOID, number="INV-2026-502")

    result = _run(queue_db, DUE + timedelta(days=40))

    assert result["status"] == "no_due"
    assert result["sent"] == [] and mail_seam == [] and _ledger(queue_db) == []


def test_unmeasured_total_is_stated_not_fabricated(queue_db, mock_redis, mail_seam):
    _seed(queue_db, total=None, number="INV-2026-504")

    result = _run(queue_db, DUE + timedelta(days=15))

    assert [f["step"] for f in result["sent"]] == ["due_plus_15"]
    body = mail_seam[0]["body"]
    assert "amount not recorded" in body
    assert "Total: 0.00" not in body, "the unmeasured amount is never fabricated as a zero"


# ── refusals: named, recorded, and never consuming a step ────────────────────────────────────


def test_refused_send_is_reported_and_the_step_is_retried_next_tick(queue_db, mock_redis, monkeypatch):
    """No mail seam: the REAL firewall path refuses in sandbox mode.  The refusal is reported by
    name in the tick result, NOTHING enters the ledger, and the same step can still fire later."""
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", True)

    async def ready_footer(db) -> str:
        return "ACME LLC · 1 Main St · the footer is available; POLICY must refuse this one"

    monkeypatch.setattr(invoice_service, "_compliance_footer", ready_footer)
    invoice_id = _seed(queue_db)

    result = _run(queue_db, DUE)

    assert result["sent"] == []
    (refusal,) = result["refused"]
    assert refusal["code"] == E_SANDBOX_BLOCKED
    assert refusal["step"] == "due" and refusal["invoice_id"] == invoice_id
    assert _ledger(queue_db) == [], "a refused send consumes nothing"
    named = queue_db.query(
        "SELECT detail FROM activity_log WHERE action = 'invoice_dunning_refused'"
    )
    assert len(named) == 1 and json.loads(named[0]["detail"])["code"] == E_SANDBOX_BLOCKED

    # the refusal did not burn the step: with a working path it still fires, same day
    from app.agents import integrations

    monkeypatch.setattr(settings, "sandbox_mode", False)
    calls: list[dict] = []
    monkeypatch.setattr(
        integrations, "send_email",
        lambda *, to, subject, body: calls.append({"to": to}) or {"ok": True, "result": {"message_id": "sg-retry"}},
    )
    retried = _run(queue_db, DUE)
    assert [f["step"] for f in retried["sent"]] == ["due"]
    assert len(calls) == 1


def test_missing_payment_link_refuses_by_name(queue_db, mock_redis, mail_seam):
    invoice_id = _seed(queue_db, link=None)

    result = _run(queue_db, DUE + timedelta(days=15))

    assert result["sent"] == [] and mail_seam == [] and _ledger(queue_db) == []
    (refusal,) = result["refused"]
    assert refusal["code"] == E_VALIDATION
    assert "payment_link" in refusal["message"]
    assert refusal["invoice_id"] == invoice_id
