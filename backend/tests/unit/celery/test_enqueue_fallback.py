"""Scheduler enqueue fallback (D2/T19 + A2-03): a refused hand-off must not write work off.

The T19 contract for the API path (a failed hand-off is a NAMED refusal, never a fake queue) now
covers the three scheduler fan-out sites through one shared helper — `_common.enqueue_created`.
These tests drive the REAL `_create_and_enqueue` / `_ads_stripe_sync` bodies on the queue harness
(file-backed DB behind the app-wide session factory, driven on the Celery process loop) with
`delay`/`group` raising the same class of error a refused broker raises.

A2-03 (wave A3/S3): the rows are NO LONGER marked `failed`.  A Celery `group` publishes one
message at a time, so an exception proves nothing about what the broker already accepted — and
`claim_task` accepts only `pending`, which made every refused fan-out lose work that was queued.
The rows stay `pending` and each is stamped with `metadata.rearmed_at`, the marker
`maintenance._reconcile_stranded_pending` re-dispatches.  The REFUSAL is unchanged (E_REDIS).

Audit reference: lane 4 finding L4-F1 (`agent_tasks.py:595,629`; `daily_cycle.py:268` at the
time of the audit — the sites now route through the helper).

RED leg (observed): a plain `raise` planted as the first line of `enqueue_created`'s except
block -> no row is marked and no named refusal is raised -> every test here fails; restore -> green.
"""
from __future__ import annotations

import json

import pytest

from app.core.errors import PolsiaError


def _dead_broker(monkeypatch) -> None:
    """Make the single-task dispatch raise exactly like a refused broker."""
    import celery_app.tasks.agent_tasks as agent_tasks

    def dead(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead, raising=False)


def test_a_dead_broker_re_arms_the_sweep_row_and_names_the_refusal(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _create_and_enqueue

    _dead_broker(monkeypatch)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(_create_and_enqueue("customer_support", "D2 dead-broker sweep", bucket="d2-a"))

    assert excinfo.value.code == "E_REDIS", "the task must fail with the named refusal, not the raw error"
    rows = queue_db.query(
        "SELECT id, status, error_message, metadata FROM tasks WHERE title = ?",
        ("D2 dead-broker sweep",),
    )
    assert len(rows) == 1, "the row was created before the dispatch"
    # A2-03 — OLD: `rows[0]["status"] == "failed"` ("a stranded `pending` row is the defect this
    # test pins"), plus an `E_REDIS` error_message.  NEW: the row stays CLAIMABLE and carries the
    # reconciler's marker, because a refused publish cannot know whether the message was taken.
    assert rows[0]["status"] == "pending", "a refused hand-off must not terminalise the row"
    assert rows[0]["error_message"] is None, "the row is not written off, so it carries no failure"
    meta = json.loads(rows[0]["metadata"])
    assert meta["rearmed_at"], "the row is marked for the stranded-row reconciler"
    assert meta["rearm_reason"] == "enqueue_refused:ConnectionError"


def test_a_dead_broker_re_arms_both_ads_stripe_rows(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _dead_broker(monkeypatch)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(_ads_stripe_sync())

    assert excinfo.value.code == "E_REDIS"
    rows = queue_db.query(
        "SELECT status, error_message, metadata FROM tasks "
        "WHERE agent_type IN ('ads_management','finance')"
    )
    assert len(rows) == 2, "both planned tasks were committed before the dispatch"
    # OLD: `{r["status"] for r in rows} == {"failed"}` ("every stranded row must be terminal").
    # NEW: both stay `pending`, each carrying its own re-arm marker.
    assert {r["status"] for r in rows} == {"pending"}
    assert all(r["error_message"] is None for r in rows)
    assert all(json.loads(r["metadata"])["rearmed_at"] for r in rows)


def test_the_group_handoff_re_arms_all_rows_on_a_dead_broker(queue_db, monkeypatch):
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
        "SELECT status, error_message, metadata FROM tasks WHERE id IN (?, ?)", (ids[0], ids[1])
    )
    # OLD: `{r["status"] for r in rows} == {"failed"}` + an `E_REDIS` error_message on each.
    # NEW: both stay claimable and carry the marker the reconciler reads.
    assert {r["status"] for r in rows} == {"pending"}
    assert all(r["error_message"] is None for r in rows)
    assert all(json.loads(r["metadata"])["rearmed_at"] for r in rows)


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
