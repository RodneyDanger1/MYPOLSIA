"""V12/W-C LOW triage — finance fixes: F7 (one currency normalizer), F21 (tolerance floor),
F22 (persist savepoint), F10-planning (break_even/margin through Decimal).

RED-first: every leg here failed on the pre-fix tree (lane raw/red-*.txt; re-runnable via raw/redleg.sh).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import date
from decimal import Decimal

import pytest

from app.api.v1 import finance as finance_api
from app.services import finance_service


# ── F7 — currency normalization shared by both writers ──────────────────────────────────────
@pytest.mark.asyncio
async def test_f7_currency_is_normalized_identically_by_both_writers(async_db_session):
    """`" USD "` must store as `"usd"` whichever writer crosses it (the PG CHECK family)."""
    event = await finance_service.record_stripe_event(
        async_db_session, "evt_f7_1", "invoice.paid", currency=" USD "
    )
    assert event is not None and event.currency == "usd"

    expense = await finance_service.record_expense(
        async_db_session,
        amount_cents=250,
        currency=" USD ",
        category="ads",
        vendor="Google",
        date=date(2026, 10, 13),
    )
    assert expense.currency == "usd"

    # blank -> "not recorded": the STRIPE writer stores NULL (no column default) ...
    blank_event = await finance_service.record_stripe_event(
        async_db_session, "evt_f7_2", "invoice.paid", currency="   "
    )
    assert blank_event is not None and blank_event.currency is None
    # ... and the shared normalizer answers None before any model default (the expense
    # column's own documented default="usd" then applies on insert).
    assert finance_service._normalize_currency("   ") is None


# ── F21 — a CONFIGURED non-positive tolerance is the default, never "off" (V12/V1) ──────────
def test_f21_zero_tolerance_falls_back_to_the_default(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(finance_api, "settings", SimpleNamespace(stripe_signature_tolerance=0))
    assert (
        finance_api._signature_tolerance() == finance_api.DEFAULT_SIGNATURE_TOLERANCE
    ), "configured 0 must not silently disable the replay guard"

    secret = "whsec_test"
    ts = int(time.time()) - 4000  # far outside the 300 s default
    body = b'{"id": "evt"}'
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    # V12/V1 reconciliation: the floor lives at the ENTRY POINT — `construct_stripe_event`
    # normalizes an explicit 0 away BEFORE either verifier sees it.  The low-level
    # `_stdlib_verify(..., 0)` seam keeps its SHIPPED R28/R32 escape-hatch semantics (a falsy
    # tolerance skips the age check, exactly like the installed stripe SDK) — see
    # tests/unit/test_r28_finance_tasks_sweep.py::test_stdlib_verify_tolerance_zero_disables_the_window.
    with pytest.raises(finance_api.HTTPException) as excinfo:
        finance_api.construct_stripe_event(body, f"t={ts},v1={sig}", secret, tolerance=0)
    assert "tolerance" in str(excinfo.value.detail)


# ── F22 — a mid-batch failure must not leave partial rows in the caller's transaction ───────
@pytest.mark.asyncio
async def test_f22_mid_batch_failure_rolls_this_persists_writes_back(async_db_session, monkeypatch):
    from sqlalchemy import func, select

    from app.models.finance import ExpenseRecord

    calls = {"n": 0}
    real = finance_service.record_expense

    async def flaky(db, **values):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("mid-batch boom (injected)")
        return await real(db, **values)

    monkeypatch.setattr(finance_service, "record_expense", flaky)
    result = await finance_service.persist_finance_run(
        async_db_session,
        {
            "expenses": [
                {"category": "software", "vendor": "ACME", "date": "2026-10-13",
                 "amount_cents": 111, "currency": "usd"},
                {"category": "software", "vendor": "ACME", "date": "2026-10-13",
                 "amount_cents": 222, "currency": "usd"},
            ]
        },
    )
    assert result["ok"] is False and result["error"]["code"]
    count = int(
        (await async_db_session.execute(select(func.count()).select_from(ExpenseRecord))).scalar_one()
    )
    assert count == 0, "a mid-batch failure must not leave partially persisted rows (F22)"


# ── F10 (planning seam) — exact decimal math at the boundaries ───────────────────────────────
def test_f10_break_even_and_margin_are_exact_at_the_float_boundary():
    """21 / 0.7 is exactly 30 clients; the binary-float division answered 30.000000000000004."""
    assert finance_api.break_even_clients(21, "0.7", 0, 0, 0) == 30
    exact = float(Decimal("0.1") / Decimal("0.3"))
    assert finance_api.margin_fraction("0.3", "0.2", 0, 0) == exact
    # shipped shapes stay intact
    assert finance_api.margin_fraction(1000, 100, 10, 30) == 0.6
    assert finance_api.break_even_clients(600, 1000, 100, 10, 30) == 1
