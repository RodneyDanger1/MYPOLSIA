"""WAVE 8 / logic-debugger — FAILURE MATRIX #3, GROUP C: IDEMPOTENCY under replay/duplicate delivery.

The queue is at-least-once (Celery `acks_late=True`, Redis redelivery after the visibility timeout),
so EVERY hop must be single-effect.  This file drives the DELIVERY level — the layer the existing
service-level pins (`already_used`, `duplicates_skipped`, the ON CONFLICT inserts) do not reach:

  C1  a message delivered TWICE after the first settled: no second run row, no second model call,
      no activity row, and the row's terminal state is not rewritten;
  C2  a message delivered twice for a PARKED row: the second delivery does not disturb the parking
      (`awaiting_approval`, its parked fingerprints, its token);
  C3  the recovery producer is itself idempotent: one lease recovery + TWO reconciler ticks hand
      the row to the broker exactly once;
  C4  a delivery whose row the reaper already FINISHED (attempts exhausted) is refused by name and
      never resurrects the row;
  C5  a duplicate delivery never double-counts the execution ladder (`tasks.attempts`).

The service-level idempotency pins (inbound re-delivery = one row, unsubscribe replay =
`already:true`, send-log dedupe, approval `already_used`, chat confirm replay) live in the wave-7
files and are cited in FAILURE-MATRIX-W8.md rather than duplicated here.
"""
from __future__ import annotations

import json

from app.config import settings
from app.core.errors import E_TASK_CLAIMED, E_TASK_NOT_RUNNABLE
from celery_app.tasks import maintenance


def _delay_spy(monkeypatch) -> list[int]:
    """Spy the ONE broker edge (`run_agent_task.delay`); no Redis in these tests."""
    from celery_app.tasks import agent_tasks

    calls: list[int] = []

    def _delay(task_id: int) -> None:
        calls.append(int(task_id))

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", _delay, raising=False)
    return calls


def _activity_actions(queue_db) -> list[str]:
    return [row["action"] for row in queue_db.query("SELECT action FROM activity_log ORDER BY id")]


def _task_row(queue_db, task_id: int) -> dict:
    return queue_db.row(task_id)


def _backdate_rearm_marker(queue_db, task_id: int) -> None:
    """Move `metadata.rearmed_at` one reconciler window into the past (the marker's own shape)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models.task import Task
    from app.services.task_service import task_metadata_bag

    async def _backdate() -> None:
        async with queue_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            bag = task_metadata_bag(row.metadata_json)
            bag["rearmed_at"] = (
                datetime.now(UTC).replace(tzinfo=None)
                - timedelta(seconds=maintenance.PENDING_RECONCILE_AFTER_SECONDS + 60)
            ).isoformat()
            row.metadata_json = bag
            await db.commit()

    queue_db.run(_backdate())


# ── C1. a duplicate delivery after a settle ─────────────────────────────────────────────────
def test_c01_a_second_delivery_after_a_settle_runs_nothing_and_writes_nothing(
    queue_db, install_run_spy, execute_body
):
    spy = install_run_spy()
    task_id = queue_db.create_task(title="duplicate delivery after settle")

    first = execute_body(task_id)
    assert first["status"] == "completed", first
    runs_after_first = queue_db.count("agent_runs")
    activity_after_first = _activity_actions(queue_db)
    row_after_first = _task_row(queue_db, task_id)
    assert runs_after_first == 1
    assert row_after_first["status"] == "completed"

    second = execute_body(task_id)  # the redelivery

    assert second.get("status") == "skipped", second
    assert second.get("reason") in (E_TASK_CLAIMED, E_TASK_NOT_RUNNABLE), second
    assert len(spy.calls) == 1, "the work ran exactly once"
    assert queue_db.count("agent_runs") == runs_after_first, "no second run row on a redelivery"
    assert _activity_actions(queue_db) == activity_after_first, (
        "a delivery that did nothing must not add a feed row: the owner would read a second run"
    )
    row = _task_row(queue_db, task_id)
    assert row["status"] == "completed", "the duplicate delivery must not rewrite the outcome"
    assert row["attempts"] == row_after_first["attempts"], (
        "a delivery that could not claim must not consume an execution"
    )


# ── C2. a duplicate delivery of a PARKED row ───────────────────────────────────────────────
def _park_row(queue_db, task_id: int) -> None:
    from app.services.task_service import get_task

    async def _park() -> None:
        async with queue_db.factory() as db:
            row = await get_task(db, task_id)
            row.status = "awaiting_approval"
            row.metadata_json = {
                **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
                "approval_required": [{"fingerprint": "fp-matrix-c2"}],
                "approval_token": "token-for-the-matrix",
                "approval_token_fingerprint": "fp-matrix-c2",
            }
            await db.commit()

    queue_db.run(_park())


def test_c02_a_duplicate_delivery_of_a_parked_row_leaves_the_parking_intact(
    queue_db, install_run_spy, execute_body
):
    spy = install_run_spy(delegate=False, result={"summary": "must never run again"})
    task_id = queue_db.create_task(title="parked row duplicate delivery")
    _park_row(queue_db, task_id)
    assert _task_row(queue_db, task_id)["status"] == "awaiting_approval"

    outcome = execute_body(task_id)

    assert outcome.get("status") == "skipped", outcome
    assert outcome.get("reason") == E_TASK_NOT_RUNNABLE, (
        "a parked row is not 'claimed by somebody else' — it is not runnable; the refusal must say "
        "which one it is"
    )
    assert len(spy.calls) == 0, "a parked row must not be re-run by a redelivery"
    after = _task_row(queue_db, task_id)
    assert after["status"] == "awaiting_approval"
    bag = json.loads(after["metadata"]) if isinstance(after["metadata"], str) else after["metadata"]
    assert bag["approval_required"] == [{"fingerprint": "fp-matrix-c2"}]
    assert bag["approval_token"] == "token-for-the-matrix", (
        "a duplicate delivery must not retire the owner's token"
    )
    assert bag["approval_token_fingerprint"] == "fp-matrix-c2"
    assert queue_db.count("agent_runs") == 0


# ── C3. the recovery producer is single-effect ─────────────────────────────────────────────
def test_c03_one_lease_recovery_plus_two_reconciler_ticks_dispatch_exactly_once(
    queue_db, install_run_spy, monkeypatch
):
    calls = _delay_spy(monkeypatch)
    spy = install_run_spy(delegate=False, result={"summary": "recovered"})
    task_id = queue_db.create_task(title="recovered row", agent_type="social_media")
    queue_db.claim(task_id, "dead-worker-1")
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)

    recovered = queue_db.run(maintenance._requeue_stale_claims_in_session())
    assert recovered["requeued"] == 1, recovered
    row = _task_row(queue_db, task_id)
    assert row["status"] == "pending"
    bag = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
    assert bag["rearm_reason"] == "stale_claim_requeued"

    # One window must pass before the reconciler treats the marker as stranded (by design: the
    # marker is also the "a live hand-off happened recently" evidence).
    _backdate_rearm_marker(queue_db, task_id)

    first_tick = queue_db.run(maintenance._reconcile_stranded_pending())
    second_tick = queue_db.run(maintenance._reconcile_stranded_pending())

    assert first_tick["redispatched"] == 1, first_tick
    assert calls == [task_id], "one recovery = one broker hand-off"
    assert second_tick["redispatched"] == 0, (
        "the acceptance must clear the re-arm marker, or every tick re-dispatches the row forever"
    )
    assert calls == [task_id], calls
    assert len(spy.calls) == 0, "the reconciler only dispatches; it never runs the work itself"


# ── C4. a redelivery after the reaper FINISHED the row ────────────────────────────────────
def test_c04_a_delivery_after_the_reaper_ended_the_row_is_refused_and_never_resurrects_it(
    queue_db, install_run_spy, execute_body
):
    spy = install_run_spy(delegate=False, result={"summary": "must never run"})
    task_id = queue_db.create_task(title="exhausted row redelivery")
    queue_db.claim(task_id, "dead-worker-1")
    queue_db.release(task_id, "dead-worker-1")
    queue_db.claim(task_id, "dead-worker-2")
    queue_db.release(task_id, "dead-worker-2")
    queue_db.claim(task_id, "dead-worker-3")
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)

    awaitable = maintenance._requeue_stale_claims_in_session()
    reaped = queue_db.run(awaitable)
    assert reaped["requeued"] == 1, reaped  # the sweep counts every recovery, terminal included
    row = _task_row(queue_db, task_id)
    assert row["status"] == "failed"
    assert "lease expired" in (row["error_message"] or "")

    outcome = execute_body(task_id)

    assert outcome.get("status") == "skipped", outcome
    assert outcome.get("reason") == E_TASK_NOT_RUNNABLE, outcome
    assert len(spy.calls) == 0
    assert queue_db.count("agent_runs") == 0
    after = _task_row(queue_db, task_id)
    assert after["status"] == "failed", "a late delivery must not resurrect a terminal row"
    assert after["error_message"] == row["error_message"], "nor rewrite why it ended"


# ── C5. the execution ladder is counted once per claim ────────────────────────────────────
def test_c05_a_requeue_and_redelivery_counts_exactly_one_new_execution(queue_db):
    """`attempts` is the ladder's counter; an off-by-one here silently halves/doubles the bound."""
    task_id = queue_db.create_task(title="attempt counter row")
    queue_db.claim(task_id, "worker-1")
    assert _task_row(queue_db, task_id)["attempts"] == 1
    queue_db.release(task_id, "worker-1")
    assert _task_row(queue_db, task_id)["attempts"] == 1, (
        "a graceful release is not an execution"
    )
    queue_db.claim(task_id, "worker-2")
    assert _task_row(queue_db, task_id)["attempts"] == 2, (
        "the redelivery's claim counts exactly ONE execution — a second increment here would "
        "halve the retry ladder (the wave-5 measured defect)"
    )
