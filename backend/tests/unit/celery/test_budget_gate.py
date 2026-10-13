"""Dispatch-time budget gate (D3; audit A1 / lane-2 finding L2-F01).

The DAG planner's `accounting.authorize` gate had no production caller; the scheduler paths now
check the day's MEASURED spend (`agent_runs.cost_usd`, company-local day) against the declared
settings caps BEFORE creating rows. These tests seed `agent_runs` directly on the queue harness
(file-backed DB behind the app-wide session factory, driven on the Celery process loop).

Audit reference: L2-F01 (`planner.py:519` was the only `authorize` call site;
`daily_cycle.py` consumed the flat plan with no gate).

RED leg (observed): the `await assert_budget_allows(agent_type)` call in `_create_and_enqueue`
replaced with `pass` -> the over-cap and kill-switch tests come back `queued` with a row on
disk; restore -> green.
"""
from __future__ import annotations

from decimal import Decimal

from app.agents.accounting import budget_policy_from_settings


def _policy():
    """The policy the settings actually declare — never hardcoded in the test."""
    return budget_policy_from_settings()


def _seed_spend(queue_db, agent_type: str, amount_usd: str) -> None:
    from app.core.database import utcnow
    from app.models.task import AgentRun

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(AgentRun(agent_type=agent_type, cost_usd=Decimal(amount_usd), started_at=utcnow()))
            await db.commit()

    queue_db.run(_seed())


def _noop_delay(monkeypatch) -> None:
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: None, raising=False)


def test_a_fresh_day_dispatches_a_sweep_normally(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _create_and_enqueue

    _noop_delay(monkeypatch)

    result = queue_db.run(_create_and_enqueue("customer_support", "D3 fresh-day sweep", bucket="d3-a"))

    assert result["status"] == "queued", "measured spend 0 must not block a dispatch"
    assert queue_db.count("tasks", "title = ?", ("D3 fresh-day sweep",)) == 1


def test_a_full_day_denies_the_sweep_and_creates_no_row(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _create_and_enqueue

    _noop_delay(monkeypatch)
    policy = _policy()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))  # the day is full

    result = queue_db.run(_create_and_enqueue("customer_support", "D3 over-cap sweep", bucket="d3-b"))

    assert result["status"] == "budget_denied"
    assert "insufficient budget" in result["reason"], "the reason carries the measured numbers"
    assert queue_db.count("tasks", "title = ?", ("D3 over-cap sweep",)) == 0, "no row when denied"


def test_the_kill_switch_freezes_dispatch_even_on_a_fresh_day(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _create_and_enqueue

    _noop_delay(monkeypatch)
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")

    result = queue_db.run(_create_and_enqueue("customer_support", "D3 freeze sweep", bucket="d3-c"))

    assert result["status"] == "budget_denied"
    assert "kill switch" in result["reason"]
    assert queue_db.count("tasks", "title = ?", ("D3 freeze sweep",)) == 0


def test_ads_stripe_sync_reports_each_denied_entry_and_skips_its_row(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _noop_delay(monkeypatch)
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")

    result = queue_db.run(_ads_stripe_sync())

    assert len(result["denied"]) == 2, "both planned agents are named, not silently dropped"
    assert all("kill switch" in reason for reason in result["denied"])
    assert queue_db.count("tasks", "agent_type IN ('ads_management','finance')") == 0


# ── L4-09: the manual-trigger path passes the SAME choke point as the scheduler ───────────────
# `POST /agents/{type}/trigger` answers 202 and dispatches `run_agent_task`; nothing on that path
# called `assert_budget_allows`, so a Run Now click past the daily cap spent real money with no
# record. The gate now sits in `_execute_agent_task` (after the claim, before the run ledger
# row). RED leg: remove the gate -> the over-cap test below comes back `completed` with an
# agent_runs row on disk.
#
# W1/T03 (D2): the refusal is recorded on the row as `rejected` ("refused before execution —
# nothing charged"), not `failed`.  W1/T04 (D3): ONLY a run whose row carries `source='manual'`
# (what the trigger route writes) bypasses the cap — with a named `manual_override` record; every
# OTHER dispatch keeps the hard gate, which is what the rows below (created with the service's
# default source) exercise.  The manual-override legs live in
# tests/unit/celery/test_t04_manual_override.py.


def test_an_over_cap_dispatch_is_rejected_and_recorded(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    policy = _policy()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))  # the day is full
    task_id = queue_db.create_task(title="L4-09 over-cap run", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "rejected"
    assert "insufficient budget" in outcome["reason"], "the skip names the measured reason"
    assert queue_db.count("agent_runs", "agent_type = ?", ("finance",)) == 0, "no run ledger row"
    row = queue_db.row(task_id)
    assert row["status"] == "rejected" and "budget:" in (row["error_message"] or "")
    assert row["claimed_by"] is None


def test_a_run_on_a_fresh_day_still_completes(queue_db, monkeypatch, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    task_id = queue_db.create_task(title="L4-09 fresh run", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed", "measured spend 0 must not block a run"
    assert outcome["manual_override"] is False
