"""V12/W-C — finance/data-family PINS (xfail, defect + why-it-stays named per test).

F8  [spec] — two-way substring link matching can flip the WRONG invoice.
F9         — absent-day zero in the ai_spend read block (deliberate R24/D3 pin on the other side).
F10 (display) — report cash rendering crosses binary floats (planning seam FIXED separately).
F11 (systemic) — SQLite stores Numeric as REAL; exact-at-rest holds on PostgreSQL only.
F13        — research fetch budget is post-hoc/self-reported; enforcement is tool-layer territory.
F14 [spec] — candidate price_band rebuilt through float() (wire-type change crosses the UI).
F15 [spec] — single-use confirm guard relies on with_for_update(), a no-op on SQLite.
F18        — economics failures never cached / no backoff (documented intent).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

import app.models  # noqa: F401


# ── F8 ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="F8 [spec]: candidate->stored containment matching (both directions, >=8 chars) can "
    "cross-match two links sharing a long token and flip the WRONG invoice; exact-first matching "
    "is a product call (flip is create-leg-only)",
)
async def test_f8_shared_substring_links_must_not_cross_match(async_db_session, mock_redis):
    from app.models.invoice import Invoice, InvoiceStatus
    from app.services.finance_service import apply_failed_payment_to_invoices

    exact = Invoice(
        number="INV-F8-E", status=InvoiceStatus.SENT, issue_date=date(2026, 10, 1),
        due_date=date(2026, 11, 1), currency="USD", total=Decimal("10.00"),
        payment_link="https://buy.stripe.com/test_abc123",
    )
    promo = Invoice(
        number="INV-F8-P", status=InvoiceStatus.SENT, issue_date=date(2026, 10, 1),
        due_date=date(2026, 11, 1), currency="USD", total=Decimal("10.00"),
        payment_link="https://buy.stripe.com/test_abc123-promo",
    )
    async_db_session.add_all([exact, promo])
    await async_db_session.flush()

    await apply_failed_payment_to_invoices(
        async_db_session,
        event_id="evt_f8",
        event_type="invoice.payment_failed",
        payload={"data": {"object": {"payment_link": "https://buy.stripe.com/test_abc123"}}},
    )
    await async_db_session.flush()
    assert exact.status == InvoiceStatus.OVERDUE
    assert promo.status == InvoiceStatus.SENT, (
        "the '-promo' link only CONTAINS the named link — it must not flip"
    )


# ── F9 ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="F9: the absent-day $0.00 in the ai_spend READ block collapses 'nothing measured' "
    "into a fake zero (F-08/F-10 class); kept as-is because the R24 wire test "
    "(test_finance_ai_spend.py:146) pins the 0.0 as deliberate D3 gate parity — flipping the "
    "read is a parent contract call",
)
async def test_f9_absent_day_must_not_read_as_a_measured_zero(api_client, auth_headers):
    body = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()
    ai = body["ai_spend"]
    assert ai is not None
    assert ai["spent"] is None, f"no agent_runs rows -> unmeasured, not ${ai['spent']}"


# ── F10 (display seam) ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="F10 (display): report cash rendering crosses binary floats "
    "(round(float(cents)/100, 2) at report_service.py:204) — exact only below 2^53; "
    "quantized-Decimal rendering is a report-surface contract change",
)
async def test_f10_report_cash_rendering_is_exact_at_the_float_boundary(async_db_session):
    from app.models.finance import RevenueSnapshot
    from app.services.report_service import build_cash_block

    async_db_session.add(
        RevenueSnapshot(snapshot_date=date(2026, 10, 14), stripe_balance_cents=2**53 + 1)
    )
    await async_db_session.flush()
    block = await build_cash_block(async_db_session)
    assert block["cash_on_hand"] == 90071992547409.93


# ── F11 ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(
    strict=False,
    reason="F11 (systemic): SQLite stores SQLAlchemy Numeric as REAL — 'money exact at rest' "
    "holds on PostgreSQL only; the deployment decision (Postgres in prod / integer cents on "
    "SQLite) is the parent's",
)
def test_f11_sqlite_numeric_money_drifts_under_sum(queue_db):
    """stdlib read (the house rule): the ORM result processor masks the drift at scale 10;
    the raw file carries the REAL-summed float."""
    from app.models.invoice import Invoice, InvoiceStatus

    async def _seed():
        async with queue_db.factory() as db:
            for i in range(30):
                db.add(
                    Invoice(
                        number=f"INV-F11-{i}", status=InvoiceStatus.SENT,
                        issue_date=date(2026, 10, 1), due_date=date(2026, 11, 1),
                        currency="USD", total=Decimal("33.33"), subtotal=Decimal("33.33"),
                    )
                )
            await db.commit()

    queue_db.run(_seed())
    rows = queue_db.query("SELECT total FROM invoices")
    total = 0.0
    for row in rows:
        total += float(row["total"])  # the codebase's own float-sum seam (e.g. `sum(costs)`)
    assert Decimal(str(total)) == Decimal("999.90"), (
        f"30 x 33.33 summed to {total!r} on SQLite Numeric->REAL (raw reads, float-summed)"
    )


# ── F13 ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(
    strict=False,
    reason="F13: the research fetch budget is post-hoc and model-self-reported (code-gap #11); "
    "hard enforcement belongs at the capability/tool layer — W-A/W-B runtime files, "
    "off-territory for W-C",
)
def test_f13_the_research_budget_must_hard_stop_at_the_capability_layer():
    from app.agents.competitor_research import agent as research_agent

    enforce = getattr(research_agent, "enforce_research_budget", None)
    assert callable(enforce), (
        "no tool-layer fetch counter/stop exists: over-budget runs are only RECORDED "
        "(research_budget_violation -> 'no hard enforcement')"
    )


# ── F14 ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(
    strict=False,
    reason="F14 [spec]: the stored candidate's price_band is rebuilt through float() "
    "(_assemble_candidate) — kept because the wire-type change (str/Decimal) reaches the UI "
    "contract the planner renders; representation exactness is a parent call",
)
def test_f14_candidate_price_band_must_not_pass_through_binary_floats():
    from app.services import planner_service

    fn = getattr(planner_service, "_assemble_candidate", None)
    assert callable(fn), "candidate assembly entry point moved — re-anchor this pin"
    candidate = fn(
        {
            "name": "t", "one_liner": "o", "buyer": "b",
            "price_band": [Decimal("0.1"), Decimal("0.3")],
            "channel": "c", "unit_cost_sketch": "s",
        },
        owner_hours_per_week=None,
        owner_budget_usd=None,
        fetched_urls=set(),
        fetched_at="2026-10-14T00:00:00Z",
    )
    band = candidate["price_band"]
    assert not any(isinstance(value, float) for value in band), (
        f"price_band crossed binary floats: {band!r}"
    )


# ── F15 ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.xfail(
    strict=False,
    reason="F15 [spec]: the single-use confirm guard relies on with_for_update(), a no-op on "
    "SQLite — two consumers can both read state='pending'; the conditional-UPDATE guard is a "
    "chat-service change for the parent",
)
def test_f15_sqlite_for_update_does_not_exclude_a_second_consumer(queue_db):
    from sqlalchemy import select

    from app.models.chat import ChatMessage

    async def _seed():
        async with queue_db.factory() as db:
            row = ChatMessage(
                role="polsia",
                content="seed",
                intent={"kind": "create_task", "state": "pending", "plan": {}},
            )
            db.add(row)
            await db.commit()
            return int(row.id)

    row_id = queue_db.run(_seed())

    async def _race():
        factory = queue_db.factory()
        async with factory() as first, factory() as second:
            held = (
                await first.execute(
                    select(ChatMessage).where(ChatMessage.id == row_id).with_for_update()
                )
            ).scalar_one()
            held.intent = {**held.intent, "state": "consumed"}
            await first.flush()  # the consume write is UNCOMMITTED
            rival = (
                await second.execute(
                    select(ChatMessage).where(ChatMessage.id == row_id).with_for_update()
                )
            ).scalar_one()
            state = rival.intent["state"]
            await first.rollback()
            return state

    second_state = queue_db.run(_race())
    assert second_state == "consumed", (
        "FOR UPDATE must exclude a second consumer; SQLite returned the stale 'pending' — "
        "two confirms can pass the guard"
    )


# ── F18 ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=False,
    reason="F18: economics failures are never cached and have no backoff — documented intent "
    "(module docstring :38-39: a recovered source shows up immediately); adding a negative "
    "cache/backoff is a policy trade for the parent",
)
async def test_f18_a_down_source_must_not_be_refetched_on_every_request():
    from app.services import economics_service

    economics_service._CACHE.pop("fx", None)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("source down (injected)")

    await economics_service._serve_section("fx", boom)
    await economics_service._serve_section("fx", boom)
    assert calls["n"] == 1, (
        f"a failing source was re-fetched on every request ({calls['n']} calls): no negative "
        "cache/backoff"
    )
