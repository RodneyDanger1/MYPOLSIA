"""W4 / LIVE-3 — a snapshot row is a PROVENANCE claim: no measurement, no row.

THE DEFECT under test.  `persist_finance_run` wrote whatever `result["snapshot"]` held.  A payload
that named a date but measured NOTHING (`{"snapshot": {"snapshot_date": "2026-10-28"}}`) therefore
created a `revenue_snapshots` row whose seven money/count columns are all NULL and reported
`snapshot_written: True`.  Nothing numeric was invented — but row EXISTENCE is itself a provenance
signal that the read paths trust:

  * `GET /finance/summary` serves `last_snapshot_date` from the newest row, so a run that measured
    nothing made the product say a day had been measured;
  * `build_cash_block`'s reason for an absent balance is "no revenue snapshot has been recorded
    yet" — the row silences it;
  * the finance page leaves its "not started yet" idle state as soon as a snapshot date exists.

That is the same state confusion LIVE-3 found in the live database (a legacy all-zero snapshot
served as a measurement), one layer up: an unmeasured day wearing a measurement's shape.  The
2026-10-20 legacy row is the historical instance; this test pins the writer half so the class
cannot be re-created by any later caller.

THE CONTRACT under test:
  * a snapshot dict with NO measured column writes NO row (`snapshot_written: False`, and the
    skip is NAMED — `snapshot_skipped: "unmeasured"` — never silently dropped);
  * a MEASURED ZERO is a measurement: `{"active_subscribers": 0}` still writes, and the summary
    serves `0` (never `None`), because "measured nobody" is a reading;
  * a measurement-less retry neither creates a row nor erases a same-day measurement.

RED leg (recorded in REPORT.md): the first test below fails on the current tree with
`snapshot_written: True` and one row stored for the date.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.finance import RevenueSnapshot
from app.schemas.agent_outputs import placeholder_payload
from app.services.finance_service import get_finance_summary, persist_finance_run

DAY = date(2026, 10, 28)


async def _rows(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(RevenueSnapshot)) or 0)


async def _snapshot(db) -> RevenueSnapshot | None:
    db.expunge_all()
    return (
        await db.execute(select(RevenueSnapshot).where(RevenueSnapshot.snapshot_date == DAY))
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_a_snapshot_that_measured_nothing_writes_no_row(async_db_session):
    """LIVE-3's writer half: a date-only snapshot is not a measurement and must not be stored."""
    report = await persist_finance_run(
        async_db_session, {"snapshot": {"snapshot_date": DAY.isoformat()}}
    )

    assert report["ok"] is True, report
    assert report["snapshot_written"] is False, (
        "a snapshot that measured no column reported snapshot_written=True; "
        "a row with a date and seven NULLs claims a day was measured"
    )
    assert report.get("snapshot_skipped") == "unmeasured", (
        f"the skip must be NAMED, not silent; report={report!r}"
    )
    assert await _rows(async_db_session) == 0, (
        "an all-NULL snapshot row was stored; row existence is the provenance signal the "
        "summary, the cash block and the finance page read"
    )

    summary = await get_finance_summary(async_db_session, month=DAY)
    assert summary["last_snapshot_date"] is None, (
        f"the summary reported a snapshot date ({summary['last_snapshot_date']}) for a run that "
        "measured nothing"
    )
    for key in (
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "stripe_balance_cents",
        "total_revenue_month_cents",
    ):
        assert summary[key] is None, f"{key} must stay unknown, got {summary[key]!r}"


@pytest.mark.asyncio
async def test_a_measured_zero_still_writes_and_is_served_as_zero(async_db_session):
    """The guard's other side: "measured nobody" is a reading and must survive as 0, never None."""
    report = await persist_finance_run(
        async_db_session,
        {
            "snapshot": {
                "snapshot_date": DAY.isoformat(),
                "active_subscribers": 0,  # measured: no subscriber
                "stripe_balance_cents": 0,  # measured: empty Stripe balance
            }
        },
    )

    assert report["ok"] is True, report
    assert report["snapshot_written"] is True, (
        "a supplied 0 is a measured zero; refusing to write it would be the same state "
        f"confusion in the other direction; report={report!r}"
    )
    row = await _snapshot(async_db_session)
    assert row is not None and row.active_subscribers == 0
    assert row.stripe_balance_cents == 0
    assert row.mrr_cents is None, "a column the run never supplied stays unmeasured"

    summary = await get_finance_summary(async_db_session, month=DAY)
    assert summary["active_subscribers"] == 0
    assert summary["stripe_balance_cents"] == 0
    assert summary["mrr_cents"] is None


@pytest.mark.asyncio
async def test_an_unmeasured_retry_does_not_create_a_row_and_does_not_erase_one(
    async_db_session,
):
    """A same-day measurement-less retry is a no-op — it neither invents nor erases a reading."""
    await persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat(), "mrr_cents": 250_000}},
    )
    retry = await persist_finance_run(
        async_db_session, {"snapshot": {"snapshot_date": DAY.isoformat()}}
    )

    assert retry["ok"] is True, retry
    assert retry["snapshot_written"] is False, retry
    assert await _rows(async_db_session) == 1
    row = await _snapshot(async_db_session)
    assert row is not None and row.mrr_cents == 250_000, (
        "the retry measured nothing; the earlier same-day measurement must survive"
    )


@pytest.mark.asyncio
async def test_a_placeholder_finance_run_persists_no_snapshot(async_db_session):
    """LIVE-2 end-to-end at the persistence seam: the mock payload writes no revenue_snapshot.

    The input is `placeholder_payload` (the sanctioned CLAUDE_CLI_MOCK shape; its own unit test is
    `test_f2_placeholder_truth.py`).  This asserts the WRITE half: whatever the placeholder carries,
    `revenue_snapshots` stays empty and the summary cannot absorb fabricated zeros.
    """
    payload = placeholder_payload(
        "finance",
        "Mock Claude response for testing",
        "CLAUDE_CLI_MOCK is on: the mock result was not JSON, so no structured output exists. "
        "Nothing was performed.",
    )

    report = await persist_finance_run(async_db_session, payload)

    assert report["ok"] is True, report
    assert await _rows(async_db_session) == 0, (
        "a mocked finance run persisted a revenue snapshot; nothing was measured"
    )
    summary = await get_finance_summary(async_db_session, month=DAY)
    assert summary["last_snapshot_date"] is None
    assert summary["mrr_cents"] is None
    assert summary["stripe_balance_cents"] is None
