"""V11/W2 F3 — the planner money gate: `/planner/propose` must not run agent work unmetered.

Audit finding F3: `planner_service.propose` (planner_service.py:422-439) ran two agent steps
(competitor_research fetch + company_planner model call) with NO `assert_budget_allows`, no
concurrency dedupe and no bounded timeout — the day's spend cap was enforced at the worker
dispatch choke point and this route bypasses it entirely.

Pinned here:
1. an exhausted day cap REFUSES before any agent work runs — `E_BUDGET_EXCEEDED` with
   `how_to_fix` (T1);
2. the healthy path still proposes (3-5 candidates) — T2 (regression);
3. a step that overruns the bounded step timeout is refused BY NAME and the next step never
   starts — T3;
4. a second concurrent propose is refused while the first is in flight (in-flight dedupe) — T4.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.errors import E_BUDGET_EXCEEDED, E_CLI_TIMEOUT, E_TASK_CLAIMED, PolsiaError
from app.services import planner_service

FETCHED = "https://research.example/pricing"


def research_payload() -> dict:
    return {
        "summary": "notes",
        "competitors": [{"name": "Incumbent", "source_urls": [FETCHED], "sources": [FETCHED]}],
        "findings": ["prices from $100"],
        "search_queries": ["q1", "q2"],
    }


def idea(**over) -> dict:
    value = {
        "name": "Cafe launch kit",
        "one_liner": "one page + email sequence for one cafe",
        "buyer": "independent cafe owners",
        "price_band": [100.0, 150.0],
        "channel": "stripe payment link",
        "unit_cost_sketch": "2h setup",
        "est_variable_cost": 20.0,
        "launch_cost_usd": 50.0,
        "days_to_first_dollar": 10,
        "competition_level": "medium",
        "fleet_fit": "high",
        "demand_grade": "likely",
        "owner_hours_per_week_needed": 4,
        "claims": [{"claim": "cafes pay $100+", "source_url": FETCHED, "quote": "from $100"}],
    }
    value.update(over)
    return value


def ideas(n: int = 3) -> list[dict]:
    out = []
    for i in range(n):
        row = idea()
        row["name"] = f"{row['name']} {i + 1}"
        out.append(row)
    return out


@pytest.fixture
async def company(async_db_session):
    from app.models.company import CompanyConfig

    row = CompanyConfig(
        name="Planner Budget Co", owner_hours_per_week=10, owner_budget_usd=Decimal("500.00")
    )
    async_db_session.add(row)
    await async_db_session.commit()
    return row


def _spies(monkeypatch):
    calls: list[str] = []

    def _research(profile):
        calls.append("research")
        return research_payload()

    def _proposal(profile, research):
        calls.append("model")
        return {"ideas": ideas(3), "agent_type": "company_planner"}

    monkeypatch.setattr(planner_service, "_research_sync", _research)
    monkeypatch.setattr(planner_service, "_proposal_sync", _proposal)
    return calls


async def _spend(db, amount: str) -> None:
    from app.models.task import AgentRun

    db.add(
        AgentRun(
            agent_type="competitor_research",
            cost_usd=Decimal(amount),
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await db.commit()


async def test_an_exhausted_day_cap_refuses_before_any_agent_work(async_db_session, company, monkeypatch, mock_redis):
    calls = _spies(monkeypatch)
    await _spend(async_db_session, "999.00")  # far above the default $25/day cap

    with pytest.raises(PolsiaError) as excinfo:
        await planner_service.propose(async_db_session)

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert excinfo.value.how_to_fix, "a refusal must say how to fix it"
    assert calls == [], "no agent work may run once the day cap is exhausted"


async def test_the_healthy_path_still_proposes(async_db_session, company, monkeypatch, mock_redis):
    calls = _spies(monkeypatch)

    result = await planner_service.propose(async_db_session)

    assert len(result["candidates"]) == 3
    assert calls == ["research", "model"]
    assert await planner_service.status(async_db_session)  # the ledger row landed


async def test_a_step_over_the_bounded_timeout_is_refused_by_name(async_db_session, company, monkeypatch, mock_redis):
    import time

    calls: list[str] = []

    def _slow_research(profile):
        calls.append("research")
        time.sleep(0.5)
        return research_payload()

    monkeypatch.setattr(planner_service, "_research_sync", _slow_research)
    monkeypatch.setattr(planner_service, "_proposal_sync", lambda p, r: calls.append("model") or {"ideas": ideas(3)})
    monkeypatch.setattr(planner_service, "PLANNER_STEP_TIMEOUT_SECONDS", 0.05, raising=False)

    with pytest.raises(PolsiaError) as excinfo:
        await planner_service.propose(async_db_session)

    assert excinfo.value.code == E_CLI_TIMEOUT
    assert excinfo.value.how_to_fix
    assert calls == ["research"], "the model step must never start after a timed-out step"


async def test_a_concurrent_propose_is_refused_while_one_is_in_flight(async_db_session, company, monkeypatch, mock_redis):
    release = threading.Event()

    def _blocking_research(profile):
        release.wait(10)
        return research_payload()

    monkeypatch.setattr(planner_service, "_research_sync", _blocking_research)
    monkeypatch.setattr(planner_service, "_proposal_sync", lambda p, r: {"ideas": ideas(3)})

    first = asyncio.create_task(planner_service.propose(async_db_session))
    try:
        for _ in range(500):  # wait until the first call is inside its agent step
            await asyncio.sleep(0.01)
            if getattr(planner_service, "_PROPOSE_IN_FLIGHT", None):
                break
        with pytest.raises(PolsiaError) as excinfo:
            await planner_service.propose(async_db_session)
        assert excinfo.value.code == E_TASK_CLAIMED
        assert excinfo.value.how_to_fix
    finally:
        release.set()
    result = await first
    assert len(result["candidates"]) == 3
