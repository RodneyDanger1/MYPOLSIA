"""F-07 (worker half) — a partial run is stamped on the row and the feed says PARTIAL.

Lives in `tests/unit/celery/` because it drives the REAL worker body through the queue fixtures
(`queue_db`, `install_run_spy`).  The service/API halves are in
`tests/unit/test_w2_f07_execution_state.py`.
"""
from __future__ import annotations

import json


def _metadata(db, task_id: int) -> dict:
    raw = db.row(task_id).get("metadata")
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def test_a_partial_run_is_stamped_on_the_row_and_the_feed_says_partial(queue_db,
                                                                      install_run_spy):
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="partial run", agent_type="competitor_research")
    install_run_spy(delegate=False, result={
        "summary": "[partial] two of three suppliers verified",
        "payload_status": "partial",
        "verdict": {
            "status": "partial",
            "label": "[partial]",
            "reason": "one supplier page did not load",
            "source": "agent_envelope.status",
        },
    })

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "completed", "a partial run did real work: it is not a failure"
    assert outcome["payload_status"] == "partial"

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["result_summary"].startswith("[partial] ")
    bag = _metadata(queue_db, task_id)
    assert bag["result_status"] == "partial"
    assert bag["verdict"]["source"] == "agent_envelope.status"

    activity = queue_db.query(
        "SELECT action, level, detail FROM activity_log WHERE action = 'task_partial'"
    )
    assert len(activity) == 1, "the feed row names PARTIAL, it does not read task_completed"
    assert activity[0]["level"] == "warning"
    assert json.loads(activity[0]["detail"])["payload_status"] == "partial"


def test_a_clean_run_writes_no_partial_marker_and_a_success_row(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="clean run", agent_type="competitor_research")
    install_run_spy(delegate=False, result={"summary": "two suppliers verified"})

    outcome = run_agent_task(task_id)

    assert outcome["status"] == "completed"
    assert outcome["payload_status"] is None
    assert "result_status" not in _metadata(queue_db, task_id)
    actions = [row["action"] for row in queue_db.query("SELECT action FROM activity_log ORDER BY id")]
    assert "task_completed" in actions
    assert "task_partial" not in actions


def test_the_derivation_reads_the_row_the_worker_wrote(queue_db, install_run_spy):
    """The worker's write and the service's read agree — one contract, two sides."""
    from sqlalchemy import select

    from app.models.task import Task
    from app.services.task_service import task_execution_state
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="contract", agent_type="finance")
    install_run_spy(delegate=False, result={"summary": "[partial] only one of two statements available",
                            "payload_status": "partial"})
    run_agent_task(task_id)

    async def _load():
        async with queue_db.factory() as session:
            return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()

    task = queue_db.run(_load())

    assert task_execution_state(task) == ("partial", True)
