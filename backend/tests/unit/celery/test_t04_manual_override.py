"""T04/D3 — the manual owner trigger runs past the daily cap WITH a named override record;
every other dispatch keeps the hard gate.

`POST /agents/{type}/trigger` writes `tasks.source='manual'` (app/api/v1/agents.py:74) — that is
what makes a row the owner's Run Now vs a beat/orchestrator/scheduler dispatch
(`_create_and_enqueue`/`_ads_stripe_sync` write `source='scheduler'`; `POST /tasks` defaults to
`'dashboard'`).  D3's ruling: the daily cap is a spend governor for SCHEDULED dispatch, not a veto
over the payer — the manual run proceeds and the bypass is NAMED, in the run's own result and in
an activity row, so it is auditable rather than silent.

The two paths this file pins:
* manual + full day  -> run COMPLETES, `manual_override: true` + reason in the run result and the
  `budget_manual_override` activity row;
* anything else + full day -> today's hard gate (row `rejected`, T03), NO override record, and
  `manual_override: false` on the outcome.
"""
from __future__ import annotations

import json

from app.agents.accounting import budget_policy_from_settings
from app.core.database import utcnow


def _fill_the_day(queue_db, agent_type: str = "social_media") -> None:
    """Seed MEASURED spend for today's window at the declared day limit (the policy's own number)."""
    from decimal import Decimal

    from app.models.task import AgentRun

    policy = budget_policy_from_settings()

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(AgentRun(agent_type=agent_type, cost_usd=Decimal(str(policy.day_limit_usd)),
                            started_at=utcnow()))
            await db.commit()

    queue_db.run(_seed())


def _create_task_with_source(
    queue_db, *, title: str, agent_type: str, source: str, metadata: dict | None = None
) -> int:
    """`QueueDB.create_task` pins `source="test"`; these legs need the real source values.

    The row is created through the SERVICE on the same process loop, so the row on disk is one the
    production writer would create (nothing here fakes the `source` after the fact).
    """
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db, title=title, agent_type=agent_type, source=source, metadata=metadata
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def test_a_manual_run_past_the_daily_cap_completes_with_a_named_override(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    _fill_the_day(queue_db)
    task_id = _create_task_with_source(
        queue_db, title="T04 manual over cap", agent_type="finance", source="manual"
    )

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed", "the owner's manual trigger is not vetoed by the cap"
    assert outcome["manual_override"] is True
    assert "past the daily cap" in outcome["manual_override_reason"]

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["error_message"] is None

    # the RUN's own result carries the named record (answerable from the ledger alone)
    runs = queue_db.query(
        "SELECT output FROM agent_runs WHERE task_id = ?", (task_id,)
    )
    assert len(runs) == 1
    stored = json.loads(runs[0]["output"])
    assert stored["manual_override"] is True
    assert "past the daily cap" in stored["manual_override_reason"]

    # ...and so does the activity stream
    activities = queue_db.query(
        "SELECT action, level, summary, detail FROM activity_log WHERE action = 'budget_manual_override'"
    )
    assert len(activities) == 1
    entry = activities[0]
    assert entry["level"] == "warning"
    assert f"task {task_id}" in entry["summary"]
    detail = json.loads(entry["detail"])
    assert detail["manual_override"] is True and detail["task_id"] == task_id


def test_a_scheduled_run_past_the_daily_cap_keeps_the_hard_gate(queue_db, install_run_spy):
    """The exact scenario of the manual leg — only `source` differs.  Hard gate stays."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    _fill_the_day(queue_db)
    task_id = _create_task_with_source(
        queue_db, title="T04 scheduled over cap", agent_type="finance", source="scheduler"
    )

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "rejected"
    assert outcome["manual_override"] is False
    row = queue_db.row(task_id)
    assert row["status"] == "rejected" and "budget:" in (row["error_message"] or "")
    assert queue_db.count("agent_runs", "task_id = ?", (task_id,)) == 0, "no ledger row, nothing charged"
    assert queue_db.count("activity_log", "action = 'budget_manual_override'") == 0, (
        "no override may be recorded for a refused dispatch"
    )


def test_a_manual_run_on_a_fresh_day_records_no_override(queue_db, install_run_spy):
    """No cap hit means no override: the flag stays False and the stream stays silent."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    task_id = _create_task_with_source(
        queue_db, title="T04 manual fresh day", agent_type="finance", source="manual"
    )

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed"
    assert outcome["manual_override"] is False
    assert queue_db.count("activity_log", "action = 'budget_manual_override'") == 0


def test_the_trigger_route_still_writes_the_manual_source(queue_db, install_run_spy):
    """The definition of 'manual' is the ROUTE's own flag — pinned so it cannot drift silently.

    `app/api/v1/agents.py::trigger_agent` creates the row with `source="manual"`; the gate keys on
    exactly that value.  A rename on the route would otherwise turn every Run Now into a hard-gated
    dispatch with no test failing.
    """
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[3] / "app" / "api" / "v1" / "agents.py"
    text = source.read_text(encoding="utf-8")
    assert re.search(r'source="manual"', text), (
        "the trigger route no longer writes source='manual' — T04's override definition is stale"
    )
