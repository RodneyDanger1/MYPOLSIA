"""T10 / D19 — the daily-report counters are sentinelled like every other optional field.

M5 sentinelled `summary`/`insights`/`metrics_snapshot`, so a re-run with defaults left them
alone — but `save_evening_summary`'s two counters were still `int = 0` and always assigned, and
`save_morning_plan`'s `tasks_planned` was the same: a re-run that measured nothing silently
ZEROED counts an earlier run had measured.  D19 rules the counters follow the same rule:
not supplied -> left as-is; an explicit `None` clears (the columns are nullable).

RED leg (measured, receipt §RED): revert `tasks_completed`/`tasks_failed` to `int = 0` with
unconditional assignment and `test_a_rerun_with_defaults_preserves_the_counters` fails
(the counters come back 0).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.report_service import (
    get_daily_report,
    save_evening_summary,
    save_morning_plan,
)

TODAY = date(2026, 10, 13)


# ── the evening counters ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fresh_write_sets_the_counters(async_db_session):
    report = await save_evening_summary(
        async_db_session,
        TODAY,
        summary="first write",
        insights=["one"],
        tasks_completed=4,
        tasks_failed=1,
        metrics_snapshot={"mrr": 5000},
    )
    assert (report.tasks_completed, report.tasks_failed) == (4, 1)
    assert report.evening_summary == "first write"
    assert report.metrics_snapshot == {"mrr": 5000}


@pytest.mark.asyncio
async def test_a_rerun_with_defaults_preserves_the_counters(async_db_session):
    """The defect D19 closes: defaults must not zero a measured count."""
    await save_evening_summary(
        async_db_session, TODAY, summary="measured", tasks_completed=7, tasks_failed=2
    )
    await async_db_session.commit()

    # A re-run that supplies NOTHING (every argument defaulted).
    rerun = await save_evening_summary(async_db_session, TODAY)
    await async_db_session.commit()

    assert (rerun.tasks_completed, rerun.tasks_failed) == (7, 2), (
        "a defaulted re-run zeroed the counters"
    )
    stored = await get_daily_report(async_db_session, TODAY)
    assert stored is not None and (stored.tasks_completed, stored.tasks_failed) == (7, 2)
    assert stored.evening_summary == "measured", "the text fields keep their M5 behaviour too"


@pytest.mark.asyncio
async def test_a_rerun_can_still_replace_and_clear_the_counters(async_db_session):
    """Supplied values replace; an explicit `None` clears (labelled states, not accidents)."""
    await save_evening_summary(
        async_db_session, TODAY, summary="first", tasks_completed=1, tasks_failed=1
    )
    await async_db_session.commit()

    replaced = await save_evening_summary(async_db_session, TODAY, tasks_completed=9)
    await async_db_session.commit()
    assert (replaced.tasks_completed, replaced.tasks_failed) == (9, 1), "only what was supplied"

    cleared = await save_evening_summary(
        async_db_session, TODAY, tasks_completed=None, tasks_failed=None
    )
    await async_db_session.commit()
    assert cleared.tasks_completed is None and cleared.tasks_failed is None


# ── the morning counter (same file, same class) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_morning_rerun_with_defaults_preserves_tasks_planned(async_db_session):
    await save_morning_plan(async_db_session, TODAY, "Plan A", tasks_planned=5)
    await async_db_session.commit()

    rerun = await save_morning_plan(async_db_session, TODAY, "Plan A re-stated")
    await async_db_session.commit()
    assert rerun.tasks_planned == 5, "a defaulted re-run zeroed the planned count"
    assert rerun.morning_plan == "Plan A re-stated"

    replaced = await save_morning_plan(async_db_session, TODAY, "Plan B", tasks_planned=8)
    await async_db_session.commit()
    assert replaced.tasks_planned == 8

    cleared = await save_morning_plan(async_db_session, TODAY, "Plan C", tasks_planned=None)
    await async_db_session.commit()
    assert cleared.tasks_planned is None
