"""Scheduler enqueue fallback (D2): a dead broker must not strand committed rows `pending`.

The T19 contract for the API path (a failed hand-off marks the row `failed` with the measured
reason, then the caller gets the named refusal) now covers the three scheduler fan-out sites
through one shared helper — `_common.enqueue_created`. These tests drive the REAL
`_create_and_enqueue` / `_ads_stripe_sync` bodies on the queue harness (file-backed DB behind
the app-wide session factory, driven on the Celery process loop) with `delay`/`group` raising
the same class of error a refused broker raises.

Audit reference: lane 4 finding L4-F1 (`agent_tasks.py:595,629`; `daily_cycle.py:268` at the
time of the audit — the sites now route through the helper).

RED leg (observed): a plain `raise` planted as the first line of `enqueue_created`'s except
block -> no row is marked and no named refusal is raised -> every test here fails; restore -> green.
"""
from __future__ import annotations

import pytest

from app.core.errors import PolsiaError


def _dead_broker(monkeypatch) -> None:
    """Make the single-task dispatch raise exactly like a refused broker."""
    import celery_app.tasks.agent_tasks as agent_tasks

    def dead(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead, raising=False)


def test_a_dead_broker_marks_the_sweep_row_failed_and_names_the_refusal(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _create_and_enqueue

    _dead_broker(monkeypatch)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(_create_and_enqueue("customer_support", "D2 dead-broker sweep", bucket="d2-a"))

    assert excinfo.value.code == "E_REDIS", "the task must fail with the named refusal, not the raw error"
    rows = queue_db.query(
        "SELECT id, status, error_message FROM tasks WHERE title = ?", ("D2 dead-broker sweep",)
    )
    assert len(rows) == 1, "the row was created before the dispatch"
    assert rows[0]["status"] == "failed", "a stranded `pending` row is the defect this test pins"
    assert "E_REDIS" in (rows[0]["error_message"] or ""), "the row carries the measured reason"


def test_a_dead_broker_fails_both_ads_stripe_rows(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _dead_broker(monkeypatch)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(_ads_stripe_sync())

    assert excinfo.value.code == "E_REDIS"
    rows = queue_db.query(
        "SELECT status, error_message FROM tasks WHERE agent_type IN ('ads_management','finance')"
    )
    assert len(rows) == 2, "both planned tasks were committed before the dispatch"
    assert {r["status"] for r in rows} == {"failed"}, "every stranded row must be terminal"
    assert all("E_REDIS" in (r["error_message"] or "") for r in rows)


def test_the_group_handoff_marks_all_rows_on_a_dead_broker(queue_db, monkeypatch):
    """Covers the morning-cycle fan-out path through the shared helper (group branch)."""
    import celery

    from celery_app.tasks._common import enqueue_created

    ids = [queue_db.create_task(title=f"D2 group row {i}") for i in range(2)]

    class _DeadGroup:
        def __init__(self, _sigs) -> None:
            pass

        def apply_async(self):
            raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(celery, "group", _DeadGroup)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(enqueue_created(ids, label="morning cycle"))

    assert excinfo.value.code == "E_REDIS"
    rows = queue_db.query(
        "SELECT status, error_message FROM tasks WHERE id IN (?, ?)", (ids[0], ids[1])
    )
    assert {r["status"] for r in rows} == {"failed"}
    assert all("E_REDIS" in (r["error_message"] or "") for r in rows)


def test_a_healthy_broker_leaves_rows_pending_and_dispatches_once(queue_db, monkeypatch):
    """Negative control: the fallback must not touch rows when the hand-off SUCCEEDS."""
    import celery_app.tasks.agent_tasks as agent_tasks
    from celery_app.tasks._common import enqueue_created

    sent: list[int] = []
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda tid: sent.append(tid), raising=False)

    task_id = queue_db.create_task(title="D2 happy row")
    queue_db.run(enqueue_created([task_id], label="test"))

    assert sent == [task_id], "exactly one dispatch, for the created row"
    row = queue_db.row(task_id)
    assert row["status"] == "pending", "a successful dispatch leaves the row for the worker"
