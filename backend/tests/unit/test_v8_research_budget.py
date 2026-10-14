"""V8/C5 — research tool budgets, S version (code-gap #11 tier (a)).

Shape: the ROLE prompt states the per-task budget; a post-hoc validator counts the fetches
RECORDED in the run's output and writes ONE honest `research_budget_exceeded` activity row when
over.  NO hard enforcement (tier (b) needs a tool-call proxy; code-gap §11 says so) — the run
itself is never blocked or failed by this check.

Metric note (recorded in RECEIPT): the run output's only countable fetch evidence is
`search_queries` (the queries the run actually listed); page READS are not distinguishable in
the output today, so the count is the recorded queries — named as such in the row's detail
(`metric="search_queries_recorded"`), never rounded up into a fabricated fetch total.
"""
from __future__ import annotations

from sqlalchemy import select

from app.agents.competitor_research.agent import (
    RESEARCH_FETCH_BUDGET,
    ROLE,
    research_budget_violation,
)
from app.models.report import ActivityLog


def _payload(count: int) -> dict:
    return {
        "search_queries": [f"query {i}" for i in range(count)],
        "findings": [],
        "competitors": [],
    }


def test_the_role_prompt_states_the_budget():
    assert f"at most {RESEARCH_FETCH_BUDGET}" in ROLE
    assert "research fetch" in ROLE.lower()


def test_over_budget_counts_the_recorded_fetches():
    violation = research_budget_violation(_payload(RESEARCH_FETCH_BUDGET + 1))

    assert violation is not None
    assert violation["count"] == RESEARCH_FETCH_BUDGET + 1
    assert violation["budget"] == RESEARCH_FETCH_BUDGET
    assert violation["metric"] == "search_queries_recorded"


def test_at_or_under_budget_is_none():
    assert research_budget_violation(_payload(RESEARCH_FETCH_BUDGET)) is None
    assert research_budget_violation(_payload(0)) is None


def test_a_malformed_payload_is_none_never_a_fabricated_count():
    assert research_budget_violation({}) is None
    assert research_budget_violation({"search_queries": "nope"}) is None
    assert research_budget_violation(None) is None


# ── the recording leg (the `_persist_run_artifacts` dispatch branch) ────────────────────────
async def test_over_budget_records_exactly_one_named_row(async_db_session, mock_redis):
    from celery_app.tasks import agent_tasks

    report = await agent_tasks._persist_run_artifacts(
        async_db_session, "competitor_research", _payload(RESEARCH_FETCH_BUDGET + 2)
    )
    await async_db_session.commit()

    assert report == {}, "nothing to persist for this agent beyond the honest record"

    rows = list((await async_db_session.execute(select(ActivityLog))).scalars().all())
    assert len(rows) == 1, "exactly ONE row when over budget"
    (row,) = rows
    assert row.action == "research_budget_exceeded"
    assert row.agent_type == "competitor_research"
    assert row.level == "warning"
    assert row.detail["count"] == RESEARCH_FETCH_BUDGET + 2
    assert row.detail["budget"] == RESEARCH_FETCH_BUDGET


async def test_under_budget_records_nothing(async_db_session, mock_redis):
    from celery_app.tasks import agent_tasks

    report = await agent_tasks._persist_run_artifacts(
        async_db_session, "competitor_research", _payload(1)
    )

    assert report == {}
    rows = list((await async_db_session.execute(select(ActivityLog))).scalars().all())
    assert rows == []
