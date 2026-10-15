"""WAVE V15 / E3 item 3-02 (S1-02) — the hourly cycles' once-per-(cycle, local-day) claim.

RED-first pin for BUG-DIGEST MAJOR §B6: the morning/evening entries fire hourly with
`expires: 3600` (the local-hour gate width), so a pre-ack kill redelivers INSIDE the same
window and the whole cycle re-runs — a second orchestrator model call (double spend).  The
claim is a DURABLE row committed BEFORE any spend; a redelivered tick exits as a NAMED skip
recorded in the activity log, with ZERO provider calls.

Claim store (decided with evidence, recorded in the lane receipt): `activity_log` — the table
the item itself designates for the named-skip record; no existing table carries a
DB-enforced uniqueness key for (cycle, local-day) that is not a fabricated task row, and a new
table would serialize the lane on E1's migration.  The claim check+insert is serialized (PG:
company-row FOR UPDATE; SQLite: single writer), and the claim COMMITS before the model call.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from celery_app.tasks import daily_cycle


def _now_utc_hour() -> int:
    return datetime.now(UTC).hour


class FanOutRecorder:
    """Records the broker fan-out without touching a broker (the D2/T19 seam).

    `enqueue_created` imports `run_agent_task` lazily and calls `.delay` for one task /
    `celery.group([...]).apply_async()` for many — both are patched here.
    """

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    # `run_agent_task` stand-in: `.delay` records; `.s(...)` makes an inert signature.
    def delay(self, task_id: int) -> None:
        self.enqueued.append(int(task_id))

    def s(self, task_id: int):
        return ("sig", int(task_id))


def _install_fanout(monkeypatch) -> FanOutRecorder:
    rec = FanOutRecorder()
    import celery

    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)

    def _group(sigs):
        return SimpleNamespace(apply_async=lambda: rec.enqueued.extend(s[1] for s in sigs))

    monkeypatch.setattr(celery, "group", _group)
    return rec


def _install_model_spy(monkeypatch, *, result=None, raises: bool = False):
    """Stand in for `crew_factory.run_agent_for_task` — the ONE provider-call path."""
    import app.agents.crew_factory as crew_factory

    calls: list[dict] = []

    def _spy(agent_type, task_dict, context):
        calls.append({"agent_type": agent_type})
        if raises:
            raise RuntimeError("worker killed mid-run (rehearsal)")
        return result if result is not None else {"summary": f"spy plan for {agent_type}"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return calls


def _seed_company_hour(qdb, hour: int, *, paused: bool = False) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            db.add(
                CompanyConfig(
                    name="Claim Co", timezone="UTC", daily_cycle_hour=hour, dispatch_paused=paused
                )
            )
            await db.commit()

    qdb.run(_go())


def _set_company_hour(qdb, hour: int) -> None:
    from sqlalchemy import select

    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            row = (await db.execute(select(CompanyConfig))).scalars().first()
            row.daily_cycle_hour = hour
            await db.commit()

    qdb.run(_go())


def _run_in_window(qdb, runner, *, seed_first: bool = False, pin=None) -> dict:
    """Run a cycle exactly inside its window, re-pinning if the test crosses :59:59.

    `pin(hour)` applies the hour wherever the runner reads it (morning: the company row;
    evening: `settings.evening_cycle_hour` — the frozen schema carries no evening hour)."""
    if seed_first:
        _seed_company_hour(qdb, _now_utc_hour())
    pin = pin or (lambda h: _set_company_hour(qdb, h))
    for _attempt in range(3):
        pin(_now_utc_hour())
        result = runner()
        if result.get("reason") not in ("outside_morning_window", "outside_evening_window"):
            return result
    raise AssertionError("could not land inside the cycle window")


def _claim_rows(qdb) -> list[dict]:
    return qdb.query(
        "SELECT action, summary, level FROM activity_log WHERE action IN "
        "('cycle_claimed', 'cycle_tick_skipped') ORDER BY id"
    )


def test_morning_cycle_runs_once_then_redelivery_is_a_named_skip_with_zero_provider_calls(
    queue_db, monkeypatch, quiet_redis
):
    calls = _install_model_spy(monkeypatch, result={"summary": "morning plan"})
    fanout = _install_fanout(monkeypatch)

    first = _run_in_window(queue_db, daily_cycle.run_morning_cycle, seed_first=True)
    assert first["status"] == "completed", first
    assert len(calls) == 1
    created_first = first["created"]
    assert created_first == 3, "the three fixed morning tasks"
    assert len(fanout.enqueued) == 3

    # A redelivered tick inside the SAME window: named skip, zero provider calls, no new work.
    second = daily_cycle.run_morning_cycle()
    assert second["status"] == "skipped", second
    assert second["reason"] == "already_claimed", second
    assert second["cycle"] == "morning" and second["day"] == first["day"]
    assert len(calls) == 1, "the redelivery must not reach the model path"
    assert len(fanout.enqueued) == 3, "no duplicate task rows / no second fan-out"
    assert queue_db.count("tasks") == 3

    claims = _claim_rows(queue_db)
    assert [row["action"] for row in claims] == ["cycle_claimed", "cycle_tick_skipped"], claims
    assert queue_db.count("agent_runs") == 1, "exactly the one recorded cycle run"


def test_evening_cycle_claims_independently_of_the_morning(queue_db, monkeypatch, quiet_redis):
    from app.config import settings

    calls = _install_model_spy(monkeypatch, result={"summary": "evening words"})
    _install_fanout(monkeypatch)

    morning = _run_in_window(queue_db, daily_cycle.run_morning_cycle, seed_first=True)
    assert morning["status"] == "completed"
    evening = _run_in_window(
        queue_db,
        daily_cycle.run_evening_cycle,
        pin=lambda h: monkeypatch.setattr(settings, "evening_cycle_hour", h),
    )
    assert evening["status"] == "completed", (
        "the evening cycle owns its own (cycle, day) claim — morning's must not block it"
    )
    assert len(calls) == 2

    redelivered = daily_cycle.run_evening_cycle()
    assert redelivered["status"] == "skipped"
    assert redelivered["reason"] == "already_claimed"
    assert len(calls) == 2, "zero provider calls on the evening redelivery"

    actions = [row["action"] for row in _claim_rows(queue_db)]
    assert sorted(actions) == ["cycle_claimed", "cycle_claimed", "cycle_tick_skipped"]


def test_crash_after_claim_blocks_the_replay_rehearsal(queue_db, monkeypatch, quiet_redis):
    """Pre-ack kill rehearsal: tick dies AFTER its claim commit and BEFORE completing.

    The claim is what survives; the redelivery finds it and skips — the double spend B6
    describes cannot happen."""
    calls = _install_model_spy(monkeypatch, raises=True)
    _install_fanout(monkeypatch)

    with pytest.raises(RuntimeError):
        _run_in_window(queue_db, daily_cycle.run_morning_cycle, seed_first=True)
    assert len(calls) == 1, "the killed tick did reach the provider once (that spend is real)"

    # The broker redelivers the same tick inside the window (its `expires` has not passed).
    replay = daily_cycle.run_morning_cycle()
    assert replay["status"] == "skipped"
    assert replay["reason"] == "already_claimed"
    assert len(calls) == 1, "the replay must make ZERO provider calls"
    assert queue_db.count("tasks") == 0, "the killed tick created nothing; the replay must not either"

    # The killed run is visible as failed in the ledger (H-03 closes it) — evidence, not silence.
    run_rows = queue_db.query("SELECT status FROM agent_runs")
    assert [r["status"] for r in run_rows] == ["failed"]


def test_hourly_cycle_expires_are_shortened_below_the_hour():
    from celery_app.beat_schedule import beat_schedule

    for key in ("morning-cycle", "evening-cycle"):
        options = beat_schedule[key]["options"]
        assert int(options["expires"]) < 3600, (
            f"{key}: expires {options['expires']} == the local-hour gate width; "
            "a stale tick must not ride the whole window"
        )
