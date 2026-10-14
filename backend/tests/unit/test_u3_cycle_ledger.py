"""U3 / L4 — H-03 (backend auditor, wave U2): the cycle orchestrator runs are IN the run ledger
and BEHIND the dispatch budget gate.

The defect this file pins (audit `wave-u2/backend-audit/BACKEND-AUDIT.md` H-03, re-verified on the
live bytes): `celery_app/tasks/daily_cycle.py` called `run_agent_for_task("orchestrator", …)`
DIRECTLY at `:206-218` (morning) and `:475-485` (evening) — bypassing `create_agent_run` /
`finish_agent_run` / `_apply_run_usage` (whose only other production call site is the queue worker,
`agent_tasks.py:531/662/671`) AND bypassing `assert_budget_allows`.  Three measured consequences:

1. `GET /finance/summary`'s `ai_spend` and `celery_app.tasks._common.day_spend_by_agent` sum
   `agent_runs.cost_usd` for the company-local day — two real model runs/day were missing from that
   sum, so "Spend today" under-measured every scheduled day and the day cap could not see them.
2. The cycle's OWN call was never gated: no `assert_budget_allows("orchestrator")` ran before it.
3. A raising run left no record at all.

What this file proves, per test (each named in the test's own docstring), and the RED leg each one
carries: every test in this file FAILS against the pre-fix tree — (1)(2)(5) on "no `agent_runs` row",
(3)(6) on "the cycle ran the model on a day it should have refused", (4) on "a crashed run left no
row".  Verified by running this file before the fix (`raw/red-cycle-ledger.txt`).

Harness: the shared `queue_db` fixture (file-backed SQLite behind the app-wide session factory,
driven on the Celery process loop — `tests/unit/celery/conftest.py`, re-exposed at `tests/unit/`),
with the cycle run through its real Celery entry points.  `quiet_redis` is re-declared locally for
the same reason `test_t13_dead_surface.py` re-declares it: the celery-directory copy is autouse only
inside that directory, and the cycle publishes its headline event.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import app.config as app_config
from app.agents.accounting import budget_policy_from_settings
from celery_app.tasks import daily_cycle

# ── harness helpers ──────────────────────────────────────────────────────────────────────────


class CycleSpy:
    """Stands in for `crew_factory.run_agent_for_task`; records calls and answers deterministically.

    Installed on the module attribute, which is what the cycle resolves at call time — the same
    seam the shipped cycle tests use (`test_daily_cycle_window.py::_install_agent_spy`).
    """

    def __init__(self, *, result: dict | None = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, agent_type, task_dict, context):
        self.calls.append({"agent_type": agent_type, "task": dict(task_dict or {})})
        if self.error is not None:
            raise self.error
        return dict(self.result or {})


class _GroupRecorder:
    """Stands in for `celery.group` (imported at call time by `_common.enqueue_created`)."""

    def __init__(self) -> None:
        self.apply_async_count = 0

    def __call__(self, sigs):
        return self

    def apply_async(self, *args, **kwargs) -> None:
        self.apply_async_count += 1


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the cycle's headline publish goes to a counting mock."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


def _install_spy(monkeypatch, **kwargs) -> CycleSpy:
    import app.agents.crew_factory as crew_factory

    spy = CycleSpy(**kwargs)
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return spy


def _install_group_recorder(monkeypatch) -> _GroupRecorder:
    recorder = _GroupRecorder()
    monkeypatch.setattr("celery.group", recorder)
    return recorder


def _seed_spend(queue_db, agent_type: str, amount_usd: str) -> None:
    """One `agent_runs` row with a measured cost, inside today's company-local day window."""
    from decimal import Decimal

    from app.core.database import utcnow
    from app.models.task import AgentRun

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(
                AgentRun(agent_type=agent_type, cost_usd=Decimal(amount_usd), started_at=utcnow())
            )
            await db.commit()

    queue_db.run(_seed())


def _day_spend(queue_db) -> tuple[float, dict[str, float]]:
    from celery_app.tasks._common import day_spend_by_agent

    async def _spend() -> tuple[float, dict[str, float]]:
        async with queue_db.factory() as db:
            return await day_spend_by_agent(db)

    return queue_db.run(_spend())


def _cycle_runs(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT task_id, run_type, status, cost_usd, tokens_input, tokens_output, tokens_used,"
        " cli_session_id, raw_log, ended_at FROM agent_runs WHERE agent_type = 'orchestrator'"
        " ORDER BY id"
    )


def _activities(queue_db, action: str) -> list[dict]:
    return queue_db.query(
        "SELECT agent_type, level, summary, detail FROM activity_log WHERE action = ?", (action,)
    )


_MORNING_USAGE = {
    "summary": "plan with cost",
    "tasks": [],
    "insights": [],
    "tokens_input": 1000,
    "tokens_output": 500,
    "cost_usd": 0.25,
    "cli_session_id": "cycle-session-1",
}


def _morning_env(queue_db, monkeypatch) -> CycleSpy:
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "morning_cycle_hour", 6)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 6, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    return _install_spy(monkeypatch, result=_MORNING_USAGE)


def _evening_env(queue_db, monkeypatch) -> CycleSpy:
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "evening_cycle_hour", 20)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 20, 0, tzinfo=UTC))
    return _install_spy(
        monkeypatch,
        result={
            "summary": "evening with cost",
            "insights": [],
            "tokens_input": 40,
            "tokens_output": 10,
            "cost_usd": 0.10,
            "cli_session_id": "cycle-session-2",
        },
    )


# ── 1. the ledger row ────────────────────────────────────────────────────────────────────────


def test_a_completed_morning_cycle_leaves_exactly_one_cycle_run_row(queue_db, monkeypatch):
    """The morning cycle's model call is a RECORDED run: one `agent_runs` row, no task, closed.

    RED (pre-fix): the cycle calls `run_agent_for_task` directly, so `agent_runs` stays EMPTY and
    every assertion below fails on an empty list.
    """
    spy = _morning_env(queue_db, monkeypatch)

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "completed", result
    assert spy.calls and spy.calls[0]["agent_type"] == "orchestrator"
    rows = _cycle_runs(queue_db)
    assert len(rows) == 1, f"expected exactly ONE cycle run row, saw {rows!r}"
    row = rows[0]
    assert row["task_id"] is None, "a cycle run has no task (the schema's partial unique permits it)"
    assert row["run_type"] == "cycle"
    assert row["status"] == "completed"
    assert row["ended_at"] is not None, "the run is CLOSED, not left running"
    # the run's own accounting columns, from the result's RUN_META_KEYS
    assert row["tokens_input"] == 1000
    assert row["tokens_output"] == 500
    assert row["tokens_used"] == 1500
    assert row["cli_session_id"] == "cycle-session-1"
    assert float(row["cost_usd"]) == pytest.approx(0.25)
    assert queue_db.count("agent_runs") == 1, "no fabricated extra rows"


def test_the_cycle_runs_cost_reaches_the_days_measured_spend(queue_db, monkeypatch):
    """The `ai_spend` under-count the audit measured: the cycle's cost must be in the day's sum.

    `day_spend_by_agent` is the function BOTH the finance read and the dispatch gate use, so this
    single assertion is the whole H-03 leg-1 consequence.

    RED (pre-fix): no ledger row exists -> the day's spend is 0.0 for the orchestrator.
    """
    _morning_env(queue_db, monkeypatch)

    result = daily_cycle.run_morning_cycle()
    assert result["status"] == "completed"

    total, per_agent = _day_spend(queue_db)
    assert per_agent.get("orchestrator") == pytest.approx(0.25), (
        f"the cycle's measured cost is missing from the day: {per_agent!r}"
    )
    assert total == pytest.approx(0.25)


# ── 2. the gate ──────────────────────────────────────────────────────────────────────────────


def test_an_over_cap_day_denies_the_morning_cycle_before_any_model_call(queue_db, monkeypatch):
    """A full day refuses the cycle's own dispatch: named skip, no model call, no run row.

    "Refused before execution — nothing charged" is the queue's own D2/D3 contract for a scheduled
    dispatch; the cycle is a scheduled dispatch.

    RED (pre-fix): nothing gates the cycle, so the spy IS called, `agent_runs` gains its row and the
    tick reports `completed` despite the full day.
    """
    spy = _morning_env(queue_db, monkeypatch)
    policy = budget_policy_from_settings()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))  # the day is full

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "skipped", result
    assert result["reason"] == "budget_denied"
    assert "insufficient budget" in result["budget"], "the skip names the measured numbers"
    assert spy.calls == [], "no model call may happen on a refused dispatch"
    assert _cycle_runs(queue_db) == [], "refused before execution: no ledger row"
    assert queue_db.count("tasks") == 0, "no planned work on a full day"
    denied = _activities(queue_db, "cycle_budget_denied")
    assert len(denied) == 1, "the refusal is owner-visible"
    assert denied[0]["agent_type"] == "orchestrator"
    assert denied[0]["level"] == "warning"
    detail = json.loads(denied[0]["detail"])
    assert detail["cycle"] == "morning"
    assert detail["day"] == "2026-10-13"


def test_an_over_cap_day_denies_the_evening_cycle_before_any_model_call(queue_db, monkeypatch):
    """The evening call site is gated too (`daily_cycle.py:475-485` — the second H-03 anchor).

    RED (pre-fix): the evening cycle runs the model and writes its summary/report despite the cap.
    """
    spy = _evening_env(queue_db, monkeypatch)
    policy = budget_policy_from_settings()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))

    result = daily_cycle.run_evening_cycle()

    assert result["status"] == "skipped", result
    assert result["reason"] == "budget_denied"
    assert spy.calls == []
    assert _cycle_runs(queue_db) == []
    assert queue_db.count("daily_reports") == 0, "no report is filed on a refused tick"
    denied = _activities(queue_db, "cycle_budget_denied")
    assert len(denied) == 1
    assert json.loads(denied[0]["detail"])["cycle"] == "evening"


# ── 3. the failure path ──────────────────────────────────────────────────────────────────────


def test_a_raising_cycle_run_is_closed_as_failed_in_the_ledger(queue_db, monkeypatch):
    """A crashed cycle run CLOSES its ledger row; the exception still propagates (loud).

    Without this the fix would trade one invisibility for another: a forever-`running` row.

    RED (pre-fix): no ledger row exists at all, so the row assertions fail.
    """
    _morning_env(queue_db, monkeypatch)
    spy = _install_spy(monkeypatch, error=RuntimeError("boom"))
    assert spy.error is not None

    with pytest.raises(RuntimeError, match="boom"):
        daily_cycle.run_morning_cycle()

    rows = _cycle_runs(queue_db)
    assert len(rows) == 1, f"the crashed run must still be one row, saw {rows!r}"
    assert rows[0]["status"] == "failed"
    assert "RuntimeError: boom" in (rows[0]["raw_log"] or "")
    assert rows[0]["ended_at"] is not None, "a failed run is closed, never left running"


# ── 4. the evening cycle's ledger row ────────────────────────────────────────────────────────


def test_a_completed_evening_cycle_leaves_a_cycle_run_row(queue_db, monkeypatch):
    """The evening call site records its run and its cost, like the morning one.

    RED (pre-fix): `agent_runs` stays empty.
    """
    spy = _evening_env(queue_db, monkeypatch)

    result = daily_cycle.run_evening_cycle()

    assert result["status"] == "completed", result
    assert spy.calls and spy.calls[0]["agent_type"] == "orchestrator"
    rows = _cycle_runs(queue_db)
    assert len(rows) == 1, f"expected exactly ONE cycle run row, saw {rows!r}"
    assert rows[0]["run_type"] == "cycle"
    assert rows[0]["status"] == "completed"
    assert float(rows[0]["cost_usd"]) == pytest.approx(0.10)
    total, per_agent = _day_spend(queue_db)
    assert per_agent.get("orchestrator") == pytest.approx(0.10)
    assert total == pytest.approx(0.10)
