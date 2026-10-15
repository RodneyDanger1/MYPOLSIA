"""V9/B4 — invoices v1: numbering, status transitions, send refusal, webhook flip (route + service).

WHAT THIS FILE PROVES (the plan's B4 test list, §9 lines 492-504 of the frozen plan):

* numbering — server-assigned `INV-YYYY-NNN`, sequential, and a REAL UNIQUE-collision retry
  (the collision is forced against the live SQLite UNIQUE constraint, not a simulated exception);
* illegal status transitions refused BY NAME (draft -> paid refused; everything after void refused);
* send refuses `E_CONFIG` while sender identity / the compliance footer path is unconfigured —
  and the invoice STAYS a labelled draft (never a fake `sent`);
* webhook flip — a `invoice.payment_failed` delivery matching an invoice's `payment_link` flips
  the row to `overdue`; a delivery that matches nothing writes an activity row naming the miss
  (never silent); a replay of the same event id flips nothing twice;
* the routes — POST create draft, GET list, GET one, POST id/send, POST id/void — reachable in a
  LOCAL test app only (`main.py` is NOT wired by this seat: the parent wires the invoices router
  at settle, V9-D3; until then this router exists nowhere but this local app).

TEST DOUBLES (tests only, named at their boundary): `_compliance_footer` is stubbed where the real
`app/services/email_compliance.py` (seat b3, parallel write) is not the behaviour under test, and
`app.agents.integrations.send_email` — the provider edge — is stubbed for the success path so no
real email is ever attempted. The sandbox refusal test runs the REAL firewall path.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.errors import (
    E_CONFIG,
    E_DB,
    E_NOT_FOUND,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    PolsiaError,
)
from app.models.invoice import Invoice, InvoiceStatus
from app.models.report import ActivityLog

ISSUE = date(2026, 11, 1)
DUE = date(2026, 12, 1)
AUTH = {"X-API-Key": "test-key"}
FOOTER_TEXT = "ACME LLC · 1 Main St, Testville · unsubscribe: reply STOP · privacy: https://acme.example/privacy\nAI assistance: this message was assisted by AI."


@pytest.fixture
def api_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "api_key", "test-key")


async def _fake_footer(db) -> str:
    """The parallel-seat seam: seat b3 owns `email_compliance.footer_for_company`; this file
    stubs the CALL SITE so the invoice path's own behaviour is what is under test."""
    return FOOTER_TEXT


def _local_app(db_session):
    """A LOCAL app carrying ONLY the invoices router + the named-refusal handler (V9-D3: the
    parent wires main.py, which is where the real app-level PolsiaError handler lives too)."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from app.api.deps import as_http_exception
    from app.api.v1.invoices import router
    from app.core.database import get_db
    from app.core.errors import PolsiaError

    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_db] = lambda: db_session

    @application.exception_handler(PolsiaError)
    async def _named_refusal(request: Request, exc: PolsiaError) -> JSONResponse:
        http_exc = as_http_exception(exc)
        return JSONResponse(
            status_code=http_exc.status_code, content={"detail": http_exc.detail}, headers=http_exc.headers
        )

    return application


def _client(db_session) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_local_app(db_session)), base_url="http://test")


async def _activity(db, action: str) -> list[ActivityLog]:
    return list(
        (await db.execute(select(ActivityLog).where(ActivityLog.action == action))).scalars().all()
    )


def _payload(number: str = "INV-2026-900", link: str = "https://buy.stripe.com/test_b4flip") -> dict:
    return {
        "number": number,
        "status": InvoiceStatus.SENT,
        "issue_date": ISSUE,
        "due_date": DUE,
        "net_days": 30,
        "currency": "USD",
        "total": Decimal("250.00"),
        "payment_link": link,
        "bill_to": {"name": "ACME", "email": "billing@acme.example"},
    }


async def _seed_sent_invoice(db, **over) -> Invoice:
    invoice = Invoice(**_payload(**over))
    db.add(invoice)
    await db.flush()
    return invoice


def _failed_event(event_id: str, link: str, event_type: str = "invoice.payment_failed") -> dict:
    """A real Stripe delivery shape: the failed-payment event names the payment link."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": {"object": "invoice", "payment_link": link}},
    }


# ── numbering ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_assigns_sequential_numbers_and_null_money(async_db_session, api_key, mock_redis):
    async with _client(async_db_session) as client:
        first = await client.post(
            "/api/v1/invoices",
            json={"issue_date": ISSUE.isoformat(), "bill_to": {"name": "ACME", "email": "billing@acme.example"}},
            headers=AUTH,
        )
        second = await client.post(
            "/api/v1/invoices",
            json={"issue_date": ISSUE.isoformat(), "net_days": 15, "bill_to": {"name": "ACME", "email": "billing@acme.example"}},
            headers=AUTH,
        )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["number"] == "INV-2026-001"
    assert second.json()["number"] == "INV-2026-002"
    assert first.json()["status"] == "draft"
    # money is NOT fabricated: no subtotal/tax/total sent => null on the wire (never 0).
    # `tax` is the field this pin was MISSING (S1-04: it carried default=0 + server_default
    # '0', so it stored a fabricated measured zero — wave-v13/s1-backend FINDINGS S1-04);
    # the assertions below are the ones whose absence let that ship.
    assert first.json()["subtotal"] is None
    assert first.json()["tax"] is None
    assert first.json()["total"] is None
    assert second.json()["tax"] is None
    # net_days drives the default due date
    assert second.json()["due_date"] == (ISSUE + timedelta(days=15)).isoformat()


@pytest.mark.asyncio
async def test_number_allocation_recovers_from_a_real_unique_collision(
    async_db_session, monkeypatch, mock_redis
):
    """The retry is proven against the LIVE UNIQUE constraint: the candidate is forced to a
    number that already exists, the flush really raises IntegrityError, the savepoint rolls
    back, the number is re-derived — and the collision invoice is not the one that lands."""
    from app.services import invoice_service

    first = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    await async_db_session.commit()
    assert first.number == "INV-2026-001"

    calls = {"n": 0}
    real = invoice_service._next_number

    async def colliding_once(db, year):
        calls["n"] += 1
        if calls["n"] == 1:
            return first.number  # already taken: the DB must reject this attempt
        return await real(db, year)

    monkeypatch.setattr(invoice_service, "_next_number", colliding_once)
    second = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    await async_db_session.commit()

    assert second.number == "INV-2026-002"
    assert calls["n"] == 2, "the allocation must have been retried exactly once"
    rows = (await async_db_session.execute(select(Invoice))).scalars().all()
    assert sorted(row.number for row in rows) == ["INV-2026-001", "INV-2026-002"]


@pytest.mark.asyncio
async def test_number_allocation_refuses_by_name_when_every_attempt_collides(
    async_db_session, monkeypatch, mock_redis
):
    from app.services import invoice_service

    first = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    await async_db_session.commit()

    async def always_taken(db, year):
        return first.number

    monkeypatch.setattr(invoice_service, "_next_number", always_taken)
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    assert ei.value.code == E_DB
    assert "unique" in ei.value.message.lower()
    assert ei.value.how_to_fix
    # nothing half-written: the losing attempt left no row behind
    rows = (await async_db_session.execute(select(Invoice))).scalars().all()
    assert [row.number for row in rows] == ["INV-2026-001"]


# ── status transitions ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_to_paid_is_refused_by_name(async_db_session, mock_redis):
    from app.services import invoice_service

    invoice = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.transition_invoice(async_db_session, invoice, "paid")
    assert ei.value.code == E_VALIDATION
    assert "draft" in ei.value.message and "paid" in ei.value.message
    assert "sent" in ei.value.message and "void" in ei.value.message  # the legal targets are named
    await async_db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.DRAFT


@pytest.mark.asyncio
async def test_paid_is_terminal_and_sets_paid_at(async_db_session, mock_redis):
    from app.services import invoice_service

    invoice = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    await invoice_service.transition_invoice(async_db_session, invoice, "sent")
    await invoice_service.transition_invoice(async_db_session, invoice, "paid")
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.paid_at is not None, "entering paid records WHEN payment was recorded"
    for target in ("void", "overdue", "sent"):
        with pytest.raises(PolsiaError) as ei:
            await invoice_service.transition_invoice(async_db_session, invoice, target)
        assert ei.value.code == E_VALIDATION
        assert "paid" in ei.value.message


@pytest.mark.asyncio
async def test_unknown_status_value_is_refused_by_name(async_db_session, mock_redis):
    from app.services import invoice_service

    invoice = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.transition_invoice(async_db_session, invoice, "collected")
    assert ei.value.code == E_VALIDATION
    assert "collected" in ei.value.message


@pytest.mark.asyncio
async def test_void_route_then_void_again_is_refused(async_db_session, api_key, mock_redis):
    async with _client(async_db_session) as client:
        created = await client.post(
            "/api/v1/invoices", json={"issue_date": ISSUE.isoformat()}, headers=AUTH
        )
        invoice_id = created.json()["id"]
        voided = await client.post(
            f"/api/v1/invoices/{invoice_id}/void", json={"reason": "issued in error"}, headers=AUTH
        )
        assert voided.status_code == 200, voided.text
        assert voided.json()["status"] == "void"
        again = await client.post(f"/api/v1/invoices/{invoice_id}/void", json={}, headers=AUTH)
        assert again.status_code == 422
        assert again.json()["detail"]["code"] == E_VALIDATION


# ── send: refusals by name, then the real composition/delivery path ─────────────────────────


@pytest.mark.asyncio
async def test_send_refuses_by_name_without_compliance_config_and_stays_draft(
    async_db_session, api_key, mock_redis
):
    """The b3 posture: with sender identity / the compliance footer unconfigured (or the
    module absent — b3 writes it in parallel), a real send is REFUSED by name, never sent."""
    async with _client(async_db_session) as client:
        created = await client.post(
            "/api/v1/invoices",
            json={
                "issue_date": ISSUE.isoformat(),
                "bill_to": {"name": "ACME", "email": "billing@acme.example"},
                "payment_link": "https://buy.stripe.com/test_b4send",
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        invoice_id = created.json()["id"]
        resp = await client.post(f"/api/v1/invoices/{invoice_id}/send", json={}, headers=AUTH)
        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == E_CONFIG
        assert detail["how_to_fix"]
        after = await client.get(f"/api/v1/invoices/{invoice_id}", headers=AUTH)
        assert after.json()["status"] == "draft", "a refused send is never a fake 'sent'"


@pytest.mark.asyncio
async def test_send_in_sandbox_is_refused_by_name_and_stays_draft(
    async_db_session, monkeypatch, mock_redis
):
    """The firewall leg: sandbox mode refuses the external send (E_SANDBOX_BLOCKED)."""
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(invoice_service, "_compliance_footer", _fake_footer)

    invoice = await invoice_service.create_invoice(
        async_db_session,
        issue_date=ISSUE,
        bill_to={"name": "ACME", "email": "billing@acme.example"},
        payment_link="https://buy.stripe.com/test_b4send",
    )
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.send_invoice(async_db_session, invoice.id)
    assert ei.value.code == E_SANDBOX_BLOCKED
    await async_db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.DRAFT


@pytest.mark.asyncio
async def test_send_marks_sent_only_on_a_real_delivery_and_carries_the_footer(
    async_db_session, monkeypatch, mock_redis
):
    """THE REAL cross-seat integration: b3's `email_compliance.footer_for_company` builds the
    footer from the seeded company row — no footer seam stubbed in this test."""
    from app.agents import integrations
    from app.config import settings
    from app.models.company import CompanyConfig
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)
    async_db_session.add(
        CompanyConfig(
            name="ACME",
            sender_legal_name="ACME LLC",
            sender_postal_address="1 Main St, Testville",
            website_url="https://acme.example",
        )
    )
    await async_db_session.flush()
    calls: list[dict] = []

    def fake_send(*, to: str, subject: str, body: str) -> dict:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-msg-1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send)

    invoice = await invoice_service.create_invoice(
        async_db_session,
        issue_date=ISSUE,
        bill_to={"name": "ACME", "email": "billing@acme.example"},
        payment_link="https://buy.stripe.com/test_b4send",
        total=Decimal("120.00"),
    )
    sent = await invoice_service.send_invoice(
        async_db_session, invoice.id, cover_note="Here is your November invoice."
    )
    assert sent.status is InvoiceStatus.SENT
    (call,) = calls
    assert call["to"] == "billing@acme.example"
    assert invoice.number in call["subject"]
    assert "Here is your November invoice." in call["body"]
    assert "https://buy.stripe.com/test_b4send" in call["body"]
    # the REAL compliance footer from b3's module, built off the seeded company row:
    from app.services.company_service import get_company
    from app.services.email_compliance import footer_for_company

    company = await get_company(async_db_session)
    expected_footer = footer_for_company(company)
    assert "ACME LLC" in call["body"] and "1 Main St, Testville" in call["body"]
    assert "To opt out:" in call["body"]
    assert "https://acme.example/privacy" in call["body"]
    assert call["body"].rstrip().endswith(expected_footer.rstrip()), "the footer closes the message"
    sends = await _activity(async_db_session, "invoice_sent")
    assert len(sends) == 1
    assert sends[0].detail["invoice_id"] == invoice.id
    assert sends[0].detail["message_id"] == "sg-msg-1"

    # only a draft can be sent: a second send is refused by name
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.send_invoice(async_db_session, invoice.id)
    assert ei.value.code == E_VALIDATION
    assert "draft" in ei.value.message


@pytest.mark.asyncio
async def test_send_without_a_bill_to_email_is_refused_by_name(
    async_db_session, monkeypatch, mock_redis
):
    from app.config import settings
    from app.services import invoice_service

    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.setattr(invoice_service, "_compliance_footer", _fake_footer)
    invoice = await invoice_service.create_invoice(async_db_session, issue_date=ISSUE)
    with pytest.raises(PolsiaError) as ei:
        await invoice_service.send_invoice(async_db_session, invoice.id)
    assert ei.value.code == E_VALIDATION
    assert "email" in ei.value.message


# ── webhook flip ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_payment_failed_flips_the_matching_invoice_to_overdue(async_db_session, mock_redis):
    from app.services.finance_service import record_stripe_event

    link = "https://buy.stripe.com/test_b4flip"
    invoice = await _seed_sent_invoice(async_db_session, link=link)
    await async_db_session.commit()

    await record_stripe_event(
        async_db_session,
        stripe_event_id="evt_b4_flip",
        event_type="invoice.payment_failed",
        raw_payload=_failed_event("evt_b4_flip", link),
    )
    await async_db_session.commit()

    await async_db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.OVERDUE
    ledger = await _activity(async_db_session, "invoice_payment_failed")
    assert len(ledger) == 1
    assert ledger[0].detail["invoice_id"] == invoice.id
    assert ledger[0].detail["event_id"] == "evt_b4_flip"
    assert ledger[0].detail["from_status"] == "sent"


@pytest.mark.asyncio
async def test_payment_failed_with_no_match_writes_a_miss_row_never_silent(
    async_db_session, mock_redis
):
    from app.services.finance_service import record_stripe_event

    invoice = await _seed_sent_invoice(async_db_session)
    await async_db_session.commit()

    await record_stripe_event(
        async_db_session,
        stripe_event_id="evt_b4_miss",
        event_type="invoice.payment_failed",
        raw_payload=_failed_event("evt_b4_miss", "https://buy.stripe.com/some_other_link"),
    )
    await async_db_session.commit()

    await async_db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.SENT, "no match => no flip"
    missed = await _activity(async_db_session, "invoice_payment_failed_unmatched")
    assert len(missed) == 1
    assert missed[0].detail["event_id"] == "evt_b4_miss"
    assert missed[0].detail["candidates"] == ["https://buy.stripe.com/some_other_link"]


@pytest.mark.asyncio
async def test_payment_failed_replay_flips_nothing_twice(async_db_session, mock_redis):
    """The flip rides the CREATE leg only: a replayed delivery writes no new ledger rows."""
    from app.services.finance_service import record_stripe_event

    link = "https://buy.stripe.com/test_b4replay"
    await _seed_sent_invoice(async_db_session, number="INV-2026-901", link=link)
    await async_db_session.commit()

    event = _failed_event("evt_b4_replay", link)
    for _ in range(2):
        await record_stripe_event(
            async_db_session,
            stripe_event_id="evt_b4_replay",
            event_type="invoice.payment_failed",
            raw_payload=event,
        )
        await async_db_session.commit()

    assert len(await _activity(async_db_session, "invoice_payment_failed")) == 1
    assert len(await _activity(async_db_session, "invoice_payment_failed_unmatched")) == 0


@pytest.mark.asyncio
async def test_unrelated_event_types_do_not_touch_invoices(async_db_session, mock_redis):
    """Additive only: a delivery that is not a failed payment leaves invoices and the feed alone."""
    from app.services.finance_service import record_stripe_event

    link = "https://buy.stripe.com/test_b4flip"
    invoice = await _seed_sent_invoice(async_db_session, link=link)
    await async_db_session.commit()

    await record_stripe_event(
        async_db_session,
        stripe_event_id="evt_b4_ok",
        event_type="payment_intent.succeeded",
        raw_payload={"id": "evt_b4_ok", "type": "payment_intent.succeeded",
                     "data": {"object": {"object": "payment_intent", "payment_link": link}}},
    )
    await async_db_session.commit()

    await async_db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.SENT
    assert await _activity(async_db_session, "invoice_payment_failed") == []
    assert await _activity(async_db_session, "invoice_payment_failed_unmatched") == []


# ── routes: list / get / auth ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_get_and_not_found(async_db_session, api_key, mock_redis):
    async with _client(async_db_session) as client:
        for _ in range(2):
            resp = await client.post(
                "/api/v1/invoices", json={"issue_date": ISSUE.isoformat()}, headers=AUTH
            )
            assert resp.status_code == 201, resp.text
        listed = await client.get("/api/v1/invoices", headers=AUTH)
        assert listed.status_code == 200
        numbers = [row["number"] for row in listed.json()]
        assert numbers == ["INV-2026-002", "INV-2026-001"]  # newest-first (id desc) pins here too
        one = await client.get("/api/v1/invoices/1", headers=AUTH)
        assert one.status_code == 200 and one.json()["number"] == "INV-2026-001"
        missing = await client.get("/api/v1/invoices/999", headers=AUTH)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == E_NOT_FOUND
        unauthenticated = await client.get("/api/v1/invoices")
        assert unauthenticated.status_code == 401
