"""PHASE-3 fixer-2 / L2-01 — the finance month window is the COMPANY-local month.

RED before the fix (lane-2 `probe_lane2.py` part A): process clock 2026-10-31 +
`COMPANY_TIMEZONE=Asia/Tokyo` (company-local 2026-11-01) -> `_month_bounds(None)` started
2026-10-01, so `GET /finance/summary` counted the OCTOBER row (9900) and dropped the November
row (1141) — the "Expenses (Month)" tile showed the wrong month.  The fix is one token:
`date.today()` -> `today_in()` (`finance_service.py:69`; `today_in` already imported).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.config import settings
from app.core import timeutil
from app.services import finance_service


class _FakeDate(date):
    """Stands in for the PROCESS-local clock: says 2026-10-31 (a UTC box)."""

    @classmethod
    def today(cls):  # type: ignore[override]
        return date(2026, 10, 31)


class _FakeDatetime(datetime):
    """The instant the app is running: 2026-10-31T16:30Z == 2026-11-01 01:30 in Asia/Tokyo."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        instant = datetime(2026, 10, 31, 16, 30, tzinfo=UTC)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


def _freeze(monkeypatch) -> None:
    monkeypatch.setattr(settings, "company_timezone", "Asia/Tokyo")
    monkeypatch.setattr(finance_service, "date", _FakeDate)   # process-local clock
    monkeypatch.setattr(timeutil, "datetime", _FakeDatetime)  # the wall clock both read


def test_month_bounds_default_is_the_company_local_month(monkeypatch):
    """The direct pin: 2026-10-31 process / 2026-11-01 company -> November's bounds."""
    _freeze(monkeypatch)
    assert finance_service._month_bounds(None) == (date(2026, 11, 1), date(2026, 12, 1))
    # an explicit month still wins (the parameter's contract, unchanged)
    assert finance_service._month_bounds(date(2026, 10, 4)) == (
        date(2026, 10, 1),
        date(2026, 11, 1),
    )


@pytest.mark.asyncio
async def test_finance_summary_counts_the_company_local_month(async_db_session, monkeypatch):
    """The end-to-end pin: the November (company-local) row is the one the summary counts."""
    from app.models.finance import ExpenseRecord

    _freeze(monkeypatch)

    async_db_session.add(
        ExpenseRecord(category="ads", vendor="v", amount_cents=9900, date=date(2026, 10, 15))
    )
    async_db_session.add(
        ExpenseRecord(category="ads", vendor="v", amount_cents=1141, date=date(2026, 11, 1))
    )
    await async_db_session.commit()

    summary = await finance_service.get_finance_summary(async_db_session)
    assert summary["total_expenses_month_cents"] == 1141, (
        "the default month window must be the COMPANY-local month (Asia/Tokyo), whose only "
        "row is the 1141 one — 9900 is the process-local October row"
    )

    explicit = await finance_service.get_finance_summary(
        async_db_session, month=date(2026, 10, 1)
    )
    assert explicit["total_expenses_month_cents"] == 9900
