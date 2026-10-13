"""FIXER-5 / L2-09 — `record_expense` applies the same currency rule `record_stripe_event` does.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l2l3.txt):
`record_expense(currency='USD')` stored `'USD'` while `record_stripe_event(currency='USD')`
stored `'usd'` — two behaviours for one CHECK family (`ck_expense_records_currency` /
`ck_stripe_events_currency`, 0002:107-108, PostgreSQL-only).  SQLite (every unit test)
accepts the uppercase value; on PG it is an `IntegrityError` inside `persist_finance_run`'s
loop, which its blanket except reports as `E_DB` and aborts the whole batch — the agent's
payload value is blamed on the database.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.core.errors import E_VALIDATION, PolsiaError
from app.models.finance import ExpenseRecord
from app.services.finance_service import persist_finance_run, record_expense, record_stripe_event


async def _records(db) -> list[ExpenseRecord]:
    return list((await db.execute(select(ExpenseRecord))).scalars().all())


def _kwargs(**overrides) -> dict:
    values = {
        "category": "software",
        "vendor": "Example",
        "amount_cents": 1000,
        "currency": "USD",
        "date": date(2026, 10, 13),
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_both_writers_store_usd_for_an_uppercase_currency(async_db_session):
    """The L2-09 probe's exact shape: the two writers must agree."""
    expense = await record_expense(async_db_session, **_kwargs())
    event = await record_stripe_event(
        async_db_session, stripe_event_id="evt_fix5_l2_09", event_type="charge.failed",
        currency="USD",
    )
    assert expense.currency == "usd"
    assert event.currency == "usd"


@pytest.mark.asyncio
async def test_currency_is_stripped_and_lowered(async_db_session):
    expense = await record_expense(async_db_session, **_kwargs(currency="  Usd  "))
    assert expense.currency == "usd"


@pytest.mark.asyncio
async def test_blank_currency_never_becomes_an_empty_string(async_db_session):
    """Blank normalises to `None` (mirroring `record_stripe_event`); the column's own
    `default="usd"` then applies, exactly as if the field had been omitted — the one thing
    that can never land is `''` (junk that the lowercase CHECK would happily pass)."""
    expense = await record_expense(async_db_session, **_kwargs(currency=""))
    assert expense.currency == "usd"


@pytest.mark.asyncio
async def test_non_text_currency_is_a_named_refusal(async_db_session):
    """Fail-closed, never `AttributeError` inside a persistence path."""
    with pytest.raises(PolsiaError) as excinfo:
        await record_expense(async_db_session, **_kwargs(currency=840))
    assert excinfo.value.code == E_VALIDATION
    assert "currency" in excinfo.value.message
    assert await _records(async_db_session) == []


@pytest.mark.asyncio
async def test_payload_path_normalizes_through_record_expense(async_db_session):
    """`persist_finance_run`'s expense loop crosses the same normalisation (single point)."""
    report = await persist_finance_run(
        async_db_session,
        {
            "snapshot": None,
            "expenses": [
                {"category": "ads", "vendor": "Google", "amount_cents": 5000,
                 "currency": "USD", "date": "2026-10-13"},
            ],
        },
    )
    assert report["ok"] is True
    assert report["expenses_written"] == 1
    (row,) = await _records(async_db_session)
    assert row.currency == "usd"


@pytest.mark.asyncio
async def test_omitted_currency_keeps_the_documented_default(async_db_session):
    """Control: no currency supplied -> the column's own default, never NULL-with-meaning."""
    values = _kwargs()
    values.pop("currency")
    expense = await record_expense(async_db_session, **values)
    assert expense.currency == "usd"
