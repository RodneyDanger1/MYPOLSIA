"""V8/C5 (V8-D6) — company pause/resume enforcement at the dispatch decision.

Mechanism (recorded in lane STATE.md + RECEIPT): `dispatch_preflight` is sync and holds no DB
session, so the flag is resolved ASYNCHRONOUSLY where a session exists —
`crew_factory.resolve_dispatch_paused(db)` reads `company_config.dispatch_paused` (the 0008
column, c2) — the worker gate in `_execute_agent_task` refuses BEFORE anything runs (rejected
row + `skipped` delivery, the same shape as the budget gate), and the resolved flag is handed
to the sync seam (`dispatch_preflight(..., dispatch_paused=...)` /
`run_agent_for_task(..., dispatch_paused=...)`) so a direct caller cannot bypass the decision.
"""
from __future__ import annotations

import pytest

from app.agents import crew_factory
from app.core.errors import E_POLICY_DENIED, PolsiaError
from app.models.company import CompanyConfig

PAUSED_LINE = "company paused by owner"


def _seed_company(qdb, **over) -> None:
    async def _go():
        async with qdb.factory() as db:
            db.add(CompanyConfig(name="Pause Co", **over))
            await db.commit()

    qdb.run(_go())


# ── the resolver: the DB flag is what the decision reads ─────────────────────────────────────
async def test_the_resolver_reads_the_flag_from_the_company_row(async_db_session):
    assert await crew_factory.resolve_dispatch_paused(async_db_session) is False, (
        "no company row -> not paused (the DDL default is false)"
    )

    row = CompanyConfig(name="Pause Co", dispatch_paused=True)
    async_db_session.add(row)
    await async_db_session.commit()
    assert await crew_factory.resolve_dispatch_paused(async_db_session) is True

    row.dispatch_paused = False
    await async_db_session.commit()
    assert await crew_factory.resolve_dispatch_paused(async_db_session) is False


# ── the sync seam: dispatch_preflight / run_agent_for_task ───────────────────────────────────
def test_dispatch_preflight_refuses_when_the_flag_is_paused(monkeypatch):
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)

    problems = crew_factory.dispatch_preflight("email_outreach", dispatch_paused=True)
    assert any(PAUSED_LINE in p for p in problems), problems

    assert crew_factory.dispatch_preflight("email_outreach", dispatch_paused=False) == []


def test_run_agent_for_task_refuses_with_e_policy_denied_when_paused(monkeypatch):
    monkeypatch.delenv("POLSIA_KILL_SWITCH", raising=False)

    with pytest.raises(PolsiaError) as excinfo:
        crew_factory.run_agent_for_task("email_outreach", {}, {}, dispatch_paused=True)

    assert excinfo.value.code == E_POLICY_DENIED
    assert PAUSED_LINE in excinfo.value.message


# ── the worker gate: nothing runs while paused ──────────────────────────────────────────────
def test_a_paused_company_refuses_the_run_before_anything_executes(
    queue_db, install_run_spy, mock_redis
):
    from celery_app.tasks.agent_tasks import run_agent_task

    _seed_company(queue_db, dispatch_paused=True)
    task_id = queue_db.create_task(title="paused refusal", agent_type="email_outreach")
    spy = install_run_spy(delegate=False, result={"summary": "must never run"})

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "rejected"
    assert PAUSED_LINE in outcome["reason"], "the refusal names the pause"
    assert E_POLICY_DENIED in outcome["reason"], "the refusal carries the policy code"

    row = queue_db.row(task_id)
    assert row["status"] == "rejected", (
        "refused before anything ran: the live product's Rejected state, not `failed`"
    )
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert E_POLICY_DENIED in (row["error_message"] or "")
    assert spy.calls == [], "the agent must never be dispatched while the company is paused"
    assert queue_db.count("agent_runs") == 0, "no run ledger row for a refused dispatch"


def test_an_unpaused_company_passes_the_gate_and_the_run_completes(
    queue_db, install_run_spy, mock_redis
):
    from celery_app.tasks.agent_tasks import run_agent_task

    _seed_company(queue_db, dispatch_paused=False)
    task_id = queue_db.create_task(title="unpaused pass", agent_type="business_planning")
    spy = install_run_spy(delegate=False, result={"summary": "ran while unpaused"})

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "completed"
    assert len(spy.calls) == 1, "the pause gate must not refuse an unpaused company"
    assert queue_db.row(task_id)["status"] == "completed"
