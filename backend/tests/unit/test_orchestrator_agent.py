"""RD15 dead-sweep — `app/agents/orchestrator/agent.py`: the production branches, tested.

This module runs twice a day (`celery_app/tasks/daily_cycle.py:159-167` morning, `:362-370`
evening) yet had NO test driving it ((0/39) in the audit coverage snapshot). The branches pinned
here are the ones a production incident would come from: which prompt a run uses, whether the
plan is truncated when the operator lowers `planner_max_nodes`, and whether the evening run
really clears `tasks` while keeping `insights`.

Nothing product-side is stubbed: a schema-shaped `CLAUDE_CLI_MOCK_RESPONSE` flows through the
real `collect_structured` mock pipeline (parse -> validate -> `model_dump`), so these tests
exercise `run()`'s actual code against an actual validated payload.

The truncation branch is live by CONFIGURATION, not by default: `settings.planner_max_nodes`
defaults to 12 (app/config.py:120) and `OrchestratorPlan.tasks` is capped at `max_length=12`
(app/schemas/agent_outputs.py:92) — so it fires only when an operator LOWERS the knob, which is
what `test_morning_run_truncates_...` does (`planner_max_nodes=3`).
"""
from __future__ import annotations

import json

import app.config as app_config
from app.agents.orchestrator.agent import EVENING_PROMPT, MORNING_PROMPT, OrchestratorAgent

# The exact dicts `daily_cycle` passes (read from celery_app/tasks/daily_cycle.py, 2026-10-13).
MORNING_TASK = {
    "title": "Morning planning cycle",
    "description": "Generate today's task plan for 2026-10-13",
}
EVENING_TASK = {
    "title": "Evening reporting cycle",
    "description": "Generate evening summary for 2026-10-13",
}


def _plan(n_tasks: int, *, mode: str = "morning", insights: list[str] | None = None) -> dict:
    plan: dict = {
        "summary": "Plan with tasks.",
        "key_focus": "Focus",
        "mode": mode,
        "tasks": [
            {"title": f"Task number {i}", "agent_type": "competitor_research"}
            for i in range(1, n_tasks + 1)
        ],
    }
    if insights is not None:
        plan["insights"] = insights
    return plan


def _mock_response(monkeypatch, plan: dict) -> None:
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps(plan))


# ------------------------------------------------------------------ plan_mode

def test_plan_mode_reads_the_production_task_dicts():
    assert OrchestratorAgent.plan_mode(MORNING_TASK) == "morning"
    assert OrchestratorAgent.plan_mode(EVENING_TASK) == "evening"


def test_plan_mode_keyword_cases_are_case_insensitive_and_description_counts():
    assert OrchestratorAgent.plan_mode({"title": "DAILY SUMMARY"}) == "evening"
    assert OrchestratorAgent.plan_mode({"title": "", "description": "Close out the week"}) == "evening"
    assert OrchestratorAgent.plan_mode({"title": "ship the thing", "description": "build it"}) == "morning"
    assert OrchestratorAgent.plan_mode({}) == "morning"  # empty task defaults to the morning plan


# ------------------------------------------------------------------ morning

def test_morning_run_keeps_tasks_and_reports_counts(monkeypatch):
    _mock_response(monkeypatch, _plan(3))
    out = OrchestratorAgent().run(MORNING_TASK, {})
    assert out["mode"] == "morning"
    assert out["planned_task_count"] == 3
    assert out["plan_titles"] == ["Task number 1", "Task number 2", "Task number 3"]
    assert len(out["tasks"]) == 3
    assert out["insights"] == []  # morning runs still carry an (empty) insights list
    assert not [n for n in out.get("notes") or [] if "truncated" in n]


def test_morning_run_computed_mode_overrides_a_model_declared_mode(monkeypatch):
    """The task title is the authority (module docstring): a model claiming 'evening' on the
    morning cycle must not flip the payload's mode."""
    _mock_response(monkeypatch, _plan(1, mode="evening"))
    out = OrchestratorAgent().run(MORNING_TASK, {})
    assert out["mode"] == "morning"


def test_morning_run_truncates_to_planner_max_nodes_and_records(monkeypatch):
    monkeypatch.setattr(app_config.settings, "planner_max_nodes", 3)
    _mock_response(monkeypatch, _plan(5))
    out = OrchestratorAgent().run(MORNING_TASK, {})
    assert out["planned_task_count"] == 3
    assert out["plan_titles"] == ["Task number 1", "Task number 2", "Task number 3"]
    assert len(out["tasks"]) == 3
    assert "plan truncated to planner_max_nodes=3 (model proposed 5)" in out["notes"]


# ------------------------------------------------------------------ evening

def test_evening_run_clears_tasks_and_keeps_insights(monkeypatch):
    _mock_response(monkeypatch, _plan(2, insights=["shipped the landing page", "spend under budget"]))
    agent = OrchestratorAgent()
    out = agent.run(EVENING_TASK, {})
    assert out["mode"] == "evening"
    assert out["tasks"] == []
    assert out["insights"] == ["shipped the landing page", "spend under budget"]
    assert "planned_task_count" not in out  # the planning keys are a morning-only shape
    assert agent.role_prompt is EVENING_PROMPT


def test_role_prompt_follows_EACH_run_not_the_construction(monkeypatch):
    """One instance, evening then morning: the prompt must switch per run (run() sets it)."""
    _mock_response(monkeypatch, _plan(1))
    agent = OrchestratorAgent()
    agent.run(EVENING_TASK, {})
    assert agent.role_prompt is EVENING_PROMPT
    out = agent.run(MORNING_TASK, {})
    assert agent.role_prompt is MORNING_PROMPT
    assert out["mode"] == "morning"
    assert out["planned_task_count"] == 1
