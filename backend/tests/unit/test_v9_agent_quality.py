"""V9 B2 (S1 #8/#9): GET /api/v1/ops/agent-quality + the morning report's weekly quality line.

The endpoint is deterministic (no LLM): per registry agent it answers runs_7d, success_rate,
last_failure_at and cost_per_completed_task from the agent_runs ledger — null-honest.  The
orchestrator's morning run appends ONE weekly line and a structured drop record when the measured
7-day success rate is below the 0.8 floor — never silent, never a second line.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.database import utcnow
from app.models.task import AgentRun


@pytest.mark.asyncio
async def test_agent_quality_lists_every_registry_agent_null_honest(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/agent-quality", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    rows = {row["agent_type"]: row for row in resp.json()["agents"]}
    assert set(rows) == {
        "orchestrator", "business_planning", "competitor_research", "social_media", "email_outreach",
        "customer_support", "ads_management", "code_generation", "finance",
    }
    for row in rows.values():
        assert row["runs_7d"] == 0
        assert row["success_rate"] is None
        assert row["last_failure_at"] is None
        assert row["cost_per_completed_task"] is None


@pytest.mark.asyncio
async def test_agent_quality_measures_the_ledger_window(api_client, auth_headers, async_db_session):
    now = utcnow()
    async_db_session.add(AgentRun(agent_type="social_media", status="completed", cost_usd=Decimal("1.5"),
                                  started_at=now - timedelta(days=1), ended_at=now - timedelta(days=1)))
    async_db_session.add(AgentRun(agent_type="social_media", status="completed", cost_usd=Decimal("0.5"),
                                  started_at=now - timedelta(days=2), ended_at=now - timedelta(days=2)))
    async_db_session.add(AgentRun(agent_type="social_media", status="failed", cost_usd=None,
                                  started_at=now - timedelta(hours=2), ended_at=now - timedelta(hours=1)))
    async_db_session.add(AgentRun(agent_type="social_media", status="failed", cost_usd=Decimal("9"),
                                  started_at=now - timedelta(days=9), ended_at=now - timedelta(days=9)))
    async_db_session.add(AgentRun(agent_type="finance", status="completed", cost_usd=None,
                                  started_at=now - timedelta(days=1), ended_at=now - timedelta(days=1)))
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/ops/agent-quality", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    rows = {row["agent_type"]: row for row in resp.json()["agents"]}

    social = rows["social_media"]
    assert social["runs_7d"] == 3  # the 9-day-old run is outside the window
    assert social["success_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert social["last_failure_at"] is not None
    assert social["cost_per_completed_task"] == 1.0  # (1.5 + 0.5) / 2 completed

    finance = rows["finance"]
    assert finance["runs_7d"] == 1
    assert finance["success_rate"] == 1.0
    assert finance["cost_per_completed_task"] is None, "a completed run with no measured cost is unpriced"


def test_weekly_quality_line_names_the_worst_agent_below_the_floor():
    from app.agents.orchestrator.agent import weekly_quality_line

    line = weekly_quality_line({
        "social_media": {"success_rate": 0.5, "runs_7d": 8},
        "finance": {"success_rate": 0.6, "runs_7d": 5},
    })
    assert line is not None
    assert "social_media" in line and "50%" in line

    assert weekly_quality_line({"finance": {"success_rate": 0.8, "runs_7d": 5}}) is None
    assert weekly_quality_line({"finance": {"success_rate": 0.9, "runs_7d": 5}}) is None
    assert weekly_quality_line({"finance": {"success_rate": None, "runs_7d": 0}}) is None
    assert weekly_quality_line(None) is None
    assert weekly_quality_line({}) is None


def test_morning_run_appends_one_line_and_records_the_drop(monkeypatch):
    from app.agents.orchestrator.agent import OrchestratorAgent

    agent = OrchestratorAgent()
    monkeypatch.setattr(
        agent, "collect_structured",
        lambda task, context: ({"summary": "Plan for the day.", "tasks": [], "key_focus": "SEO"}, None),
    )
    task = {"title": "Morning planning cycle", "metadata": {"cycle": "morning"}}
    payload = agent.run(task, {"agent_quality": {"social_media": {"success_rate": 0.5, "runs_7d": 8}}})

    assert payload["summary"].startswith("Plan for the day.")
    assert payload["summary"].count("Weekly quality watch") == 1
    assert "social_media" in payload["summary"]
    drop = payload["agent_quality_drop"]
    assert drop["agents"][0]["agent_type"] == "social_media"

    quiet = agent.run({**task}, {"agent_quality": {"social_media": {"success_rate": 0.95, "runs_7d": 8}}})
    assert "Weekly quality watch" not in quiet["summary"]
    assert "agent_quality_drop" not in quiet
