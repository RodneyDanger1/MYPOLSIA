"""PHASE-3 fixer-2 / L2-03 — `agent_runs.attempt`: mapped, written, and keyed.

RED before the fix (lane-2 `probe_lane2.py` part C): `AgentRun.__table__` had no `attempt`,
`create_all` had neither the column nor `ux_agent_runs_task_attempt` (both were migration-only
in alembic 0002), and `create_agent_run` had no parameter to write it — so runs of attempt 1
and attempt 3 of one task were indistinguishable and the unique index was inert.

The seam: the celery call site that PASSES the value (`celery_app/tasks/agent_tasks.py:398`,
FIXER-3's file) passes `attempt=task.attempts`; the interface is frozen as the keyword
`attempt` here (and re-pinned in `tests/unit/celery/test_service_contract.py`).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.task import AgentRun, Task
from app.services.task_service import create_agent_run


@pytest.mark.asyncio
async def test_attempt_round_trips_through_create_agent_run(async_db_session):
    task = Task(title="t", agent_type="orchestrator")
    async_db_session.add(task)
    await async_db_session.flush()

    run = await create_agent_run(
        async_db_session, "orchestrator", task_id=task.id, attempt=3
    )
    await async_db_session.commit()

    stored = (
        await async_db_session.execute(select(AgentRun).where(AgentRun.id == run.id))
    ).scalar_one()
    assert stored.attempt == 3
    assert stored.task_id == task.id


@pytest.mark.asyncio
async def test_attempt_defaults_to_none_and_a_retry_ladder_stays_distinct(async_db_session):
    """No attempt supplied -> NULL (legal); attempts 1 and 2 of one task are two rows."""
    task = Task(title="t", agent_type="orchestrator")
    async_db_session.add(task)
    await async_db_session.flush()

    bare = await create_agent_run(async_db_session, "orchestrator", task_id=task.id)
    assert bare.attempt is None

    first = await create_agent_run(async_db_session, "orchestrator", task_id=task.id, attempt=1)
    second = await create_agent_run(async_db_session, "orchestrator", task_id=task.id, attempt=2)
    await async_db_session.commit()
    assert (first.attempt, second.attempt) == (1, 2)


@pytest.mark.asyncio
async def test_the_natural_key_fires_on_a_duplicate_attempt(async_db_session):
    """`(task_id, attempt)` is UNIQUE where `task_id IS NOT NULL` — the retry key is real."""
    task = Task(title="t", agent_type="orchestrator")
    async_db_session.add(task)
    await async_db_session.flush()

    await create_agent_run(async_db_session, "orchestrator", task_id=task.id, attempt=1)
    with pytest.raises(IntegrityError):
        await create_agent_run(async_db_session, "orchestrator", task_id=task.id, attempt=1)


@pytest.mark.asyncio
async def test_null_keys_never_collide(async_db_session):
    """Rows with no task (cycle/manual runs) and rows with no attempt both stay legal."""
    task = Task(title="t", agent_type="orchestrator")
    async_db_session.add(task)
    await async_db_session.flush()

    # no task at all, twice — the partial predicate exempts them
    await create_agent_run(async_db_session, "orchestrator", run_type="cycle")
    await create_agent_run(async_db_session, "orchestrator", run_type="cycle")
    # a task with no recorded attempt, twice — NULL attempt is distinct in both dialects
    await create_agent_run(async_db_session, "orchestrator", task_id=task.id)
    await create_agent_run(async_db_session, "orchestrator", task_id=task.id)
    await async_db_session.commit()
