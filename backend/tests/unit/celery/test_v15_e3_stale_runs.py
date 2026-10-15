"""WAVE V15 / E3 item 3-01 (S1-01) — the `close_stale_agent_runs` maintenance tick.

RED-first pin for the maintenance tick that closes `agent_runs` rows stuck `running` forever
(BUG-DIGEST MAJOR §B5: `create_agent_run` is the only writer, `finish_agent_run` the only
closer, and every maintenance tick ignored the table).  The tick mirrors
`requeue_stale_task_claims` (`maintenance.py`): a bounded sweep, a named reason recorded on
every row it closes, ONE batched activity row, and idempotence — a second tick over the same
rows is a measured no-op.

Non-negotiables this file pins:
* only STALE rows close (`started_at` older than the bound); a fresh `running` row is untouched;
* the close goes through `task_service.finish_agent_run` (`cancelled` — the vocabulary's
  terminal-abandoned state) with the named reason on `raw_log`, `ended_at` stamped;
* NO fabricated cost: an unmeasured `cost_usd` stays NULL, and a MEASURED cost keeps counting
  against the day cap (`_common.day_spend_by_agent`) exactly as before the close;
* the beat entry exists with an `expires` below its interval.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

# Importing the module under test is part of the RED pin: at HEAD `close_stale_agent_runs`
# does not exist on it (every maintenance tick ignores `agent_runs`).
from celery_app.tasks import maintenance

#: The bound the implementation must use (module constant; documented as 24x the 300 s
#: `claude_timeout_seconds` — no new settings key, `config.py` is not this lane's file).
BOUND_SECONDS = 7200


def _naive_utc(seconds_ago: float) -> datetime:
    """SQLite stores naive UTC wall-clock text (`timeutil.as_bound_instant`'s note)."""
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds_ago)


def _seed_run(
    qdb,
    *,
    status: str = "running",
    seconds_ago: float | None = 3 * 3600,
    cost_usd: Decimal | None = None,
    agent_type: str = "finance",
) -> int:
    from app.models.task import AgentRun

    async def _make() -> int:
        async with qdb.factory() as db:
            run = AgentRun(
                agent_type=agent_type,
                run_type="task",
                status=status,
                # A Python None on a `server_default` column means "apply the default"
                # (measured: the generated INSERT omits the column entirely), so the undated
                # case is constructed at the FILE level below — the same trick
                # test_daily_cycle_window's NULL-hour case uses.
                started_at=_naive_utc(seconds_ago if seconds_ago is not None else 0.0),
                cost_usd=cost_usd,
            )
            db.add(run)
            await db.commit()
            return int(run.id)

    run_id = qdb.run(_make())
    if seconds_ago is None:
        # QueueDB exposes no raw `execute`; the file-level NULL write is done with the stdlib
        # driver against the same file (the reader must not be the writer).
        import sqlite3

        con = sqlite3.connect(str(qdb.path))
        try:
            con.execute("UPDATE agent_runs SET started_at = NULL WHERE id = ?", (run_id,))
            con.commit()
        finally:
            con.close()
        stored = qdb.query("SELECT started_at FROM agent_runs WHERE id = ?", (run_id,))
        assert stored[0]["started_at"] is None, "the NULL must really be on the file"
    return run_id


def _run_sweep(qdb) -> dict:
    """Drive the async body the way the celery task drives it (the process loop)."""
    return qdb.run(maintenance._close_stale_runs_in_session())


def test_stale_run_closes_fresh_stays_and_second_tick_is_a_noop(queue_db):
    stale_id = _seed_run(queue_db, seconds_ago=3 * 3600)          # 3 h old -> stale
    fresh_id = _seed_run(queue_db, seconds_ago=60)                # 1 min old -> fresh

    first = _run_sweep(queue_db)

    assert first["bound_seconds"] == BOUND_SECONDS
    assert first["closed_count"] == 1, first
    assert first["closed"] == [stale_id]

    rows = queue_db.query("SELECT * FROM agent_runs ORDER BY id")
    by_id = {r["id"]: r for r in rows}
    assert by_id[stale_id]["status"] == "cancelled", (
        "a run that never finished is closed `cancelled` (agent_runs vocabulary), not `failed`"
    )
    assert by_id[stale_id]["ended_at"] is not None
    assert "E_RUN_STALE" in (by_id[stale_id]["raw_log"] or ""), (
        "the run names WHY it was closed"
    )
    assert str(BOUND_SECONDS) in (by_id[stale_id]["raw_log"] or "")
    assert by_id[stale_id]["cost_usd"] is None, "no fabricated cost on an unmeasured run"
    assert by_id[fresh_id]["status"] == "running" and by_id[fresh_id]["ended_at"] is None

    # The sweep names itself in the activity log with every closed id.
    activity = queue_db.query(
        "SELECT action, detail FROM activity_log WHERE action = ?",
        ("stale_agent_runs_closed",),
    )
    assert len(activity) == 1
    assert f'"id": {stale_id}' in activity[0]["detail"] or f"[{stale_id}]" in activity[0]["detail"]

    # Idempotent: the same rows are no longer `running`; the fresh one is the only residue.
    second = _run_sweep(queue_db)
    assert second["closed_count"] == 0, (
        f"a second tick must be a measured no-op, got {second!r}"
    )
    rows = queue_db.query("SELECT * FROM agent_runs ORDER BY id")
    by_id = {r["id"]: r for r in rows}
    assert by_id[fresh_id]["status"] == "running", "the fresh row is still untouched"


def test_measured_cost_keeps_counting_against_the_day_cap(queue_db):
    """A stale run whose cost WAS recorded keeps it — and the day cap still sees it."""
    from celery_app.tasks._common import day_spend_by_agent

    cost_id = _seed_run(queue_db, seconds_ago=3 * 3600, cost_usd=Decimal("1.2500"))

    async def _spend() -> tuple[float, dict[str, float]]:
        async with queue_db.factory() as db:
            return await day_spend_by_agent(db)

    before_total, before_agents = queue_db.run(_spend())
    assert before_total == pytest.approx(1.25)

    result = _run_sweep(queue_db)
    assert cost_id in result["closed"]

    after_total, after_agents = queue_db.run(_spend())
    assert after_total == pytest.approx(1.25), (
        "closing a stale run must not erase a measured cost (nothing is zeroed)"
    )
    assert after_agents.get("finance") == pytest.approx(1.25)

    row = next(r for r in queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (cost_id,)))
    assert row["cost_usd"] is not None, "the measured cost stays on the row"


def test_undated_running_row_is_closed_with_the_missing_timestamp_named(queue_db):
    """A `running` row with no `started_at` can never become measurable — close it, named."""
    undated_id = _seed_run(queue_db, seconds_ago=None)
    result = _run_sweep(queue_db)
    assert undated_id in result["closed"]
    row = next(r for r in queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (undated_id,)))
    assert row["status"] == "cancelled"
    assert "started_at" in (row["raw_log"] or ""), (
        "the reason names the missing timestamp instead of inventing an age"
    )


def test_beat_entry_exists_with_expires_below_the_interval():
    from celery_app.beat_schedule import beat_schedule

    entry = beat_schedule["stale-agent-runs-sweep"]
    assert entry["task"] == "celery_app.tasks.maintenance.close_stale_agent_runs"
    options = entry["options"]
    assert options["queue"] == "maintenance"
    # The interval is 300 s (crontab */5); `expires` must sit strictly below it (the sweep
    # convention the lease sweep established).
    assert int(options["expires"]) < 300, options
