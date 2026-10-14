"""U3 / L4 — H-02 (backend auditor, wave U2): the stale-claim sweep writes ONE batched activity
row per sweep naming what it recovered.

The defect this file pins (audit H-02, re-verified on the live bytes): `task_service.
requeue_stale_claims` (`:559-597`) was ONE `UPDATE … RETURNING Task.id` that returned a count and
wrote NOTHING — no activity row, no event, no reason on the requeue path (only the terminal branch
records one).  The beat task around it (`celery_app/tasks/maintenance.py:118-139`) only logged
`"task-lease sweep: recovered N of M"`.  Consequence: `/dashboard/activity` and every owner surface
could not tell that a stalled run had been requeued (B7's acceptance line has no producer).

Every test here FAILS against the pre-fix tree on the activity-row assertions (measured: zero rows —
`raw/red-requeue-activity.txt`).  The sweep's public contract is deliberately unchanged: the task
still returns `{"status", "requeued", "scanned"}` and the service still returns an `int`
(`tests/unit/celery/test_lease_sweep_task.py` and `test_queue_safety_core.py` pin both).

Harness: the shared `queue_db` fixture (file-backed SQLite behind the app-wide session factory,
driven on the Celery process loop).  The activity rows are read from the FILE, so an assertion here
is an assertion about what was really committed.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from celery_app.tasks.maintenance import requeue_stale_task_claims


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network. (The batch writer publishes nothing, but the seam stays mocked like its siblings.)"""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


def _activity_rows(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT agent_type, action, level, summary, detail FROM activity_log ORDER BY id"
    )


def _stale_claim(queue_db, title: str) -> int:
    """One `in_progress` claim whose last sign of life is older than the lease."""
    task_id = queue_db.create_task(title=title)
    queue_db.claim(task_id, "worker-dead")
    queue_db.backdate_heartbeat(task_id, seconds=settings.task_claim_lease_seconds + 60)
    return task_id


def test_a_sweep_writes_one_activity_row_naming_the_count_and_the_ids(queue_db):
    """One recovered claim -> exactly ONE `stale_claims_requeued` row, naming count + `#id`.

    RED (pre-fix): `activity_log` stays EMPTY — the recovery happens and nothing records it.
    """
    task_id = _stale_claim(queue_db, "u3-requeue-one")

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 1, "scanned": 1}
    assert queue_db.row(task_id)["status"] == "pending", "the claim really was requeued"

    rows = _activity_rows(queue_db)
    assert len(rows) == 1, f"expected exactly ONE activity row, saw {rows!r}"
    row = rows[0]
    assert row["action"] == "stale_claims_requeued"
    assert row["agent_type"] == "orchestrator"
    assert row["level"] == "warning"
    assert "1 stalled run(s) requeued" in row["summary"]
    assert f"#{task_id}" in row["summary"], "the summary names the recovered id"
    detail = json.loads(row["detail"])
    assert detail["requeued_ids"] == [task_id]
    assert detail["failed_ids"] == []
    assert detail["lease_seconds"] == settings.task_claim_lease_seconds


def test_a_no_op_sweep_writes_no_activity_row(queue_db):
    """Nothing recovered -> nothing written (the sweep must not manufacture a recovery event).

    This is also the idempotence half of the shipped pin: a second sweep reports 0 and adds no row.
    """
    fresh = queue_db.create_task(title="u3-requeue-fresh")
    queue_db.claim(fresh, "worker-alive")

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 0, "scanned": 1}
    assert _activity_rows(queue_db) == []
    assert queue_db.row(fresh)["status"] == "in_progress", "a live claim stays untouched"


def test_one_sweep_with_both_outcomes_still_writes_exactly_one_row(queue_db):
    """The terminal rule and the requeue are described by the SAME row, and told apart in it.

    An attempts-exhausted row goes `failed` (worker-died reason); a row with attempts left goes back
    to `pending`.  Both are recovered by one statement and reported by one row.

    RED (pre-fix): no row at all.
    """
    exhausted = queue_db.create_task(title="u3-requeue-exhausted")
    survivor = queue_db.create_task(title="u3-requeue-survivor")
    max_attempts = int(settings.task_max_attempts)
    for index in range(max_attempts):
        queue_db.claim(exhausted, "worker-dead")
        if index < max_attempts - 1:
            queue_db.release(exhausted, "worker-dead")
    queue_db.claim(survivor, "worker-dead")
    queue_db.backdate_heartbeat(exhausted, seconds=settings.task_claim_lease_seconds + 60)
    queue_db.backdate_heartbeat(survivor, seconds=settings.task_claim_lease_seconds + 60)

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 2, "scanned": 2}
    rows = _activity_rows(queue_db)
    assert len(rows) == 1, f"one sweep is one row, saw {rows!r}"
    detail = json.loads(rows[0]["detail"])
    assert detail["requeued_ids"] == [survivor]
    assert detail["failed_ids"] == [exhausted]
    assert "1 stalled run(s) requeued" in rows[0]["summary"]
    assert "1 sent terminal" in rows[0]["summary"], "the terminal half is named, not hidden in the count"


def test_a_multi_row_sweep_writes_exactly_one_batched_row(queue_db):
    """Batch discipline: three stale claims -> ONE row carrying all three ids (not three rows).

    RED (pre-fix): no rows.
    """
    ids = sorted(_stale_claim(queue_db, f"u3-requeue-batch-{index}") for index in range(3))

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 3, "scanned": 3}
    rows = _activity_rows(queue_db)
    assert len(rows) == 1, f"expected ONE batched row for the sweep, saw {len(rows)}"
    detail = json.loads(rows[0]["detail"])
    assert detail["requeued_ids"] == ids
    assert "3 stalled run(s) requeued" in rows[0]["summary"]
    for task_id in ids:
        assert f"#{task_id}" in rows[0]["summary"]
