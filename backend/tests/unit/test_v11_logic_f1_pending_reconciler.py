"""V11/W2 F1 — the stranded-`pending` reconciler (commit->dispatch gap).

Audit finding F1: `cadence.py:78-84` and `followups.py:133-138` commit `pending` rows and then
hand them to the broker.  A crash BETWEEN the commit and the hand-off (or a broker that accepts
then loses the message) leaves the row `pending` forever: the lease sweep (`maintenance.py`)
covers `in_progress` only, and the unique dedupe key blocks the next bucket run from
re-creating the child.  This file pins the fix:

1. a stranded `pending` scheduler row (older than the reconcile window, no `dispatched_at`
   marker) is RE-DISPATCHED to the broker and the marker is stamped — T1;
2. a fresh `pending` row (or one re-dispatched recently) is left alone — T2 (the sweep never
   races the normal path, and each redispatch spaces the next attempt by the window);
3. the normal cadence hand-off STAMPS `dispatched_at` on success — T3 (the marker is real,
   not only written by the repair path);
4. a broker that refuses the redispatch is NAMED (rows stay pending for the next sweep,
   an activity row carries the failure) — T4.

The broker edge is a spy on `run_agent_task.delay` (no Redis); everything else is the real
file-backed DB on the Celery process loop (`queue_db`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from celery_app.tasks import maintenance

WINDOW_S = maintenance.PENDING_RECONCILE_AFTER_SECONDS


def _seed_pending(row_db, *, age_s: int, metadata: dict | None = None,
                  source: str = "scheduler", number: str = "T-1") -> int:
    """One `pending` row backdated `age_s` seconds (the commit-then-crash orphan shape)."""
    from app.services.task_service import create_task

    async def _go() -> int:
        async with row_db.factory() as db:
            row = await create_task(
                db, title=f"stranded {number}", agent_type="email_outreach", source=source,
            )
            row.dedupe_key = f"v11f1:{number}"
            if metadata is not None:
                row.metadata_json = metadata
            row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=age_s)
            await db.commit()
            return int(row.id)

    return row_db.run(_go())


def _delay_spy(monkeypatch, *, raises: bool = False) -> list[int]:
    from celery_app.tasks import agent_tasks

    calls: list[int] = []

    def _delay(tid: int) -> None:
        calls.append(int(tid))
        if raises:
            raise RuntimeError("broker refused (v11-f1 test)")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", _delay, raising=False)
    return calls


def _metadata(row_db, task_id: int) -> dict:
    row = row_db.row(task_id)
    import json

    raw = row.get("metadata")
    if raw in (None, "null", ""):
        return {}
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _actions(row_db) -> list[str]:
    return [r["action"] for r in row_db.query("SELECT action FROM activity_log ORDER BY id")]


def test_a_stranded_pending_row_is_redispatched_and_marked(queue_db, monkeypatch):
    calls = _delay_spy(monkeypatch)
    task_id = _seed_pending(queue_db, age_s=WINDOW_S + 600)

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert calls == [task_id], "the stranded row must be handed to the broker again"
    assert result["redispatched"] == 1
    marker = _metadata(queue_db, task_id)
    assert marker.get("dispatched_at"), "the marker proves the re-dispatch happened"
    assert "task_pending_redispatched" in _actions(queue_db)
    assert queue_db.row(task_id)["status"] == "pending"  # a hand-off, not a fake completion


def test_fresh_or_recently_redispatched_rows_are_left_alone(queue_db, monkeypatch):
    calls = _delay_spy(monkeypatch)
    fresh = _seed_pending(queue_db, age_s=60, number="T-2")
    recent = _seed_pending(
        queue_db,
        age_s=WINDOW_S + 600,
        number="T-3",
        metadata={"dispatched_at": datetime.now(UTC).isoformat()},
    )

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert calls == [], "neither the fresh row nor the freshly-redispatched row may be re-sent"
    assert result["redispatched"] == 0
    assert _metadata(queue_db, fresh) == {}
    assert queue_db.row(recent)["status"] == "pending"


def test_cadence_handoff_stamps_dispatched_at(queue_db, monkeypatch):
    from app.models.schedule import TaskSchedule
    from celery_app.tasks import cadence

    calls = _delay_spy(monkeypatch)

    async def _seed_schedule() -> None:
        async with queue_db.factory() as db:
            db.add(
                TaskSchedule(
                    name="weekly digest", prompt="write the digest", cadence="daily",
                    hour=6, weekday=None, enabled=True,
                    next_run_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
                )
            )
            await db.commit()

    queue_db.run(_seed_schedule())
    result = queue_db.run(cadence._run_due_schedules())

    assert result["created"] == 1
    assert calls == result["task_ids"]
    meta = _metadata(queue_db, result["task_ids"][0])
    assert meta.get("dispatched_at"), "the cadence hand-off must stamp the marker on success"
    assert meta.get("schedule_id") is not None  # the existing metadata is preserved


def test_a_refused_redispatch_is_named_and_left_for_the_next_sweep(queue_db, monkeypatch):
    calls = _delay_spy(monkeypatch, raises=True)
    task_id = _seed_pending(queue_db, age_s=WINDOW_S + 600, number="T-4")

    result = queue_db.run(maintenance._reconcile_stranded_pending())

    assert calls == [task_id]
    assert result["redispatched"] == 0
    assert result["failed"] == [task_id]
    assert queue_db.row(task_id)["status"] == "pending"  # stays for the next tick
    assert not _metadata(queue_db, task_id).get("dispatched_at")  # no false marker
    assert "task_pending_redispatch_failed" in _actions(queue_db)
