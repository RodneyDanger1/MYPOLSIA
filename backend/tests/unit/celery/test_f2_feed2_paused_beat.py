"""F2-FEED-2 — a PAUSED company's scheduler cannot silently spend (owner STOP at the creation site).

The measured defect this file pins (SWARM-PLAN 2026-10-28, F2-FEED-2, REPRODUCED twice):

* `docker-compose.yml` gives `celery_beat` `restart: unless-stopped`, so any compose recreate
  resurrects the scheduler even when the round contract says beat is OFF;
* until this fix the owner's STOP (`company_config.dispatch_paused`) was enforced only at CLAIM
  time (`agent_tasks.run_agent_task` releases the claim and refuses) — so a resurrected Beat could
  still CREATE scheduler rows, and a cycle could still run its OWN orchestrator model call, while
  the owner had pressed STOP;
* the queue's automatic recovery is the spend path, and it is live-proven: at 2026-10-28
  19:30:06 UTC a queued `reconcile_stranded_pending_tasks` tick ran inside a restarted worker,
  re-dispatched four stranded rows (`redispatched: 4`, worker log quoted in the lane REPORT), and
  four real DeepSeek calls were made with no owner action — `agent_runs` 295-298, $0.0029.

The fix (`celery_app/tasks/**`): `_common.create_task_deduped` — the one creation funnel every
Beat fan-out uses — refuses by name (`E_POLICY_DENIED`) before the first INSERT; each Beat body
(cadence, follow-ups, morning/evening cycles, sweeps, night task, stranded-pending reconciler)
checks the same resolver FIRST and returns a named skip, so a paused tick is a clean, observable
no-op instead of an exception or an unattended run.

RED leg (recorded in the lane REPORT, raw JUnit under `raw/`): removing the funnel guard or the
reconciler guard makes the named tests below FAIL (a row is created / a delivery is made), which
is the pre-fix behaviour.  Restored byte-exact, they pass.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app.config as app_config
from app.core.errors import E_POLICY_DENIED, MeridianError
from celery_app.tasks import maintenance

PAUSED_PHRASE = "company paused by owner"


# ── harness helpers ──────────────────────────────────────────────────────────────────────────


def _seed_company(queue_db, **over) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with queue_db.factory() as db:
            db.add(CompanyConfig(name="Feed2 Co", **over))
            await db.commit()

    queue_db.run(_go())


def _set_paused(queue_db, value: bool) -> None:
    from sqlalchemy import select

    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with queue_db.factory() as db:
            row = (await db.execute(select(CompanyConfig).limit(1))).scalar_one()
            row.dispatch_paused = value
            await db.commit()

    queue_db.run(_go())


def _delay_spy(monkeypatch) -> list[int]:
    """Spy the ONE broker edge (`run_agent_task.delay`); no Redis in these tests."""
    from celery_app.tasks import agent_tasks

    calls: list[int] = []

    def _delay(tid: int) -> None:
        calls.append(int(tid))

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", _delay, raising=False)
    return calls


class _GroupRecorder:
    """Stands in for `celery.group` (imported at call time by `_common.enqueue_created`)."""

    def __init__(self) -> None:
        self.apply_async_count = 0

    def __call__(self, sigs):
        return self

    def apply_async(self, *args, **kwargs) -> None:
        self.apply_async_count += 1


def _seed_due_schedule(queue_db) -> None:
    from app.models.schedule import TaskSchedule

    async def _go() -> None:
        async with queue_db.factory() as db:
            db.add(
                TaskSchedule(
                    name="feed2 digest",
                    prompt="write the digest",
                    cadence="daily",
                    hour=6,
                    weekday=None,
                    enabled=True,
                    next_run_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
                )
            )
            await db.commit()

    queue_db.run(_go())


def _schedule_next_run(queue_db) -> str:
    rows = queue_db.query("SELECT next_run_at FROM task_schedules ORDER BY id")
    return str(rows[0]["next_run_at"]) if rows else ""


def _seed_stranded_pending(queue_db) -> int:
    """The commit->crash orphan shape: a `pending` scheduler row older than one window."""
    from app.services.task_service import create_task

    async def _go() -> int:
        async with queue_db.factory() as db:
            row = await create_task(
                db, title="stranded feed2 row", agent_type="email_outreach", source="scheduler"
            )
            row.dedupe_key = "f2feed2:stranded"
            row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=maintenance.PENDING_RECONCILE_AFTER_SECONDS + 600
            )
            await db.commit()
            return int(row.id)

    return queue_db.run(_go())


# ── 1. the creation funnel: the one gate every Beat fan-out funnels through ─────────────────


def test_the_creation_funnel_refuses_by_name_and_writes_no_row(queue_db):
    from celery_app.tasks._common import create_task_deduped

    _seed_company(queue_db, dispatch_paused=True)

    async def _create():
        async with queue_db.factory() as db:
            return await create_task_deduped(
                db,
                title="beat row while paused",
                agent_type="social_media",
                dedupe_key="f2feed2:paused-funnel",
            )

    with pytest.raises(MeridianError) as excinfo:
        queue_db.run(_create())

    assert excinfo.value.code == E_POLICY_DENIED
    assert PAUSED_PHRASE in excinfo.value.message
    assert excinfo.value.context.get("created") == 0
    assert queue_db.count("tasks") == 0, "no row may be written while the owner has pressed STOP"


def test_an_unpaused_company_creates_the_same_row(queue_db):
    from celery_app.tasks._common import create_task_deduped

    _seed_company(queue_db, dispatch_paused=False)

    async def _create():
        async with queue_db.factory() as db:
            task = await create_task_deduped(
                db,
                title="beat row while open",
                agent_type="social_media",
                dedupe_key="f2feed2:open-funnel",
            )
            await db.commit()
            return int(task.id)

    task_id = queue_db.run(_create())
    assert task_id > 0
    assert queue_db.row(task_id)["status"] == "pending"


# ── 2. the Beat bodies: a paused tick is a NAMED skip, not a queued row ─────────────────────


def test_a_paused_cadence_tick_names_the_pause_and_consumes_no_due_occurrence(
    queue_db, monkeypatch
):
    from celery_app.tasks import cadence

    calls = _delay_spy(monkeypatch)
    _seed_company(queue_db, dispatch_paused=True)
    _seed_due_schedule(queue_db)
    due_before = _schedule_next_run(queue_db)

    result = queue_db.run(cadence._run_due_schedules())

    assert result["status"] == "skipped", result
    assert result["reason"] == "dispatch_paused"
    assert result["code"] == E_POLICY_DENIED
    assert PAUSED_PHRASE in result["detail"]
    assert result["created"] == 0
    assert calls == [], "a paused tick hands nothing to the broker"
    assert queue_db.count("tasks") == 0, "a paused tick creates no task row"
    assert _schedule_next_run(queue_db) == due_before, (
        "the due occurrence is NOT consumed by a paused tick — resume must still see it"
    )

    # Resume: the same due row now creates its child and hands it off — the gate is not sticky.
    _set_paused(queue_db, False)
    resumed = queue_db.run(cadence._run_due_schedules())
    assert resumed["created"] == 1, resumed
    assert calls == resumed["task_ids"]
    assert queue_db.count("tasks") == 1


def test_a_paused_social_sweep_returns_a_named_skip_without_a_row(queue_db, monkeypatch):
    from celery_app.tasks import agent_tasks

    calls = _delay_spy(monkeypatch)
    _seed_company(queue_db, dispatch_paused=True)

    result = agent_tasks.run_social_sweep()

    assert result["status"] == "skipped", result
    assert result["reason"] == "dispatch_paused"
    assert result["code"] == E_POLICY_DENIED
    assert PAUSED_PHRASE in result["detail"]
    assert queue_db.count("tasks") == 0
    assert calls == []


def test_a_paused_morning_cycle_spends_nothing_and_consumes_no_day_claim(queue_db, monkeypatch):
    """The cycle's OWN orchestrator call is real money — the pause must refuse it up front."""
    import app.agents.crew_factory as crew_factory
    from celery_app.tasks import daily_cycle

    class _CycleSpy:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, agent_type, task_dict, context):
            self.calls.append({"agent_type": agent_type, "task": dict(task_dict or {})})
            return {"summary": "cycle plan", "tasks": [], "insights": []}

    spy = _CycleSpy()
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    recorder = _GroupRecorder()
    monkeypatch.setattr("celery.group", recorder)

    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "morning_cycle_hour", 6)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 13, 6, 30, tzinfo=UTC))
    _seed_company(queue_db, dispatch_paused=True)

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "skipped", result
    assert result["reason"] == "dispatch_paused"
    assert result["code"] == E_POLICY_DENIED
    assert result["cycle"] == "morning"
    assert spy.calls == [], "no model call may happen while the owner has pressed STOP"
    assert queue_db.query("SELECT id FROM agent_runs") == [], "refused before execution: no ledger row"
    assert queue_db.count("tasks") == 0, "no planned work on a paused tick"
    paused_rows = queue_db.query(
        "SELECT summary FROM activity_log WHERE action = 'cycle_dispatch_paused'"
    )
    assert len(paused_rows) == 1, "the refusal is owner-visible in the activity feed"
    assert "morning" in paused_rows[0]["summary"]

    # Resume: the SAME in-window tick runs — the paused tick consumed no (cycle, day) claim.
    _set_paused(queue_db, False)
    resumed = daily_cycle.run_morning_cycle()

    assert resumed["status"] == "completed", resumed
    assert len(spy.calls) == 1, "exactly one orchestrator call on the resumed tick"
    runs = queue_db.query("SELECT run_type FROM agent_runs")
    assert len(runs) == 1 and runs[0]["run_type"] == "cycle"


# ── 3. the spend path itself: the stranded-row reconciler (the live 19:30:06 UTC tick) ──────


def test_a_paused_stranded_pending_reconciler_hands_nothing_to_the_broker(queue_db, monkeypatch):
    """This is the exact function whose live tick re-dispatched four rows and spent $0.0029."""
    calls = _delay_spy(monkeypatch)
    _seed_company(queue_db, dispatch_paused=True)
    task_id = _seed_stranded_pending(queue_db)

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert result["status"] == "skipped", result
    assert result["reason"] == "dispatch_paused"
    assert result["code"] == E_POLICY_DENIED
    assert result["redispatched"] == 0
    assert result["scanned"] == 0
    assert calls == [], "a paused reconciler must not re-feed the dispatcher"
    assert queue_db.row(task_id)["status"] == "pending", "the work is preserved, not destroyed"

    # Resume: the same stranded row is re-dispatched by the next tick (nothing was lost).
    _set_paused(queue_db, False)
    resumed = queue_db.run(maintenance._reconcile_stranded_pending())
    assert resumed["redispatched"] == 1, resumed
    assert calls == [task_id]


def test_the_lease_reaper_still_requeues_a_dead_workers_claim_while_paused(queue_db):
    """Reaping a dead claim is BOOKKEEPING, not spend — it must keep working while paused.

    The row returns to `pending` (claimable after resume) and no run ledger row is created.
    """
    _seed_company(queue_db, dispatch_paused=True)
    task_id = queue_db.create_task(title="dead worker claim", agent_type="social_media")
    queue_db.claim(task_id, "dead-worker-1")
    queue_db.backdate_heartbeat(task_id, 3600)

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert result["requeued"] == 1, result
    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None and row["heartbeat_at"] is None
    assert queue_db.count("agent_runs") == 0


# ── 4. the last automatic hop: a row that already existed when the pause began ──────────────


def test_a_row_created_before_the_pause_refuses_at_dispatch_with_no_run_row(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="queued before pause", agent_type="business_planning")
    _seed_company(queue_db, dispatch_paused=True)
    spy = install_run_spy(delegate=False, result={"summary": "must never run"})

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "skipped", outcome
    assert outcome["requeued"] is True, "the work stays queued for resume"
    assert outcome["task_status"] == "pending"
    assert E_POLICY_DENIED in outcome["reason"]
    assert PAUSED_PHRASE in outcome["reason"]
    assert spy.calls == [], "the agent must never be dispatched while paused"
    assert queue_db.count("agent_runs") == 0, "no run ledger row for a refused dispatch"
