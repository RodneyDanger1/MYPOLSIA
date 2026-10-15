"""WAVE V15 / E3 item 3-03 (S1-03, decision #2 LOCKED) — pause must not destroy queued work.

RED-first pin for BUG-DIGEST MAJOR §B7: on `dispatch_paused` the worker wrote the claimed row
terminal `rejected` and cleared its claim — the queued work was DESTROYED and nothing re-armed
it on resume.  The locked decision: a paused dispatch releases the claim cleanly (row back to
`pending` with a named paused reason), the work SURVIVES, and the resume path re-arms it
EXACTLY ONCE (no double-run — the claim stays atomic).

Re-arm mechanism (in-lane, no cross-lane file): the paused release stamps
`tasks.metadata["paused_release"]`; the `rearm_paused_released_tasks` maintenance tick (every
5 min, `maintenance.py`) re-dispatches marked `pending` rows only while the company is NOT
paused, clearing the marker on acceptance — so the resume path is the next dispatch tick.
"""
from __future__ import annotations

from celery_app.tasks import maintenance

PAUSED_LINE = "company paused by owner"


def _seed_company(qdb, *, paused: bool) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            db.add(CompanyConfig(name="Pause Co", dispatch_paused=paused))
            await db.commit()

    qdb.run(_go())


def _set_paused(qdb, paused: bool) -> None:
    from sqlalchemy import select

    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            row = (await db.execute(select(CompanyConfig))).scalars().first()
            row.dispatch_paused = paused
            await db.commit()

    qdb.run(_go())


class DelayRecorder:
    """`run_agent_task` stand-in: records `.delay` hand-offs, contacts no broker."""

    def __init__(self) -> None:
        self.delivered: list[int] = []

    def delay(self, task_id: int) -> None:
        self.delivered.append(int(task_id))

    def s(self, task_id: int):
        return ("sig", int(task_id))


def _install_delay(monkeypatch) -> DelayRecorder:
    rec = DelayRecorder()
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)
    return rec


def _rearm(qdb) -> dict:
    return qdb.run(maintenance._rearm_paused_released_tasks())


def test_paused_dispatch_releases_the_row_instead_of_destroying_it(
    queue_db, install_run_spy, mock_redis
):
    from celery_app.tasks.agent_tasks import run_agent_task

    _seed_company(queue_db, paused=True)
    task_id = queue_db.create_task(title="paused but survives", agent_type="email_outreach")
    spy = install_run_spy(delegate=False, result={"summary": "must never run while paused"})

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "pending", (
        "a paused dispatch must release the claim back to `pending`, not destroy the work"
    )
    assert PAUSED_LINE in outcome["reason"]

    row = queue_db.row(task_id)
    assert row["status"] == "pending", "the queued work survives the pause"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert spy.calls == [], "no provider call while paused"
    assert queue_db.count("agent_runs") == 0

    # The named paused reason is durable on the row (metadata marker) and in the feed.
    assert "paused_release" in (row["metadata"] or ""), row.get("metadata")
    paused_rows = queue_db.query(
        "SELECT summary FROM activity_log WHERE action = 'dispatch_refused_paused'"
    )
    assert paused_rows and "paused" in paused_rows[0]["summary"].lower()


def test_resume_rearms_exactly_once_and_the_work_runs_once(
    queue_db, install_run_spy, mock_redis, monkeypatch
):
    from celery_app.tasks.agent_tasks import run_agent_task

    _seed_company(queue_db, paused=True)
    task_id = queue_db.create_task(title="queued over the pause", agent_type="business_planning")
    spy = install_run_spy(delegate=False, result={"summary": "ran after resume"})

    assert run_agent_task(task_id)["task_status"] == "pending"

    # Still paused: the re-arm tick must NOT hand anything to the broker.
    rec = _install_delay(monkeypatch)
    while_paused = _rearm(queue_db)
    assert rec.delivered == [], "re-arm while paused would churn claim/release — it must wait"
    assert while_paused["status"] == "completed" and while_paused["rearmed"] == []

    # Resume.
    _set_paused(queue_db, False)
    resumed = _rearm(queue_db)
    assert resumed["rearmed"] == [task_id], resumed
    assert rec.delivered == [task_id], "exactly ONE hand-off on resume"

    # Idempotent: the marker is cleared on acceptance — the next tick re-arms nothing.
    again = _rearm(queue_db)
    assert again["rearmed"] == []
    assert rec.delivered == [task_id], "re-arm is exactly-once"

    # The re-delivered message runs the work once; a second tick cannot duplicate it.
    outcome = run_agent_task(task_id)
    assert outcome["status"] == "completed"
    assert len(spy.calls) == 1
    assert queue_db.row(task_id)["status"] == "completed"
    after = _rearm(queue_db)
    assert after["rearmed"] == [] and len(spy.calls) == 1


def test_rearm_tick_keeps_a_broker_refusal_retryable(queue_db, mock_redis, monkeypatch):
    """A broker refusal must NOT clear the marker — the row stays for the next tick."""
    from celery_app.tasks.agent_tasks import run_agent_task

    _seed_company(queue_db, paused=True)
    task_id = queue_db.create_task(title="refused broker", agent_type="social_media")
    assert run_agent_task(task_id)["task_status"] == "pending"
    _set_paused(queue_db, False)

    class Refusing:
        def __init__(self) -> None:
            self.attempts: list[int] = []

        def delay(self, tid: int) -> None:
            self.attempts.append(int(tid))
            raise ConnectionError("broker down")

        def s(self, tid: int):
            return ("sig", int(tid))

    refusing = Refusing()
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", refusing)

    result = _rearm(queue_db)
    assert refusing.attempts == [task_id]
    assert result["rearmed"] == [] and result["failed"] == [task_id]
    assert "paused_release" in (queue_db.row(task_id)["metadata"] or ""), (
        "the marker survives a refused hand-off so the next tick retries it"
    )


def test_beat_entry_for_the_rearm_tick_exists():
    from celery_app.beat_schedule import beat_schedule

    entry = beat_schedule["paused-release-rearm"]
    assert entry["task"] == "celery_app.tasks.maintenance.rearm_paused_released_tasks"
    assert int(entry["options"]["expires"]) < 300
