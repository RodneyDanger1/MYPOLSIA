"""W2 TS-2 + TS-3 — the snapshot writer refuses a future day and a measurement-less row.

TS-2 (future `snapshot_date`).  `snapshot_date` is MODEL-CONTROLLED: the finance agent supplies it
inside its JSON output, and `get_finance_summary` selects the newest row by date.  One
future-dated snapshot would therefore hijack the summary (and the dashboard's measured-finance
block) until that date arrived.  Live evidence of the model's date latitude: run 5 (started
2026-10-14) stamped `snapshot_date = 2026-02-05`.  The guard refuses a date LATER THAN BOTH the
company-local today and the UTC today — the maximum, because the agent is told to emit a UTC date
while the app's "today" is the company's local day, and refusing a legitimate today at that
boundary would be a false refusal.

TS-3 (no measured column).  A snapshot row's EXISTENCE is a provenance claim (`last_snapshot_date`,
`build_cash_block`'s "no snapshot yet" reason, the finance page's idle state).  A date-only call to
`upsert_revenue_snapshot` wrote an all-NULL row / reported a measurement that never happened; it
now refuses `E_VALIDATION` by name.  A supplied `0` is a measured zero and still writes.

Both refusals live in `upsert_revenue_snapshot`, the primitive beneath `persist_finance_run`, so
every caller inherits them; the persist seam reports the refusal as its documented `error` object
(never raises) and its savepoint rolls the whole persist back.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import E_VALIDATION, MeridianError
from app.models.finance import ExpenseRecord, RevenueSnapshot
from app.services import finance_service
from app.services.finance_service import persist_finance_run, upsert_revenue_snapshot

DAY = date(2026, 10, 20)
NEXT_DAY = date(2026, 10, 21)


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.fixture
def frozen_today(monkeypatch):
    """Freeze BOTH clocks the guard compares, so its boundary is deterministic."""
    monkeypatch.setattr(finance_service, "today_in", lambda: DAY)
    monkeypatch.setattr(finance_service, "today_utc", lambda: DAY)
    return DAY


# ── TS-3: no measurement, no row ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_date_only_call_is_refused_by_name_and_writes_nothing(async_db_session) -> None:
    with pytest.raises(MeridianError) as excinfo:
        await upsert_revenue_snapshot(async_db_session, snapshot_date=DAY)

    error = excinfo.value
    assert error.code == E_VALIDATION
    assert "measured no column" in error.message
    assert error.how_to_fix and "mrr_cents" in error.how_to_fix
    assert error.context == {"snapshot_date": DAY.isoformat()}
    assert await _count(async_db_session, RevenueSnapshot) == 0


@pytest.mark.asyncio
async def test_a_call_whose_every_value_is_none_is_refused_too(async_db_session) -> None:
    with pytest.raises(MeridianError) as excinfo:
        await upsert_revenue_snapshot(
            async_db_session, snapshot_date=DAY, mrr_cents=None, arr_cents=None
        )
    assert excinfo.value.code == E_VALIDATION
    assert "measured no column" in excinfo.value.message
    assert await _count(async_db_session, RevenueSnapshot) == 0


@pytest.mark.asyncio
async def test_a_measured_zero_still_writes(async_db_session) -> None:
    """The anti-over-correction leg: 0 is a measurement and must survive the new guard."""
    row = await upsert_revenue_snapshot(async_db_session, snapshot_date=DAY, mrr_cents=0)
    assert row.mrr_cents == 0
    assert row.arr_cents is None, "an omitted column is still NULL, not a fabricated 0"
    assert await _count(async_db_session, RevenueSnapshot) == 1


# ── TS-2: a snapshot records a day that happened ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_future_snapshot_date_is_refused_by_name(async_db_session, frozen_today) -> None:
    with pytest.raises(MeridianError) as excinfo:
        await upsert_revenue_snapshot(
            async_db_session, snapshot_date=NEXT_DAY, mrr_cents=1000
        )

    error = excinfo.value
    assert error.code == E_VALIDATION
    assert "future" in error.message and "a snapshot records a day that happened" in error.message
    assert error.context["snapshot_date"] == NEXT_DAY.isoformat()
    assert error.context["latest_plausible_day"] == DAY.isoformat()
    assert error.context["company_local_today"] == DAY.isoformat()
    assert error.context["utc_today"] == DAY.isoformat()
    assert await _count(async_db_session, RevenueSnapshot) == 0, "the refused row was not written"


@pytest.mark.asyncio
async def test_today_is_allowed(async_db_session, frozen_today) -> None:
    row = await upsert_revenue_snapshot(async_db_session, snapshot_date=DAY, mrr_cents=1000)
    assert row.mrr_cents == 1000


@pytest.mark.asyncio
async def test_the_boundary_accepts_a_date_that_is_today_in_either_calendar(
    async_db_session, monkeypatch
) -> None:
    """UTC ahead of the company-local day (e.g. company UTC-5 after 19:00 local): allow UTC today."""
    monkeypatch.setattr(finance_service, "today_in", lambda: DAY)
    monkeypatch.setattr(finance_service, "today_utc", lambda: NEXT_DAY)
    row = await upsert_revenue_snapshot(async_db_session, snapshot_date=NEXT_DAY, mrr_cents=1)
    assert row.snapshot_date == NEXT_DAY

    # ... and the mirror: company-local ahead of UTC (a UTC+9 company early in its day).
    async_db_session.add(
        RevenueSnapshot(snapshot_date=date(2026, 10, 25), mrr_cents=2)
    )
    await async_db_session.flush()
    monkeypatch.setattr(finance_service, "today_in", lambda: date(2026, 10, 25))
    monkeypatch.setattr(finance_service, "today_utc", lambda: date(2026, 10, 24))
    row = await upsert_revenue_snapshot(
        async_db_session, snapshot_date=date(2026, 10, 25), arr_cents=3
    )
    assert row.arr_cents == 3


@pytest.mark.asyncio
async def test_a_datetime_or_string_date_is_refused_before_the_comparison(async_db_session) -> None:
    """A datetime is a `date` SUBCLASS; it would silently compare against a date and insert."""
    with pytest.raises(MeridianError) as excinfo:
        await upsert_revenue_snapshot(
            async_db_session, snapshot_date=datetime(2026, 10, 20, 12, 0), mrr_cents=1
        )
    assert excinfo.value.code == E_VALIDATION
    assert "must be a date" in excinfo.value.message

    with pytest.raises(MeridianError) as excinfo:
        await upsert_revenue_snapshot(async_db_session, snapshot_date="2026-10-20", mrr_cents=1)  # type: ignore[arg-type]
    assert excinfo.value.code == E_VALIDATION
    assert "must be a date" in excinfo.value.message


# ── the seam every finance run crosses: persist reports the refusal, never fakes a write ─────


@pytest.mark.asyncio
async def test_persist_refuses_a_future_dated_run_loudly_and_writes_nothing(
    async_db_session, frozen_today
) -> None:
    report = await persist_finance_run(
        async_db_session,
        {
            "snapshot": {"snapshot_date": NEXT_DAY.isoformat(), "mrr_cents": 999},
            "expenses": [
                {
                    "category": "ads",
                    "vendor": "Google",
                    "amount_cents": 500,
                    "currency": "usd",
                    "date": DAY.isoformat(),
                }
            ],
        },
    )

    assert report["ok"] is False
    assert report["snapshot_written"] is False
    assert report["rolled_back"] is True
    assert report["error"]["code"] == E_VALIDATION
    assert "future" in report["error"]["message"]
    assert await _count(async_db_session, RevenueSnapshot) == 0
    assert await _count(async_db_session, ExpenseRecord) == 0, (
        "the whole persist rolled back: a run that mis-dated its snapshot does not get to write "
        "the rest of its claim"
    )


@pytest.mark.asyncio
async def test_persist_still_writes_a_measured_same_day_snapshot(async_db_session, frozen_today) -> None:
    report = await persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat(), "mrr_cents": 0}, "expenses": []},
    )
    assert report["ok"] is True
    assert report["snapshot_written"] is True
    assert report["snapshot_skipped"] is None
    row = (await async_db_session.execute(select(RevenueSnapshot))).scalar_one()
    assert row.mrr_cents == 0 and row.arr_cents is None


@pytest.mark.asyncio
async def test_persist_a_date_only_snapshot_is_still_a_named_skip_not_an_error(
    async_db_session, frozen_today
) -> None:
    """The wave-1 LIVE-3 guard is preserved: nothing measured = skip, named, no row."""
    report = await persist_finance_run(
        async_db_session,
        {"snapshot": {"snapshot_date": DAY.isoformat()}, "expenses": []},
    )
    assert report["ok"] is True
    assert report["snapshot_written"] is False
    assert report["snapshot_skipped"] == "unmeasured"
    assert await _count(async_db_session, RevenueSnapshot) == 0
