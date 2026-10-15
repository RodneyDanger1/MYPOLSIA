"""V12/W-C LOW triage — dunning fixes: F12 (company-local self-gate), F16 (ledger read once
per tick), F20 (honest missing-currency note).

RED-first: every leg here failed on the pre-fix tree (lane raw/red-*.txt; re-runnable via raw/redleg.sh).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import app.models  # noqa: F401  (register every table before create_all)
import celery_app.tasks.dunning as dunning
from app.models.invoice import Invoice, InvoiceStatus

FROZEN_NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _invoice(**overrides):
    fields = dict(
        number="INV-V12",
        status=InvoiceStatus.SENT,
        issue_date=date(2026, 10, 1),
        due_date=date(2026, 11, 10),
        currency="USD",
        total=Decimal("250.00"),
        payment_link="https://buy.stripe.com/test_x",
    )
    fields.update(overrides)
    return Invoice(**fields)


# ── F20 — missing currency prints an honest note, never "None" ──────────────────────────────
def test_f20_missing_currency_prints_an_honest_note_not_None():
    subject, body = dunning.compose_dunning_email(
        _invoice(currency=None), dunning.DUNNING_STEPS[1]
    )
    assert "None" not in subject and "None" not in body
    assert "(currency not recorded)" in body
    assert "250.00" in body


# ── F16 — the dunning ledger is read ONCE per tick, not once per invoice ────────────────────
def test_f16_dunning_ledger_is_read_once_per_tick(queue_db, monkeypatch):
    monkeypatch.setattr(dunning, "company_tz", lambda: ZoneInfo("UTC"))
    monkeypatch.setattr(dunning, "now_utc", lambda: FROZEN_NOON)

    async def _seed():
        async with queue_db.factory() as db:
            for i in range(3):
                db.add(_invoice(number=f"INV-F16-{i}"))
            await db.commit()

    queue_db.run(_seed())

    calls = {"n": 0}
    real = dunning._sent_steps

    async def spy(db, **kwargs):
        calls["n"] += 1
        return await real(db, **kwargs)

    monkeypatch.setattr(dunning, "_sent_steps", spy)
    queue_db.run(dunning._run_dunning(now=FROZEN_NOON))
    assert calls["n"] == 1, (
        f"ledger re-read per invoice (O(open x ledger)); saw {calls['n']} reads for 3 open invoices"
    )


# ── F12 — the ladder self-gates on the COMPANY-LOCAL hour like the cycles ───────────────────
def test_f12_dunning_self_gates_on_the_company_local_hour(queue_db, monkeypatch):
    monkeypatch.setattr(dunning, "company_tz", lambda: ZoneInfo("UTC"))
    frozen = {"now": datetime(2026, 11, 10, 6, 0, tzinfo=UTC)}  # 06:00 local: not the gate hour
    monkeypatch.setattr(dunning, "now_utc", lambda: frozen["now"])

    off = dunning.run_dunning_ladder()
    assert off["status"] == "off", f"a 06:00-local tick must self-gate out, got {off!r}"
    assert off["company_hour"] == 6
    assert off["self_gate_hour"] == dunning.DUNNING_LOCAL_HOUR

    frozen["now"] = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)  # 08:00 local: the gate hour
    ran = dunning.run_dunning_ladder()
    assert ran["status"] != "off", "08:00 company-local must reach the real tick"
