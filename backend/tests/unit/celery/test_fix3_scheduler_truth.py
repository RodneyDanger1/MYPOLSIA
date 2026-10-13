"""fix-3 scheduler truth — L4-01 / L4-09 / L4-11 (+ the L2-03 attempt seam and the L3-04
day-spend proof).

What this file pins:

* L4-11 — the evening cycle self-gates on the COMPANY-LOCAL hour and files the report for the
  company-local day (the T2 morning pattern); a tick outside the window is a named skip.
* L3-06 — both cycle call sites DECLARE their cycle in the task metadata.
* L4-09 — the manual-trigger path funnels through `_execute_agent_task`, where the D3 budget
  gate now sits at the choke point: an over-cap SCHEDULED-style dispatch stops there (no
  agent_runs row, terminal `rejected` row carrying the measured reason — W1/T03's D2 status —
  named skip), a fresh day still runs, and the owner's `source='manual'` trigger runs past the
  cap with a NAMED `manual_override` record (W1/T04's D3 flip).
* L2-03 — `attempt=task.attempts` is passed to the run-ledger writer when the service signature
  accepts the frozen keyword, and the call site degrades safely when it does not.
* L3-04 — a DeepSeek token estimate reaches `agent_runs.cost_usd` via the real writer, is summed
  by `day_spend_by_agent`, and is what makes the D3 gate deny an over-cap day.

File-backed DB + the Celery process loop via the shared `queue_db` fixture (no Redis, no
sockets, `CLAUDE_CLI_MOCK`).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

import app.config as app_config
from app.agents.accounting import budget_policy_from_settings
from celery_app.tasks import daily_cycle

# ------------------------------------------------------------------ helpers


def _capture_agent_calls(monkeypatch) -> list[dict]:
    """Replace `crew_factory.run_agent_for_task` with a recorder + deterministic answers."""
    import app.agents.crew_factory as crew_factory

    captured: list[dict] = []

    def _spy(agent_type: str, task_dict: dict | None, context: dict | None):
        captured.append({"agent_type": agent_type, "task": dict(task_dict or {})})
        if agent_type == "orchestrator":
            return {"summary": "cycle summary", "tasks": [], "key_focus": "focus", "insights": []}
        return {"summary": f"spy result for {agent_type}"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return captured


def _noop_delay(monkeypatch) -> None:
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: None, raising=False)


class _GroupRecorder:
    """Stands in for `celery.group`: records the fan-out batch, never touches the broker."""

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.apply_async_count = 0

    def __call__(self, tasks):
        self.batches.append(list(tasks))
        return self

    def apply_async(self, *args, **kwargs) -> None:
        self.apply_async_count += 1


def _install_group_recorder(monkeypatch) -> _GroupRecorder:
    recorder = _GroupRecorder()
    monkeypatch.setattr("celery.group", recorder)
    return recorder


def _seed_spend(queue_db, agent_type: str, amount_usd: str) -> None:
    from decimal import Decimal

    from app.core.database import utcnow
    from app.models.task import AgentRun

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(AgentRun(agent_type=agent_type, cost_usd=Decimal(amount_usd), started_at=utcnow()))
            await db.commit()

    queue_db.run(_seed())


# ------------------------------------------------------------------ L4-11 + L3-06


def test_the_evening_cycle_fires_in_the_company_local_hour_and_files_the_local_day(
    queue_db, monkeypatch
):
    """20:00 Asia/Tokyo == 11:00 UTC: the tick matches and the row carries the Tokyo date."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "Asia/Tokyo")
    monkeypatch.setattr(app_config.settings, "evening_cycle_hour", 20)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 11, 0, tzinfo=UTC))
    captured = _capture_agent_calls(monkeypatch)

    result = queue_db.run(daily_cycle._evening_cycle())

    assert result["status"] == "completed"
    assert result["day"] == "2026-10-13"
    assert result["window"]["hour"] == 20
    assert result["window"]["tz"] == "Asia/Tokyo"
    assert captured == [{
        "agent_type": "orchestrator",
        "task": {
            "title": "Evening reporting cycle",
            "description": "Generate evening summary for 2026-10-13",
            "metadata": {"cycle": "evening"},
        },
    }]
    rows = queue_db.query("SELECT report_date FROM daily_reports")
    assert [r["report_date"] for r in rows] == ["2026-10-13"], (
        "the report is filed for the company-local day the tick called evening"
    )


def test_an_evening_tick_outside_the_company_local_hour_is_a_named_skip(queue_db, monkeypatch):
    """20:00 UTC is 05:00 the NEXT day in Tokyo — the old UTC-crontab bug — now a recorded skip."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "Asia/Tokyo")
    monkeypatch.setattr(app_config.settings, "evening_cycle_hour", 20)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 20, 0, tzinfo=UTC))
    captured = _capture_agent_calls(monkeypatch)

    result = queue_db.run(daily_cycle._evening_cycle())

    assert result["status"] == "skipped"
    assert result["reason"] == "outside_evening_window"
    assert captured == [], "no model call on a non-matching tick"
    assert queue_db.count("daily_reports") == 0


def test_an_america_chicago_afternoon_utc_tick_no_longer_files_a_partial_day(queue_db, monkeypatch):
    """The probe table's case: UTC 20:00 == 15:00 Chicago (mid-afternoon) -> skip, no row."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "America/Chicago")
    monkeypatch.setattr(app_config.settings, "evening_cycle_hour", 20)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 20, 0, tzinfo=UTC))

    result = queue_db.run(daily_cycle._evening_cycle())

    assert result["status"] == "skipped"
    assert queue_db.count("daily_reports") == 0


def test_the_morning_cycle_call_site_declares_its_cycle(queue_db, monkeypatch):
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "morning_cycle_hour", 6)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 6, 30, tzinfo=UTC))
    _noop_delay(monkeypatch)
    recorder = _install_group_recorder(monkeypatch)
    captured = _capture_agent_calls(monkeypatch)

    result = queue_db.run(daily_cycle._morning_cycle())

    assert result["status"] == "completed"
    assert recorder.apply_async_count == 1, "the fixed morning work is fanned out as one group"
    orchestrator_calls = [c for c in captured if c["agent_type"] == "orchestrator"]
    assert len(orchestrator_calls) == 1
    assert orchestrator_calls[0]["task"]["metadata"] == {"cycle": "morning"}


# ------------------------------------------------------------------ L4-09 (+ the W1 T03/T04 flip)
#
# DELIBERATE CONTRACT CHANGE (W1 T03 + T04, DECISIONS.md D2/D3): the gate still refuses every
# non-manual over-cap dispatch — but the row now lands `rejected` ("refused before execution —
# nothing charged", D2) instead of `failed`, and the OWNER's manual trigger (D3: the row the
# trigger route writes, `source='manual'`) is no longer vetoed: it runs with a NAMED
# `manual_override` record.  Old pins, recorded here as old->new:
#   * `row["status"] == "failed"`                          -> `"rejected"` (+ outcome["task_status"])
#   * the manual leg did not exist in this file            -> `test_a_manual_over_cap_run_runs_
#     with_the_override_recorded` below
# The full manual-vs-scheduled battery lives in tests/unit/celery/test_t04_manual_override.py.


def test_an_over_cap_dispatch_is_refused_at_the_choke_point(queue_db, monkeypatch):
    """The API trigger funnels into `_execute_agent_task`; the D3 gate must stop it there.

    The row this test creates is NOT `source='manual'` (the queue fixture writes `'test'`), so it
    is a scheduled-style dispatch: refused, `rejected`, nothing charged.
    """
    from celery_app.tasks.agent_tasks import _execute_agent_task

    task_id = queue_db.create_task(title="over-cap run", agent_type="finance")
    policy = budget_policy_from_settings()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))  # the day is full

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "rejected"
    assert "insufficient budget" in outcome["reason"]
    assert queue_db.count("agent_runs", "agent_type = ?", ("finance",)) == 0, "no ledger row"
    row = queue_db.row(task_id)
    assert row["status"] == "rejected", "refused before execution, never charged (D2)"
    assert "budget:" in (row["error_message"] or ""), "the row carries the measured reason"
    assert row["claimed_by"] is None, "the claim is released with the terminal state"


def _create_manual_task(queue_db, *, title: str) -> int:
    """`QueueDB.create_task` pins `source="test"`; D3's override keys on `source='manual'`."""
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(db, title=title, agent_type="finance", source="manual")
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def test_a_manual_over_cap_run_runs_with_the_override_recorded(
    queue_db, monkeypatch, install_run_spy
):
    """D3: the owner's manual trigger is not vetoed by the cap — and the bypass is NAMED."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    policy = budget_policy_from_settings()
    _seed_spend(queue_db, "social_media", str(policy.day_limit_usd))  # the day is full
    task_id = _create_manual_task(queue_db, title="manual over-cap run")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed", "D3: the payer's Run Now is not the cap's subject"
    assert outcome["manual_override"] is True
    assert "past the daily cap" in outcome["manual_override_reason"]
    assert queue_db.row(task_id)["status"] == "completed"
    activities = queue_db.query(
        "SELECT action FROM activity_log WHERE action = 'budget_manual_override'"
    )
    assert len(activities) == 1, "the override is visible in the ledger"


def test_a_manual_run_on_a_fresh_day_still_runs(queue_db, monkeypatch, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    task_id = queue_db.create_task(title="manual fresh run", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed"
    assert queue_db.row(task_id)["status"] == "completed"


# ------------------------------------------------------------------ L2-03 seam


def test_the_l2_03_attempt_seam_records_the_execution_number(
    queue_db, monkeypatch, install_run_spy
):
    """`attempt=task.attempts` reaches the ledger: the run row now says which execution it was."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    task_id = queue_db.create_task(title="attempt seam", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed"
    rows = queue_db.query("SELECT attempt, task_id FROM agent_runs WHERE task_id = ?", (task_id,))
    assert rows, "the run ledger row exists"
    assert rows[0]["attempt"] == 1, "the claim's first execution lands as attempt 1"


# ------------------------------------------------------------------ L3-04 day-spend proof


def test_a_deepseek_token_cost_is_measured_into_the_day_and_tightens_the_gate(
    queue_db, monkeypatch
):
    """Envelope -> RunUsage -> worker writer -> `agent_runs.cost_usd` -> day_spend -> D3 deny."""
    from app.agents import accounting, claude_cli, deepseek_runtime, pricing
    from app.agents.base_agent import BasePolsiaAgent
    from celery_app.tasks._common import assert_budget_allows, day_spend_by_agent
    from celery_app.tasks.agent_tasks import _apply_run_usage

    monkeypatch.setattr(pricing, "is_peak_pricing", lambda now=None: False)  # deterministic
    monkeypatch.setattr(app_config.settings, "agent_daily_cost_cap_usd", 3.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chat-day", "object": "chat.completion", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": '{"summary": "x"}'}}],
            "usage": {"prompt_tokens": 10_000_000, "completion_tokens": 1_000_000,
                      "prompt_cache_hit_tokens": 0},
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    inv = claude_cli.build_invocation(prompt="price the day", max_turns=1)
    envelope = json.loads(deepseek_runtime.invoke(inv, client=client, api_key="k").stdout)
    estimate = envelope["cost_estimate_usd"]
    assert estimate == pytest.approx(1.5 + 0.6), "10M miss tokens + 1M out, off-peak flash"

    # the gate ALLOWS on a day with no measured spend...
    queue_db.run(assert_budget_allows("finance"))

    class _Probe(BasePolsiaAgent):
        agent_type = "finance"

        def run(self, task, context):  # pragma: no cover
            return {}

    probe = _Probe()
    probe.last_usage = accounting.usage_from_envelope(envelope)
    meta = probe.usage_metadata()
    assert meta["cost_usd"] == pytest.approx(round(estimate, 6))

    from app.core.database import utcnow
    from app.models.task import AgentRun

    async def _persist() -> None:
        async with queue_db.factory() as db:
            run = AgentRun(agent_type="finance", status="completed", started_at=utcnow())
            db.add(run)
            await db.flush()
            _apply_run_usage(run, meta)
            await db.commit()

    queue_db.run(_persist())
    assert queue_db.query("SELECT cost_usd FROM agent_runs WHERE agent_type = 'finance'")[0][
        "cost_usd"
    ] is not None, "the DeepSeek run wrote a real cost, not NULL"

    async def _spend():
        async with queue_db.factory() as db:
            return await day_spend_by_agent(db)

    total, per_agent = queue_db.run(_spend())
    assert total == pytest.approx(estimate)
    assert per_agent["finance"] == pytest.approx(estimate)

    # ...and now the SAME gate DENIES: the measured token cost is what tightened the day.
    from app.agents.accounting import BudgetStop

    with pytest.raises(BudgetStop) as excinfo:
        queue_db.run(assert_budget_allows("finance"))
    assert "insufficient budget" in excinfo.value.reason
