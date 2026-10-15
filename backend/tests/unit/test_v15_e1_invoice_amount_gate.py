"""V15 / E1 — 5-01 (S1-05, decision #3 LOCKED): the invoice-email gate keys on the amount ASKED.

RED-first: at b17d7b0 `deliver_invoice_email` consults the firewall with `value_usd=0.0`
(`invoice_service.py:428-432`) no matter what the invoice asks for, so a ≥ $25 customer-facing
ask sails past the $25 approval threshold (`APPROVAL_THRESHOLDS["value_usd"] = 25.0`).

The pinned rule (task-list 5-01 verbatim): consult with the invoice `total`; when `total` is
null (S1-04 kept it honestly nullable) use `subtotal`; when BOTH are null the ask amount is
unknown and cannot be proven under the threshold — the consult carries the threshold value
ITSELF (fail-closed: an unknown ask is gated exactly like an at-threshold ask), documented and
pinned, NEVER a silent 0.0.  A recorded 0 total is a real measurement and sends at 0.0.

Legs:
  * >= $25: the exact `value_usd` is passed AND the approval flow engages (refused by name,
    nothing sent) — and with a minted owner token the send completes (the gate, not a wall);
  * < $25: sends;
  * null total -> subtotal (exact value);
  * both null -> threshold value + named refusal, never 0.0;
  * total 0 -> honest 0.0, sends.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.agents.policy_firewall import (
    APPROVAL_THRESHOLDS,
    RunApproval,
    fingerprint,
    issue_approval,
    reset_run_approval,
    set_run_approval,
)
from app.core.errors import E_POLICY_DENIED, PolsiaError
from app.models.invoice import InvoiceStatus
from app.services import invoice_service

ISSUE = date(2026, 10, 14)
SECRET = b"v15-e1-invoice-approval-secret"
FOOTER_TEXT = "To opt out: reply STOP.\nAcme LLC — 1 Main St — https://acme.example/privacy"


async def _fake_footer(db) -> str:
    return FOOTER_TEXT


@pytest.fixture(autouse=True)
def _sandbox_off(monkeypatch):
    """Every leg here is about the REAL (non-sandbox) consult value."""
    from app.config import settings

    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr(invoice_service, "_compliance_footer", _fake_footer)


@pytest.fixture
def sent_calls(monkeypatch) -> list[dict[str, Any]]:
    from app.agents import integrations

    calls: list[dict[str, Any]] = []

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-v15-1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)
    return calls


@pytest.fixture
def consult_spy(monkeypatch) -> list[dict[str, Any]]:
    """Wraps the REAL `request_side_effect`: records every consult, keeps real semantics."""
    from app.agents import side_effects

    records: list[dict[str, Any]] = []
    real = side_effects.request_side_effect

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        records.append({"args": args, **kwargs})
        return real(*args, **kwargs)

    monkeypatch.setattr(side_effects, "request_side_effect", wrapper)
    return records


async def _draft(db, **money: Any):
    return await invoice_service.create_invoice(
        db,
        issue_date=ISSUE,
        bill_to={"name": "ACME", "email": "billing@acme.example"},
        payment_link="https://buy.stripe.com/v15_e1",
        **money,
    )


@pytest.mark.asyncio
async def test_above_threshold_passes_the_exact_amount_and_engages_approval(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, total=Decimal("120.00"))

    with pytest.raises(PolsiaError) as refusal:
        await invoice_service.deliver_invoice_email(
            async_db_session, invoice, subject="Invoice", body="Here it is"
        )

    assert refusal.value.code == E_POLICY_DENIED
    assert "approval" in refusal.value.message
    (record,) = consult_spy
    assert record["value_usd"] == 120.0, (
        f"5-01 not fixed: consult carried value_usd={record['value_usd']!r} for a $120.00 ask"
    )
    assert sent_calls == [], "a refused send must not reach the provider"


@pytest.mark.asyncio
async def test_above_threshold_with_a_minted_owner_token_sends(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, total=Decimal("120.00"))
    fp = fingerprint(
        "external_reversible",
        "email",
        {"action": "send_invoice", "invoice": invoice.number, "to_domain": "acme.example"},
    )
    token = issue_approval(SECRET, action_fingerprint=fp).token
    set_run_approval(RunApproval(secret=SECRET, token=token))
    try:
        result = await invoice_service.deliver_invoice_email(
            async_db_session, invoice, subject="Invoice", body="Here it is"
        )
    finally:
        reset_run_approval()

    assert result.get("ok") is True
    (record,) = consult_spy
    assert record["value_usd"] == 120.0
    assert len(sent_calls) == 1, "an owner-approved >= $25 ask must complete"


@pytest.mark.asyncio
async def test_below_threshold_sends_at_its_exact_value(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, total=Decimal("12.00"))

    result = await invoice_service.deliver_invoice_email(
        async_db_session, invoice, subject="Invoice", body="Here it is"
    )

    assert result.get("ok") is True
    (record,) = consult_spy
    assert record["value_usd"] == 12.0
    assert len(sent_calls) == 1


@pytest.mark.asyncio
async def test_null_total_falls_back_to_the_subtotal(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, subtotal=Decimal("30.00"), total=None)

    with pytest.raises(PolsiaError) as refusal:
        await invoice_service.deliver_invoice_email(
            async_db_session, invoice, subject="Invoice", body="Here it is"
        )

    assert refusal.value.code == E_POLICY_DENIED
    (record,) = consult_spy
    assert record["value_usd"] == 30.0, "the documented fallback is the SUBTOTAL, not 0.0"
    assert sent_calls == []


@pytest.mark.asyncio
async def test_both_amounts_null_is_gated_at_the_threshold_never_zero(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, subtotal=None, total=None)

    with pytest.raises(PolsiaError) as refusal:
        await invoice_service.deliver_invoice_email(
            async_db_session, invoice, subject="Invoice", body="Here it is"
        )

    assert refusal.value.code == E_POLICY_DENIED
    assert "approval" in refusal.value.message
    (record,) = consult_spy
    assert record["value_usd"] == float(APPROVAL_THRESHOLDS["value_usd"]), (
        "an unknown ask must be gated at the threshold value (documented fail-closed branch)"
    )
    assert record["value_usd"] != 0.0, "never the pre-fix silent 0.0"
    assert sent_calls == []


@pytest.mark.asyncio
async def test_a_recorded_zero_total_is_an_honest_zero_and_sends(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    invoice = await _draft(async_db_session, subtotal=Decimal("0.00"), total=Decimal("0.00"))

    result = await invoice_service.deliver_invoice_email(
        async_db_session, invoice, subject="Invoice", body="Here it is"
    )

    assert result.get("ok") is True
    (record,) = consult_spy
    assert record["value_usd"] == 0.0, "a MEASURED zero is the honest value for a $0 ask"
    assert len(sent_calls) == 1


@pytest.mark.asyncio
async def test_reminder_context_consults_at_zero_and_the_ladder_keeps_delivering(
    async_db_session, mock_redis, consult_spy, sent_calls
):
    """The REMINDER branch: a sent invoice's follow-up (the dunning ladder's delivery) consults
    at 0.0 — documented, not accidental.

    The ask was gated at SEND time (only a sub-threshold / owner-approved / pre-S1-05 send ever
    reaches `sent`), and a beat tick carries no approval channel (`RUN_APPROVAL` is run-scoped),
    so keying reminders at the amount would permanently refuse every >= $25 reminder — silent
    ladder death, not "the approval flow engages".  End-to-end proof: the full ladder suites
    (`test_v9_dunning.py`, `test_v11_logic_f5_dunning_durability.py`) re-run green.
    """
    invoice = await _draft(async_db_session, total=Decimal("250.00"))
    invoice.status = InvoiceStatus.SENT  # the state the dunning ladder delivers on
    await async_db_session.flush()

    result = await invoice_service.deliver_invoice_email(
        async_db_session, invoice, subject="Reminder", body="Please pay"
    )

    assert result.get("ok") is True
    (record,) = consult_spy
    assert record["value_usd"] == 0.0, (
        "a reminder re-asks an amount already gated at send time; the consult is 0.0 by the "
        "documented reminder branch (beat ticks have no approval channel)"
    )
    assert len(sent_calls) == 1, "the ladder's delivery must keep working"
