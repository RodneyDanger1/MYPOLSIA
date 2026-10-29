"""R-W10-1 — the report cycle's state must be derivable from rows, never guessed.

THE DEFECT (wave-2 carry, weaver's REQUESTS R-W10-1).  `GET /dashboard/summary` carried exactly
one report fact: `last_report_date` = `max(daily_reports.report_date)`.  With Celery beat OFF by
policy, the panel could not tell "the cycle has not run since X" from "the cycle is running and
failing", and it could not tell either from "this deployment has never run a cycle".

Two separate faults are pinned here:

1. **`last_report_date` was not a report.**  The MORNING cycle creates the daily row
   (`save_morning_plan`); the EVENING cycle writes the report (`save_evening_summary`).  The live
   row for 2026-10-20 has a morning plan and NO evening summary, so the dashboard said "last
   report 2026-10-20" while the last COMPLETED report was 2026-10-19.  `report_cycle.
   last_completed_report_date` now states the completed report separately (the pre-existing
   `last_report_date` is untouched — additive, no behaviour change).
2. **No state at all.**  `report_cycle.state` is derived by
   `report_service.derive_report_cycle` from the two committed ledgers
   (`activity_log` claim/skip rows and `daily_reports` evening summaries), so
   `not_run_today` (idle) / `report_running` (in progress) / `report_missing` (started, never
   finished = broken) / `never_triggered` (nothing ever ran) / `unknown` (evidence cannot decide)
   are distinct.  The cycle's schedule lives in a beat process the API cannot see, so the block
   declares `scheduler_observable: false` — it never claims to know the next run.

The write-side vocabulary (`cycle_claimed` / `cycle_tick_skipped` / `cycle="evening"`) is pinned
against `celery_app.tasks.daily_cycle`'s own constants, so a rename there fails HERE instead of
silently reporting `never_triggered` forever.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.timeutil import today_in
from app.models.company import CompanyConfig
from app.models.report import ActivityLog, DailyReport
from app.services.report_service import (
    REPORT_CYCLE_CLAIM_ACTION,
    REPORT_CYCLE_SCHEDULER_REASON,
    REPORT_CYCLE_SKIP_ACTION,
    REPORT_CYCLE_STATES,
    REPORT_EVENING_CYCLE,
    derive_report_cycle,
)

TODAY = date(2026, 10, 28)


def _claim(db, *, cycle: str, day: str, action: str = REPORT_CYCLE_CLAIM_ACTION, detail=None):
    row = ActivityLog(
        agent_type="orchestrator",
        action=action,
        summary=f"{cycle} cycle {action} for {day}",
        level="info",
        detail=detail if detail is not None else {"cycle": cycle, "day": day},
    )
    db.add(row)
    return row


def _report(db, *, day: date, evening: str | None, morning: str | None = "plan"):
    db.add(
        DailyReport(
            report_date=day,
            morning_plan=morning,
            evening_summary=evening,
        )
    )


# ── the derivation itself (pure, so every branch is reachable without a database) ─────────────


def test_the_writer_vocabulary_has_not_drifted() -> None:
    """The literals this module reads MUST be the ones the cycle writes."""
    from celery_app.tasks.daily_cycle import (
        CYCLE_CLAIM_ACTION,
        CYCLE_SKIP_ACTION,
    )

    assert CYCLE_CLAIM_ACTION == REPORT_CYCLE_CLAIM_ACTION
    assert CYCLE_SKIP_ACTION == REPORT_CYCLE_SKIP_ACTION
    # The claim payload's day/cycle keys, and the report writer's cycle name.
    assert REPORT_EVENING_CYCLE == "evening"


def test_nothing_has_ever_run_is_nevertriggered_with_no_fabricated_values() -> None:
    block = derive_report_cycle(today=TODAY)

    assert block["state"] == "never_triggered"
    # Every evidence field is NULL — an absent fact is never rendered as 0/false/epoch.
    assert block["last_claim_at"] is None
    assert block["last_claim_cycle"] is None
    assert block["last_claim_day"] is None
    assert block["last_evening_claim_day"] is None
    assert block["last_completed_report_date"] is None
    assert block["last_skip_reason"] is None
    assert block["claims_retained_since"] is None
    # The scheduler is NOT observable here and the block says so.
    assert block["scheduler_observable"] is False
    assert block["scheduler_reason"] == REPORT_CYCLE_SCHEDULER_REASON
    assert block["state_reason"]


def test_the_live_signature_is_not_run_today_not_broken() -> None:
    """The real 2026-10-28 DB shape: last evening claim 10-19 (report written), nothing since."""
    block = derive_report_cycle(
        today=TODAY,
        last_claim_at=None,  # filled below with a datetime for realism
        last_claim_cycle="evening",
        last_claim_day="2026-10-19",
        last_evening_claim_day="2026-10-19",
        last_completed_report_date=date(2026, 10, 19),
    )

    assert block["state"] == "not_run_today"
    assert "2026-10-19" in (block["state_reason"] or "")
    assert block["last_completed_report_date"] == date(2026, 10, 19)


def test_a_claimed_day_without_an_evening_summary_is_the_broken_signature() -> None:
    block = derive_report_cycle(
        today=TODAY,
        last_claim_at=None,
        last_claim_cycle="evening",
        last_claim_day="2026-10-19",
        last_evening_claim_day="2026-10-19",
        last_completed_report_date=None,  # the report never got written
    )
    assert block["state"] == "report_missing"

    # …and an OLDER completed report does not excuse the missing one.
    stale = derive_report_cycle(
        today=TODAY,
        last_claim_at=None,
        last_evening_claim_day="2026-10-19",
        last_completed_report_date=date(2026, 10, 18),
    )
    assert stale["state"] == "report_missing"


def test_today_is_distinguished_running_vs_written() -> None:
    running = derive_report_cycle(
        today=TODAY, last_evening_claim_day=TODAY.isoformat(), last_completed_report_date=None
    )
    assert running["state"] == "report_running"

    written = derive_report_cycle(
        today=TODAY,
        last_evening_claim_day=TODAY.isoformat(),
        last_completed_report_date=TODAY,
    )
    assert written["state"] == "report_written_today"


def test_a_future_claim_is_unknown_never_running() -> None:
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    block = derive_report_cycle(
        today=TODAY, last_evening_claim_day=tomorrow, last_completed_report_date=None
    )

    assert block["state"] == "unknown"
    assert "after today" in (block["state_reason"] or "")


def test_an_unparsable_claim_day_is_unknown_and_never_raises() -> None:
    block = derive_report_cycle(
        today=TODAY, last_evening_claim_day="not-a-day", last_completed_report_date=None
    )

    assert block["state"] == "unknown"
    assert block["last_evening_claim_day"] == "not-a-day", "the raw evidence is served as stored"


def test_claims_without_a_report_cycle_claim_are_unknown_not_guessed() -> None:
    # A morning claim but the report (evening) cycle has never claimed and no report exists.
    morning_only = derive_report_cycle(
        today=TODAY,
        last_claim_at=datetime(2026, 10, 28, 6, 0, tzinfo=UTC),
        last_claim_cycle="morning",
        last_claim_day=TODAY.isoformat(),
        last_evening_claim_day=None,
        last_completed_report_date=None,
    )
    assert morning_only["state"] == "unknown", "a cycle ran, but not the one that writes reports"

    # A completed report with no retained claim cannot be explained by the ledger either.
    orphan = derive_report_cycle(
        today=TODAY, last_evening_claim_day=None, last_completed_report_date=date(2026, 10, 19)
    )
    assert orphan["state"] == "unknown"


def test_every_derived_state_is_in_the_declared_vocabulary() -> None:
    cases = [
        {},
        {"last_evening_claim_day": None, "last_completed_report_date": date(2026, 10, 19)},
        {"last_evening_claim_day": "junk"},
        {"last_evening_claim_day": TODAY.isoformat()},
        {"last_evening_claim_day": TODAY.isoformat(), "last_completed_report_date": TODAY},
        {"last_evening_claim_day": "2026-10-19"},
        {"last_evening_claim_day": "2026-10-19", "last_completed_report_date": date(2026, 10, 19)},
        {
            "last_evening_claim_day": (TODAY + timedelta(days=1)).isoformat(),
        },
    ]
    for case in cases:
        block = derive_report_cycle(today=TODAY, **case)
        assert block["state"] in REPORT_CYCLE_STATES, (case, block["state"])


# ── the wire: /dashboard/summary carries the block, derived from the same rows ────────────────


@pytest.mark.asyncio
async def test_summary_separates_the_report_ROW_from_the_COMPLETED_report(
    async_db_session, api_client, auth_headers
) -> None:
    """The live fault: a morning-only row made `last_report_date` look like a written report."""
    today = today_in()
    older = today - timedelta(days=9)
    newer = today - timedelta(days=8)

    async_db_session.add(CompanyConfig(name="R-W10-1 Co", kpis={}, timezone="UTC"))
    # An evening cycle claimed `older` and wrote its report …
    _claim(async_db_session, cycle="evening", day=older.isoformat())
    _report(async_db_session, day=older, evening="the report was written")
    # … then the morning cycle created `newer`'s row, whose evening summary never came.
    _claim(async_db_session, cycle="morning", day=newer.isoformat())
    _report(async_db_session, day=newer, evening=None)
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The pre-existing field is unchanged (additive change): it is the ROW date …
    assert data["last_report_date"] == newer.isoformat()
    # … and the block states the COMPLETED report separately.
    cycle = data["report_cycle"]
    assert cycle["last_completed_report_date"] == older.isoformat()
    # Evidence, verbatim from the committed ledger.
    assert cycle["last_claim_cycle"] == "morning"
    assert cycle["last_claim_day"] == newer.isoformat()
    assert cycle["last_evening_claim_day"] == older.isoformat()
    assert cycle["claims_retained_since"] is not None
    # Nothing has started since the older evening cycle; its report was written → idle, not broken.
    assert cycle["state"] == "not_run_today"
    assert cycle["scheduler_observable"] is False
    assert cycle["scheduler_reason"] == REPORT_CYCLE_SCHEDULER_REASON
    assert cycle["last_skip_reason"] is None


@pytest.mark.asyncio
async def test_summary_reports_a_missing_evening_summary_as_broken(
    async_db_session, api_client, auth_headers
) -> None:
    today = today_in()
    claimed = today - timedelta(days=3)
    async_db_session.add(CompanyConfig(name="R-W10-1 Co", kpis={}, timezone="UTC"))
    _claim(async_db_session, cycle="evening", day=claimed.isoformat())
    _report(async_db_session, day=claimed, evening=None)
    await async_db_session.commit()

    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()

    assert data["last_report_date"] == claimed.isoformat(), "a row exists"
    assert data["report_cycle"]["last_completed_report_date"] is None, "no report was written"
    assert data["report_cycle"]["state"] == "report_missing"


@pytest.mark.asyncio
async def test_a_named_skip_is_evidence_not_a_state(
    async_db_session, api_client, auth_headers
) -> None:
    """`cycle_tick_skipped.reason` is served beside the state; it never decides it."""
    today = today_in()
    older = today - timedelta(days=4)
    async_db_session.add(CompanyConfig(name="R-W10-1 Co", kpis={}, timezone="UTC"))
    _claim(async_db_session, cycle="evening", day=older.isoformat())
    _report(async_db_session, day=older, evening="written")
    _claim(
        async_db_session,
        cycle="morning",
        day=today.isoformat(),
        action=REPORT_CYCLE_SKIP_ACTION,
        detail={"cycle": "morning", "day": today.isoformat(), "reason": "already_claimed"},
    )
    await async_db_session.commit()

    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()

    assert data["report_cycle"]["last_skip_reason"] == "already_claimed"
    # A skipped tick is not a stated cycle: the report cycle's last claim is still `older`, and
    # its report exists, so the deployment is idle — not broken, and not "running today".
    assert data["report_cycle"]["state"] == "not_run_today"


@pytest.mark.asyncio
async def test_an_empty_deployment_serves_the_block_with_nothing_fabricated(
    async_db_session, api_client, auth_headers
) -> None:
    """No company row, no claims, no reports: the block must still be present and honest."""
    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()

    assert data["last_report_date"] is None
    cycle = data["report_cycle"]
    assert cycle["state"] == "never_triggered"
    for field in (
        "last_claim_at",
        "last_claim_cycle",
        "last_claim_day",
        "last_evening_claim_day",
        "last_completed_report_date",
        "last_skip_reason",
        "claims_retained_since",
    ):
        assert cycle[field] is None, field
    assert cycle["scheduler_observable"] is False
