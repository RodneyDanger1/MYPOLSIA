"""Finance contract tests — W3-refusals T6/T7: the persist seam and the agent-side rules.

These pin the states the wave keeps distinguishable: not-measured (`None`) vs measured
zero, a real snapshot WRITE vs an attempted one, and a provider event id being required
before a refund is ever requested.  (The API-side null/zero contract lives next door in
`tests/unit/api/test_finance.py`.)
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.agents.finance.agent import FinanceAgent
from app.core.errors import E_VALIDATION
from app.core.timeutil import today_utc
from app.models.finance import ExpenseRecord, RevenueSnapshot
from app.services.finance_service import persist_finance_run


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_persist_writes_snapshot_and_cents_roundtrip(
    async_db_session, api_client, auth_headers
):
    """(b) `persist_finance_run` writes; the summary round-trips the exact cents.

    `snapshot_written` is True here because the upsert really returned a row — that is
    the fact the worker reports, instead of the agent's former unconditional claim.
    """
    written = await persist_finance_run(
        async_db_session,
        {
            "snapshot": {
                "snapshot_date": today_utc().isoformat(),
                "mrr_cents": 500000,
                "arr_cents": 6000000,
                "active_subscribers": 42,
                "stripe_balance_cents": 150000,
            },
            "expenses": [],
        },
    )
    assert written["ok"] is True
    assert written["snapshot_written"] is True

    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["mrr_cents"] == 500000
    assert data["arr_cents"] == 6000000
    assert data["active_subscribers"] == 42
    assert data["stripe_balance_cents"] == 150000
    assert data["last_snapshot_date"] == today_utc().isoformat()


@pytest.mark.asyncio
async def test_persist_snapshot_is_idempotent_by_date_and_expenses_write(async_db_session):
    """(c) one run -> one snapshot row; a same-date rerun -> still one (upsert); expenses -> rows."""
    day = date.today().isoformat()
    assert await _count(async_db_session, RevenueSnapshot) == 0

    first = await persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": day, "mrr_cents": 111}, "expenses": []},
    )
    assert first["ok"] is True and first["snapshot_written"] is True
    assert await _count(async_db_session, RevenueSnapshot) == 1

    rerun = await persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": day, "mrr_cents": 222}, "expenses": []},
    )
    assert rerun["ok"] is True and rerun["snapshot_written"] is True
    assert await _count(async_db_session, RevenueSnapshot) == 1  # upsert, not a duplicate
    row = (
        await async_db_session.execute(
            select(RevenueSnapshot).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.mrr_cents == 222  # the rerun really UPDATED the row

    expenses = await persist_finance_run(
        async_db_session,
        {
            "snapshot": None,  # nothing measured -> nothing written, and no fake True
            "expenses": [
                {"category": "ads", "vendor": "Google", "amount_cents": 5000,
                 "currency": "usd", "date": day},
                # amount_cents<=0 is refused by `record_expense` BY NAME: skipped, counted,
                # not raised, and certainly not written.
                {"category": "software", "vendor": "GitHub", "amount_cents": 0,
                 "currency": "usd", "date": day},
            ],
        },
    )
    assert expenses["ok"] is True
    assert expenses["snapshot_written"] is False
    assert expenses["expenses_written"] == 1
    assert expenses["skipped_invalid"] == 1
    assert await _count(async_db_session, ExpenseRecord) == 1
    assert await _count(async_db_session, RevenueSnapshot) == 1


@pytest.mark.asyncio
async def test_failed_payment_without_event_id_refuses_refund(monkeypatch):
    """(e) T7: a fabricated `failed_payment:<i>` id must never reach a refund request."""
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({
            "summary": "one failed charge seen, refund proposed",
            "failed_payments": [
                {"amount_cents": 1500, "currency": "usd", "refund_requested": True},
            ],
        }),
    )
    out = FinanceAgent().run({}, {})

    refusals = out["refusals"]
    assert len(refusals) == 1
    error = refusals[0]["error"]
    assert error["code"] == E_VALIDATION
    assert error["message"] == (
        "failed payment #0 has no provider event_id; cannot request a refund for it"
    )
    # No refund request was built: no side-effect receipt exists and the fabricated id
    # appears nowhere in the serialised output.
    assert out["side_effects"] == []
    assert out["refunds_performed"] == 0
    assert "failed_payment:" not in json.dumps(out)


@pytest.mark.asyncio
async def test_snapshot_built_only_from_provided_fields(monkeypatch):
    """(f) nothing measured -> snapshot None + snapshot_written False + ONE naming alert;
    one field provided -> the row carries exactly that key (no zero-fill)."""
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"summary": "no revenue figures"}))
    out = FinanceAgent().run({}, {})

    assert out["snapshot"] is None
    assert out["snapshot_written"] is False
    assert len(out["alerts"]) == 1
    alert = out["alerts"][0]
    assert "no snapshot was written" in alert
    for field in (
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "churned_today",
        "new_today",
        "total_revenue_month_cents",
        "stripe_balance_cents",
    ):
        assert field in alert, (field, alert)

    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"summary": "mrr measured only", "mrr_cents": 500000}),
    )
    out = FinanceAgent().run({}, {})
    assert out["snapshot_written"] is False
    assert out["snapshot"] == {
        "snapshot_date": today_utc().isoformat(),
        "mrr_cents": 500000,
    }
    assert set(out["snapshot"]) == {"snapshot_date", "mrr_cents"}  # exactly the provided key
    assert out["alerts"] == []  # a measured run raises no unmeasured-fields alert
