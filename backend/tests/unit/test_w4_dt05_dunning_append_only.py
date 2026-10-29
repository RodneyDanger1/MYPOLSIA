"""W4 / DT-05 — the dunning claim finalizes by APPEND, never by an UPDATE of the append-only log.

THE DEFECT (data-hunter DT-05, P1, PostgreSQL-only).  Migration 0002 installs
`trg_activity_log_no_update` (a `BEFORE UPDATE` trigger that raises) on `activity_log`.  The
dunning ladder finalized its durable claim with `claim.detail = {...state: 'sent'}` — an UPDATE of
that table.  On PostgreSQL no delivery ever finalized, the invoice never flipped to overdue, and a
refused step was consumed forever (the exact opposite of "a refusal consumes nothing").  SQLite has
no such trigger, so every unit test stayed green while production was broken.

THE FIX under test: the claim row is INSERT-only.  Its outcome is APPENDED to
`invoice_dunning_claim_results` (migration 0017; `claim_row_id` UNIQUE, `state` sent|refused) and
`_sent_steps` derives the consumed set from those rows.  `unfinalized_claims` names only claims
with no result row.

HOW THE POSTGRES TRIGGER IS EXERCISED HERE (labelled PG-EMULATED, not PG):
`_install_append_only_trigger` creates SQLite's equivalent of 0002's trigger body on the test
database, and `test_the_emulated_trigger_really_refuses_an_update` proves the emulation itself
refuses an UPDATE — otherwise the legs below would pass vacuously.  The real PostgreSQL trigger was
NOT run on this host (no PostgreSQL): the production-DB leg is PG-ONLY/UNVERIFIED.

RED leg: with the pre-fix dunning.py restored and the trigger installed, the first test fails with
`IntegrityError ... activity_log is append-only` and the claim stays pending.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

DUE = date(2026, 11, 10)
LINK = "https://buy.stripe.com/w4-dt05"
NUMBER = "INV-W4-DT05"
TRIGGER = "trg_activity_log_no_update"
TRIGGER_SQL = f"""
CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON activity_log
BEGIN
    SELECT RAISE(ABORT, 'activity_log is append-only: UPDATE is refused (meridian migration 0002)');
END;
"""


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def _run(queue_db, day: date) -> dict[str, Any]:
    from celery_app.tasks.dunning import _run_dunning

    return queue_db.run(_run_dunning(now=_at(day)))


def _seed_invoice(queue_db, *, status: str = "sent", due: date = DUE) -> int:
    from app.models.invoice import Invoice, InvoiceStatus

    async def _go() -> int:
        async with queue_db.factory() as db:
            invoice = Invoice(
                number=NUMBER,
                status=InvoiceStatus(status),
                issue_date=due - timedelta(days=30),
                due_date=due,
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


def _claims(queue_db) -> list[dict]:
    import json

    rows = queue_db.query(
        "SELECT id, summary, detail FROM activity_log WHERE action = 'invoice_dunning_claim' "
        "ORDER BY id"
    )
    return [{**row, "detail": json.loads(row["detail"]) if row["detail"] else {}} for row in rows]


def _results(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT claim_row_id, invoice_id, step, state, message_id, code, detail "
        "FROM invoice_dunning_claim_results ORDER BY id"
    )


def _ledger(queue_db) -> list[dict]:
    import json

    rows = queue_db.query(
        "SELECT detail FROM activity_log WHERE action = 'invoice_dunning' ORDER BY id"
    )
    return [json.loads(row["detail"]) if row["detail"] else {} for row in rows]


@pytest.fixture
def append_only_trigger(queue_db):
    """Emulate migration 0002's `BEFORE UPDATE` trigger on the test SQLite database."""
    con = sqlite3.connect(str(queue_db.path))
    try:
        con.executescript(TRIGGER_SQL)
        con.commit()
    finally:
        con.close()
    yield TRIGGER
    con = sqlite3.connect(str(queue_db.path))
    try:
        con.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
        con.commit()
    finally:
        con.close()


@pytest.fixture
def mail_seam(monkeypatch):
    """The shipped dunning test boundary: provider edge + footer seam + sandbox policy."""
    from app.agents import integrations
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)

    async def footer(db) -> str:  # noqa: ANN001, ARG001 - the service's signature
        return "ACME LLC · 1 Main St, Testville · unsubscribe: reply STOP"

    monkeypatch.setattr(invoice_service, "_compliance_footer", footer)

    calls: list[dict] = []

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-w4-dt05"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)
    return calls


def test_the_emulated_trigger_really_refuses_an_update(queue_db):
    """CONTROL: the emulation must itself refuse an UPDATE, or every leg below is vacuous."""
    con = sqlite3.connect(str(queue_db.path))
    try:
        con.executescript(TRIGGER_SQL)
        con.execute(
            "INSERT INTO activity_log (agent_type, action, summary) VALUES ('finance','x','y')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError) as refused:
            con.execute("UPDATE activity_log SET summary = 'rewritten'")
            con.commit()
        assert "append-only" in str(refused.value)
    finally:
        con.close()


def test_a_dunning_claim_finalizes_under_the_append_only_trigger(
    queue_db, mock_redis, mail_seam, append_only_trigger
):
    """DT-05's named test: with UPDATE refused, the step still finalizes, flips and is audited."""
    invoice_id = _seed_invoice(queue_db)

    result = _run(queue_db, DUE - timedelta(days=3))  # the pre_due rung is owed

    assert result["errors"] == [], (
        f"the tick hit the append-only refusal instead of appending its outcome: {result['errors']}"
    )
    assert [entry["step"] for entry in result["sent"]] == ["pre_due"]
    assert len(mail_seam) == 1

    (claim,) = _claims(queue_db)
    assert claim["detail"]["state"] == "pending", (
        "the claim activity row must stay INSERT-only: it is never rewritten"
    )
    (finalized,) = _results(queue_db)
    assert finalized["claim_row_id"] == claim["id"]
    assert finalized["invoice_id"] == invoice_id
    assert finalized["step"] == "pre_due"
    assert finalized["state"] == "sent"
    assert finalized["message_id"] == "sg-w4-dt05"
    assert finalized["code"] is None

    assert [row["step"] for row in _ledger(queue_db)] == ["pre_due"]
    assert result["unfinalized_claims"] == [], (
        "a finalized claim must not be reported as a crash window"
    )


def _consumed(queue_db) -> dict[int, set[str]]:
    """The tick's consumed-steps map, read through the REAL `_sent_steps`."""
    from celery_app.tasks import dunning

    async def _go() -> dict[int, set[str]]:
        async with queue_db.factory() as db:
            return await dunning._sent_steps(db)

    return queue_db.run(_go())


def test_a_refusal_is_an_appended_result_and_the_step_can_fire_later(
    queue_db, mock_redis, monkeypatch, mail_seam, append_only_trigger
):
    """The V9 rule survives the append-only fix: a refusal consumes nothing, and it is durable."""
    from app.config import settings

    invoice_id = _seed_invoice(queue_db)

    monkeypatch.setattr(settings, "sandbox_mode", True)  # the real firewall refuses by name
    refused_run = _run(queue_db, DUE - timedelta(days=3))
    assert refused_run["refused"] and refused_run["refused"][0]["code"].startswith("E_")
    (refused_result,) = _results(queue_db)
    assert refused_result["state"] == "refused"
    assert refused_result["code"] == refused_run["refused"][0]["code"]
    assert _ledger(queue_db) == [], "a refused send consumes nothing"

    assert "pre_due" not in _consumed(queue_db).get(invoice_id, set()), (
        "a REFUSED claim result must not consume the step (the claim row is pending, the result "
        "says refused — the result is the authority)"
    )

    monkeypatch.setattr(settings, "sandbox_mode", False)
    sent_run = _run(queue_db, DUE - timedelta(days=3))
    assert [entry["step"] for entry in sent_run["sent"]] == ["pre_due"], (
        "the refused step must be re-attemptable once the cause is gone"
    )
    states = [(row["claim_row_id"], row["state"]) for row in _results(queue_db)]
    assert [state for _claim, state in states] == ["refused", "sent"], (
        "the outcome is APPENDED: one refused row then one sent row, never an UPDATE"
    )


def test_an_unfinalized_claim_is_still_named_and_still_consumes(
    queue_db, mock_redis, monkeypatch, append_only_trigger
):
    """The crash window keeps its F5 contract: pending claim -> consumed, named, never resent."""
    from app.services import invoice_service

    invoice_id = _seed_invoice(queue_db)

    async def _crash(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("worker crashed mid-send (w4-dt05)")

    monkeypatch.setattr(invoice_service, "deliver_invoice_email", _crash)
    crashed = _run(queue_db, DUE - timedelta(days=3))
    assert crashed["errors"] and crashed["errors"][0]["code"] == "E_DUNNING_STEP_FAILED"
    assert _results(queue_db) == [], "a crash finalizes nothing"

    monkeypatch.setattr(invoice_service, "deliver_invoice_email", _never_called)
    again = _run(queue_db, DUE - timedelta(days=3))
    assert again["sent"] == [], "the unfinalized claim must block a re-send"
    assert [entry["invoice_id"] for entry in again["unfinalized_claims"]] == [invoice_id]


async def _never_called(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("the claimed step must never be re-sent")
