"""V18 / L3 proof 3 — the dunning ladder advances FORWARDS ONLY, end to end on a real DB.

The V17 L3-01 fix made `detail.superseded` count as CONSUMED: the catch-up rule fires the LATEST
owed rung and declares the earlier ones superseded, so before the fix the NEXT tick walked the
ladder BACKWARDS through exactly the rungs it had just skipped (a "please pay by the due date"
reminder arriving after the +5 notice). This file proves the shipped behaviour on Postgres 16
through the REAL tick function (`dunning._run_dunning`, the task body; `now` is the module's own
frozen-clock seam, not a mock):

* a LATE-START invoice (first tick 40 days past due) fires the +30 rung immediately — one email,
  every earlier rung named `superseded`, and a second tick re-attempts the SAME rung, never an
  earlier one;
* an invoice whose +5 rung was delivered fires NOTHING at +6 (the pre-fix walk would fire `due`),
  then advances +15 -> +30 as the clock moves: strictly increasing, never backwards.

The provider is the one edge this environment cannot reach (no SendGrid credentials, and a live
send is forbidden on this host), so `deliver_invoice_email` refuses BY NAME here — the tick's
rung CHOICE and its consumption are the subject. The delivered-ledger rows the successful path
would write are seeded byte-identically to `dunning.py`'s writer (same action + detail keys);
that is a fixture of real ledger state, not a mocked engine.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, make_url, select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

DUNNING_ACTION = "invoice_dunning"
CLAIM_ACTION = "invoice_dunning_claim"
REFUSAL_ACTION = "invoice_dunning_refused"
ERROR_ACTION = "invoice_dunning_error"


@pytest_asyncio.fixture(loop_scope="session")
async def app_factory(integration_db, postgres_url, redis_url, monkeypatch):
    """The PROCESS session factory bound to the real Postgres container (+ a real Redis URL)."""
    from app.config import settings
    from app.core import database

    monkeypatch.setattr(settings, "database_url", postgres_url)
    monkeypatch.setattr(settings, "redis_url", redis_url)  # the tick's activity publisher
    await database.dispose_engine()
    factory = database.get_session_factory()
    engine_url = database.get_engine().url
    expected = make_url(postgres_url)
    assert (engine_url.host, engine_url.port, engine_url.database) == (
        expected.host,
        expected.port,
        expected.database,
    ), "the process engine must be bound to the test container"
    try:
        yield factory
    finally:
        await database.dispose_engine()


async def _invoice(factory, *, number: str, due: date, status: str = "sent") -> int:
    from app.models.invoice import Invoice

    async with factory() as db:
        row = Invoice(
            number=number,
            bill_to={"name": "AP", "email": "ap@example.com"},
            issue_date=due - timedelta(days=30),
            due_date=due,
            net_days=30,
            currency="USD",
            subtotal=Decimal("100.00"),
            tax=Decimal("0.00"),
            total=Decimal("100.00"),
            status=status,
            payment_link=f"https://pay.example.test/{number}",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return int(row.id)


async def _seed_delivered_rung(
    factory, *, invoice_id: int, number: str, step: str, step_date: date, superseded: list[str]
) -> None:
    """Write the ledger row the SHIPPED successful-send path writes (dunning.py: delivered row).

    Same action, same `detail` keys, same `superseded` list. This is the durable state a real
    send leaves behind; the tick under test reads it exactly as it would read its own output.
    """
    from app.models.report import ActivityLog

    async with factory() as db:
        db.add(
            ActivityLog(
                agent_type="finance",
                action=DUNNING_ACTION,
                summary=f"Dunning {step} sent for invoice {number} ({step_date.isoformat()})",
                detail={
                    "invoice_id": invoice_id,
                    "number": number,
                    "step": step,
                    "step_date": step_date.isoformat(),
                    "superseded": superseded,
                    "status_flipped_to": "overdue",
                    "message_id": f"seed-{number}-{step}",
                },
            )
        )
        await db.commit()


async def _claims(factory, invoice_id: int) -> list[dict]:
    from app.models.report import ActivityLog

    async with factory() as db:
        rows = (
            await db.execute(
                select(ActivityLog)
                .where(ActivityLog.action == CLAIM_ACTION)
                .order_by(ActivityLog.id)
            )
        ).scalars().all()
    return [
        row.detail
        for row in rows
        if isinstance(row.detail, dict) and row.detail.get("invoice_id") == invoice_id
    ]


async def _drop(factory, *invoice_ids: int) -> None:
    from app.models.invoice import Invoice
    from app.models.report import ActivityLog

    async with factory() as db:
        await db.execute(
            delete(ActivityLog).where(
                ActivityLog.action.in_((DUNNING_ACTION, CLAIM_ACTION, REFUSAL_ACTION, ERROR_ACTION))
            )
        )
        if invoice_ids:
            await db.execute(delete(Invoice).where(Invoice.id.in_(invoice_ids)))
        await db.commit()


def _at(day: date) -> datetime:
    """The tick's clock: 08:30 UTC on `day` (the ladder's own company-local hour is 8)."""
    return datetime(day.year, day.month, day.day, 8, 30, tzinfo=UTC)


async def _tick(day: date) -> dict:
    from celery_app.tasks.dunning import _run_dunning

    return await _run_dunning(now=_at(day))


def _steps_for(result: dict, invoice_id: int) -> list[str]:
    """Every rung the tick CHOSE for one invoice (sent OR refused, in choice order)."""
    return [
        entry["step"]
        for key in ("sent", "refused")
        for entry in result.get(key, [])
        if entry.get("invoice_id") == invoice_id
    ]


async def test_a_late_start_fires_the_latest_rung_only_and_never_an_earlier_one(app_factory):
    """First tick 40 days past due: +30 fires once, names its superseded rungs, and repeats."""
    from celery_app.tasks.dunning import DUNNING_STEPS, step_date

    today = date(2026, 3, 2)
    due = today - timedelta(days=40)
    owed = [step.key for step in DUNNING_STEPS if step_date(due, step) <= today]
    assert owed == ["pre_due", "due", "due_plus_5", "due_plus_15", "due_plus_30"], owed
    invoice_id = await _invoice(app_factory, number="V18-LATE-1", due=due)
    try:
        first = await _tick(today)
        assert _steps_for(first, invoice_id) == [owed[-1]], first

        claims = await _claims(app_factory, invoice_id)
        assert len(claims) == 1, claims
        claim = claims[0]
        assert claim["step"] == owed[-1] == "due_plus_30"
        assert claim["superseded"] == owed[:-1], claim
        assert step_date(due, DUNNING_STEPS[-1]) == due + timedelta(days=30)
        # The environment cannot deliver (no provider credentials), so the rung is REFUSED by
        # name and consumes nothing — the step is still the SAME one on the next tick.
        assert claim["state"] == "refused", claim
        assert claim["code"].startswith("E_"), claim

        later = await _tick(today + timedelta(days=1))
        assert _steps_for(later, invoice_id) == ["due_plus_30"], (
            "a refused rung is re-attempted as itself, never walked backwards"
        )

        # The whole attempted sequence is one rung — no earlier rung ever fired.
        attempted = _steps_for(first, invoice_id) + _steps_for(later, invoice_id)
        assert set(attempted) == {"due_plus_30"}, attempted
        offsets = {step.key: step.offset_days for step in DUNNING_STEPS}
        assert [offsets[step] for step in attempted] == [30, 30]
    finally:
        await _drop(app_factory, invoice_id)


async def test_a_delivered_rung_never_comes_back_and_the_ladder_advances_forwards(app_factory):
    """+5 delivered -> nothing at +6 (pre-fix: `due` fires) -> +15 -> +30, strictly forwards."""
    from celery_app.tasks.dunning import DUNNING_STEPS, step_date

    due = date(2026, 3, 2)
    invoice_id = await _invoice(
        app_factory, number="V18-LADDER-2", due=due, status="overdue"
    )
    # The +5 notice WAS delivered, and it superseded the pre-due and due rungs (the real shape).
    await _seed_delivered_rung(
        app_factory,
        invoice_id=invoice_id,
        number="V18-LADDER-2",
        step="due_plus_5",
        step_date=due + timedelta(days=5),
        superseded=["pre_due", "due"],
    )
    try:
        # ONE day past the delivered rung: every earlier rung is consumed, so NOTHING is owed.
        at_six = await _tick(due + timedelta(days=6))
        assert _steps_for(at_six, invoice_id) == [], (
            f"the superseded rungs must stay consumed (fired backwards: {at_six.get('refused')})"
        )

        # The clock reaches the +15 rung: the ladder advances FORWARDS.
        at_sixteen = await _tick(due + timedelta(days=16))
        assert _steps_for(at_sixteen, invoice_id) == ["due_plus_15"], at_sixteen
        claim = (await _claims(app_factory, invoice_id))[-1]
        owed_now = [s.key for s in DUNNING_STEPS if step_date(due, s) <= due + timedelta(days=16)]
        assert claim["step"] == owed_now[-1] == "due_plus_15"
        assert claim["superseded"] == [], "everything earlier is already consumed"

        # … and the +30 rung.
        at_thirty_one = await _tick(due + timedelta(days=31))
        assert _steps_for(at_thirty_one, invoice_id) == ["due_plus_30"], at_thirty_one

        # The fired sequence, in order, is strictly increasing: never an earlier rung.
        offsets = {step.key: step.offset_days for step in DUNNING_STEPS}
        sequence = (
            ["due_plus_5"]
            + _steps_for(at_six, invoice_id)
            + _steps_for(at_sixteen, invoice_id)
            + _steps_for(at_thirty_one, invoice_id)
        )
        as_offsets = [offsets[step] for step in sequence]
        assert as_offsets == sorted(as_offsets) == [5, 15, 30], sequence
    finally:
        await _drop(app_factory, invoice_id)


async def test_a_fully_walked_ladder_fires_nothing_again(app_factory):
    """Every rung delivered: the final tick is a no-op, not a second pass up the ladder."""
    due = date(2026, 3, 2)
    invoice_id = await _invoice(app_factory, number="V18-LADDER-3", due=due, status="overdue")
    for step, offset, superseded in (
        ("pre_due", -3, []),
        ("due", 0, ["pre_due"]),
        ("due_plus_5", 5, ["due"]),
        ("due_plus_15", 15, ["due_plus_5"]),
        ("due_plus_30", 30, ["due_plus_15"]),
    ):
        await _seed_delivered_rung(
            app_factory,
            invoice_id=invoice_id,
            number="V18-LADDER-3",
            step=step,
            step_date=due + timedelta(days=offset),
            superseded=superseded,
        )
    try:
        after = await _tick(due + timedelta(days=45))
        assert _steps_for(after, invoice_id) == [], after
    finally:
        await _drop(app_factory, invoice_id)
