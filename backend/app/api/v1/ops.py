"""GET /api/v1/ops/events/stats + POST /api/v1/ops/events/reset — the breaker's operator surface (R22).

...plus GET /api/v1/ops/agent-quality (V9 B2, S1 #8/#9): the deterministic per-agent quality read
over the `agent_runs` ledger — runs_7d, success_rate, last_failure_at, cost_per_completed_task,
null-honest.

WHY this file exists: `events.publish_stats()` and `reset_publish_breaker()` shipped with the
activity circuit breaker (the "a skip is never silent" contract, `app/core/events.py:44-61`) and
had ZERO production callers — the counters and the recovery lever were reachable only from tests.
These routes are the owner's read/reset surface during/after a Redis incident; the 15-minute beat
tick (`celery_app.tasks.maintenance.check_activity_publish_health`) writes the same stats into the
worker's log.

PROCESS BOUNDARY (stated, not hidden): the breaker's counters are module globals in
`app.core.events`, i.e. PER PROCESS.  These routes report and reset the API process's state; the
beat tick reports the worker process's state.  Neither surface claims to see the other's numbers.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`support.py:28`, `memory.py:17`).  The reset verb needs no `refusal_guard`: it flips in-memory
counters and nothing else — no DB row, no external call, nothing that can refuse.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core import events
from app.core.timeutil import day_window, today_in
from app.models.task import AgentRun
from app.services.query_helpers import fetch_rows

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(verify_api_key)])


@router.get("/events/stats")
async def read_event_publish_stats() -> dict[str, Any]:
    """failures / skipped_while_open / breaker_open / cooldown_seconds / last_error."""
    return events.publish_stats()


@router.post("/events/reset")
async def reset_event_publish_breaker() -> dict[str, Any]:
    """Clear the breaker and its counters after an incident; returns the fresh (clean) stats."""
    events.reset_publish_breaker()
    return events.publish_stats()


# ── GET /ops/agent-quality (V9 B2, S1 #8/#9) ──────────────────────────────────────────────────

#: The lookback the quality view reports (days, company-local, today inclusive).
QUALITY_WINDOW_DAYS = 7


def _blank_stats() -> dict[str, Any]:
    return {"runs_7d": 0, "completed": 0, "failed": 0, "cost_sum": None, "cost_measured": 0,
            "last_failure_at": None}


async def _agent_quality_rows(db: AsyncSession) -> list[dict[str, Any]]:
    """One row per agent: the 7-day ledger window + the all-time last failure, null-honest.

    DETERMINISTIC, no LLM.  Every registry agent answers (a missing agent reports a measured
    `runs_7d` of 0 — the ledger genuinely holds no runs for it); `success_rate` and
    `cost_per_completed_task` stay null when their denominator or measurement is absent — never 0.
    """
    from app.agents.crew_factory import AGENT_MAP

    start, _ = day_window(db, today_in() - timedelta(days=QUALITY_WINDOW_DAYS - 1))
    window_rows = await fetch_rows(
        db,
        select(AgentRun.agent_type, AgentRun.status, AgentRun.cost_usd).where(
            AgentRun.started_at >= start
        ),
        what="agent quality runs",
    )
    stats: dict[str, dict[str, Any]] = {agent_type: _blank_stats() for agent_type in sorted(AGENT_MAP)}
    for agent_type, status, cost_usd in window_rows:
        entry = stats.setdefault(str(agent_type), _blank_stats())
        entry["runs_7d"] += 1
        if status == "completed":
            entry["completed"] += 1
            if cost_usd is not None:
                entry["cost_sum"] = (entry["cost_sum"] or 0) + cost_usd
                entry["cost_measured"] += 1
        elif status == "failed":
            entry["failed"] += 1

    # The newest failure per agent, ALL TIME (the 7-day window is a rate; "when did this agent
    # last break" is a timestamp).  `coalesce(ended_at, started_at)` reads the best instant a run
    # carries; a failed row with neither stays out instead of inventing a date.
    failure_rows = await fetch_rows(
        db,
        select(
            AgentRun.agent_type,
            func.max(func.coalesce(AgentRun.ended_at, AgentRun.started_at)).label("last_failure_at"),
        )
        .where(AgentRun.status == "failed")
        .group_by(AgentRun.agent_type),
        what="agent last failures",
    )
    for agent_type, last_failure_at in failure_rows:
        stats.setdefault(str(agent_type), _blank_stats())["last_failure_at"] = last_failure_at

    rows: list[dict[str, Any]] = []
    for agent_type in sorted(stats):
        entry = stats[agent_type]
        terminal = int(entry["completed"]) + int(entry["failed"])
        rows.append(
            {
                "agent_type": agent_type,
                "runs_7d": int(entry["runs_7d"]),
                "success_rate": (int(entry["completed"]) / terminal if terminal else None),
                "last_failure_at": (
                    entry["last_failure_at"].isoformat()
                    if entry["last_failure_at"] is not None
                    else None
                ),
                "cost_per_completed_task": (
                    float(entry["cost_sum"]) / int(entry["completed"])
                    if entry["cost_measured"] and int(entry["completed"])
                    else None
                ),
            }
        )
    return rows


@router.get("/agent-quality")
async def agent_quality(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """`{agents: [{agent_type, runs_7d, success_rate, last_failure_at, cost_per_completed_task}]}`."""
    with refusal_guard():
        rows = await _agent_quality_rows(db)
    return {"agents": rows}
