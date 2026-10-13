"""The task-lease sweep (T1): recovery, the terminal rule, and idempotence.

What this file proves — and what it does not (§A21.3). Each test runs the TASK's logic
in-process: a direct call of `requeue_stale_task_claims`, which drives the real
`requeue_stale_claims` service on the Celery process loop against a file-backed database.
The SCHEDULING half — a beat process actually firing the entry every 5 minutes — needs a
live Redis and beat, which this host does not run: that half is `environment-only`. The
parity suite separately proves the entry is declared and loaded by the app.

Counts, precisely: `scanned` is the number of in-flight (`in_progress`) claims the sweep
examined (measured by a count query beside the sweep); `requeued` is the service's own
count of recovered claims — an attempts-exhausted row is among them and has been moved to
`failed` (reason recorded) rather than back to `pending`, which is the service's documented
contract (see `test_queue_safety_core.py::test_invariant_4_an_exhausted_row_goes_terminal`,
`== 2` for one pending-bound and one terminal row).
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from app.config import settings
from app.core.database import utcnow
from celery_app.tasks.maintenance import requeue_stale_task_claims


def _backdate_heartbeat_directly(db_path, task_id: int, seconds: int) -> None:
    """Move `heartbeat_at` beyond the lease with a direct sqlite3 UPDATE — no ORM, no identity map.

    The value is written in the exact naive-UTC text format the app's SQLite DATETIME binds
    use (`%Y-%m-%d %H:%M:%S.%f`), so the service's comparison sees it as older.
    """
    stale_since = (utcnow() - timedelta(seconds=seconds)).replace(tzinfo=None)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (stale_since.strftime("%Y-%m-%d %H:%M:%S.%f"), task_id),
        )
        con.commit()
    finally:
        con.close()


def test_lease_sweep_recovers_a_stale_claim_reports_counts_and_is_idempotent(queue_db):
    """Recovery + idempotence: one stale claim returns to `pending`; a second call is a no-op.

    RED leg (receipt §4): `celery_app.tasks.maintenance.requeue_stale_claims` replaced with
    `AsyncMock(return_value=0)` at runtime — this test fails on the returned `requeued` count.
    """
    stale = queue_db.create_task(title="lease-sweep-stale")
    queue_db.claim(stale, "worker-dead")
    _backdate_heartbeat_directly(
        queue_db.path, stale, seconds=settings.task_claim_lease_seconds + 60
    )

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 1, "scanned": 1}
    row = queue_db.row(stale)
    assert row["status"] == "pending", "the stale claim goes back to the queue"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert row["attempts"] == 1, "recovery is not a new attempt"

    # Idempotence (§A21.3): the second run must be a no-op — nothing left to recover, and the
    # row is byte-identical before/after it.
    before_second = queue_db.row(stale)
    again = requeue_stale_task_claims()
    assert again == {"status": "completed", "requeued": 0, "scanned": 0}
    assert queue_db.row(stale) == before_second, "a second sweep must touch nothing"


def test_lease_sweep_sends_an_exhausted_stale_row_terminal_with_the_reason(queue_db):
    """The terminal rule at the sweep's own surface: an exhausted stale claim becomes `failed`
    (reason recorded), and no later sweep can see it again.

    The first sweep's `requeued` includes it — the service counts every recovered claim and
    sends this one terminal inside its single UPDATE (see the module docstring). "Never
    requeued forever" is the second half: once terminal, the sweep no longer touches it.
    """
    exhausted = queue_db.create_task(title="lease-sweep-exhausted")
    max_attempts = int(settings.task_max_attempts)
    for index in range(max_attempts):
        queue_db.claim(exhausted, "worker-dead")
        if index < max_attempts - 1:
            queue_db.release(exhausted, "worker-dead")
    _backdate_heartbeat_directly(
        queue_db.path, exhausted, seconds=settings.task_claim_lease_seconds + 60
    )

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 1, "scanned": 1}
    row = queue_db.row(exhausted)
    assert row["status"] == "failed", "an exhausted task must not be requeued forever"
    assert row["error_message"] == f"lease expired after {max_attempts} attempts (worker died?)"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert row["attempts"] == max_attempts, "going terminal does not add an attempt"

    again = requeue_stale_task_claims()
    assert again == {"status": "completed", "requeued": 0, "scanned": 0}


def test_lease_sweep_leaves_a_fresh_claim_untouched(queue_db):
    """No interference: a live claim (fresh heartbeat) is not recovered, and nothing about it moves."""
    fresh = queue_db.create_task(title="lease-sweep-fresh")
    queue_db.claim(fresh, "worker-alive")
    before = queue_db.row(fresh)

    result = requeue_stale_task_claims()

    assert result == {"status": "completed", "requeued": 0, "scanned": 1}
    assert queue_db.row(fresh) == before, "a live claim must be byte-identical after the sweep"
