"""WAVE 8 / logic-debugger — FAILURE MATRIX #3, GROUP D: CRASH / REAPER windows.

THE INVARIANT: no row may wedge.  A crash is not an exception — it is the absence of every write
that was going to happen.  So each row here simulates a REAL kill (the worker stops mid-flight: no
settle, no heartbeat) and then asks the shipped reapers to recover within their declared bounds:

  D1  a dead worker's claim: recovered to `pending` by the lease sweep, with the re-arm marker so
      the stranded-row reconciler re-dispatches it (no run ledger row is invented for a run that
      never settled);
  D2  a crash-looping row: the attempt ladder ENDS it by name (terminal) instead of re-running it
      forever;
  D3  the recovery sweep is itself idempotent: a second tick recovers nothing;
  D4  a killed worker's `agent_runs` row: closed `cancelled` by the stale-run reaper with the named
      interruption record (`E_RUN_STALE`, cost scope, provider-call count) and ONE activity row;
  D5  a database failure at the SETTLE: the named `E_DB` propagates (never a fake success), the row
      is not left half-written, and the lease sweep recovers it — the same bound as D1;
  D6  a LATE settle on an already-reaped run (a suspended process resuming): the reaper's record
      survives and the settle is ANNOTATED, never silently contradictory (wave-7 W7-LD-03);
  D7  a late FAILED settle must keep the reaper's sentence on `raw_log` (OPEN on the wave-7 bytes
      — this is W8-LD-04, fixed in this lane; the RED leg is in raw/w8/RED-w8-ld-04.txt);
  D8  a late settle that SUPPLIES `usage_json` must still keep the interruption block (the same
      defect class as D7, one field over).

Bounds quoted (measured in GROUP B): lease 900 s + one 300 s tick = 1200 s worst-case recovery;
`AGENT_RUN_STALE_AFTER_SECONDS` = 7 200 s for the run ledger.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import settings
from app.core.errors import E_DB, PolsiaError
from celery_app.tasks import maintenance
from celery_app.tasks.maintenance import (
    AGENT_RUN_STALE_AFTER_SECONDS,
    _close_stale_runs_in_session,
)


def _task_row(queue_db, task_id: int) -> dict:
    return queue_db.row(task_id)


def _values(row: dict) -> dict:
    return json.loads(row["usage_json"]) if isinstance(row["usage_json"], str) else row["usage_json"]


def _create_running_run(queue_db, *, started_ago_s: int, usage: dict | None = None) -> int:
    from app.models.task import AgentRun

    task_id = queue_db.create_task(title="reaper window run")

    async def _make() -> int:
        async with queue_db.factory() as db:
            run = AgentRun(
                task_id=task_id,
                agent_type="finance",
                status="running",
                started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=started_ago_s),
                usage_json=usage,
            )
            db.add(run)
            await db.commit()
            return int(run.id)

    return queue_db.run(_make())


def _settle_late(queue_db, run_id: int, **kwargs) -> None:
    from app.services.task_service import finish_agent_run

    async def _settle() -> None:
        async with queue_db.factory() as db:
            await finish_agent_run(db, run_id, kwargs.pop("status"), **kwargs)
            await db.commit()

    queue_db.run(_settle())


# ── D1. the dead worker's claim ─────────────────────────────────────────────────────────────
def test_d01_a_dead_workers_claim_is_recovered_inside_the_declared_bound(queue_db):
    task_id = queue_db.create_task(title="dead worker")
    queue_db.claim(task_id, "dead-worker-1")
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert result["requeued"] == 1, result
    row = _task_row(queue_db, task_id)
    assert row["status"] == "pending", "the work is preserved, not lost"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert queue_db.count("agent_runs") == 0, (
        "a run that never settled leaves NO ledger row: an invented one would claim work happened"
    )
    bag = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
    assert bag["rearm_reason"] == "stale_claim_requeued"
    assert bag["rearmed_at"]
    # The declared recovery bound: a live claim is only taken past the lease, and a dead one is
    # found by the NEXT tick — lease + one interval, measured by the schedule itself.
    from celery_app.beat_schedule import beat_schedule

    assert settings.task_claim_lease_seconds + float(
        beat_schedule["task-lease-sweep"]["schedule"]
    ) == 1200.0


# ── D2. the crash loop is ENDED by name ─────────────────────────────────────────────────────
def test_d02_a_crash_looping_row_is_ended_by_name_not_re_run_forever(queue_db):
    task_id = queue_db.create_task(title="crash loop")
    for worker in ("dead-1", "dead-2"):
        queue_db.claim(task_id, worker)
        queue_db.release(task_id, worker)
    queue_db.claim(task_id, "dead-3")  # attempts now = settings.task_max_attempts
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)

    queue_db.run(maintenance._requeue_stale_claims_in_session())

    row = _task_row(queue_db, task_id)
    assert row["attempts"] == settings.task_max_attempts
    assert row["status"] == "failed"
    assert "lease expired" in (row["error_message"] or "")
    assert row["claimed_by"] is None
    assert queue_db.count("agent_runs") == 0


# ── D3. the reaper is idempotent ────────────────────────────────────────────────────────────
def test_d03_a_second_reap_tick_recovers_nothing(queue_db):
    task_id = queue_db.create_task(title="reaped twice")
    queue_db.claim(task_id, "dead-1")
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)

    first = queue_db.run(maintenance._requeue_stale_claims_in_session())
    second = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert first["requeued"] == 1
    assert second["requeued"] == 0 and second["scanned"] == 0
    assert _task_row(queue_db, task_id)["attempts"] == 1, (
        "a tick that recovers nothing must not touch the ladder"
    )


# ── D4. the killed worker's RUN row ─────────────────────────────────────────────────────────
def test_d04_a_running_run_whose_worker_died_is_closed_with_the_named_interruption(queue_db):
    run_id = _create_running_run(
        queue_db,
        started_ago_s=AGENT_RUN_STALE_AFTER_SECONDS + 60,
        usage={"in_flight": {"provider_calls": 3, "usage": {"input_tokens": 120}}},
    )

    closed = queue_db.run(_close_stale_runs_in_session())

    assert closed["closed"] == [run_id], closed
    rows = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    row = rows[0]
    assert row["status"] == "cancelled"
    assert "E_RUN_STALE" in (row["raw_log"] or "")
    usage = _values(row)
    assert usage["interrupted"]["reason_code"] == "E_RUN_STALE"
    assert usage["interrupted"]["cost_scope"] == "unmeasured", (
        "nothing was ever measured for this row; the cost stays NULL, never 0"
    )
    assert usage["interrupted"]["provider_calls_persisted"] == 3
    assert usage["in_flight"]["provider_calls"] == 3, "the live snapshot survives the close"
    assert row["cost_usd"] in (None, "None", 0) or float(row["cost_usd"]) == 0.0
    actions = [
        r["action"] for r in queue_db.query("SELECT action FROM activity_log ORDER BY id")
    ]
    assert actions.count(maintenance.STALE_RUNS_CLOSED_ACTION) == 1


# ── D5. a DB failure at the SETTLE ─────────────────────────────────────────────────────────
def test_d05_a_db_failure_at_the_settle_is_named_and_the_row_is_recovered(queue_db, install_run_spy, monkeypatch):
    from app.services import task_service

    spy = install_run_spy()
    task_id = queue_db.create_task(title="db dies at settle")

    async def _broken(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise PolsiaError(E_DB, "planted: the database went away between commit and settle")

    monkeypatch.setattr(task_service, "finish_agent_run", _broken)

    from celery_app.tasks.agent_tasks import _execute_agent_task

    with pytest.raises(PolsiaError) as caught:
        queue_db.run(_execute_agent_task(task_id))
    assert caught.value.code == E_DB, "the failure is named, never swallowed into a success"

    row = _task_row(queue_db, task_id)
    assert len(spy.calls) == 1, "the run really happened; only the settle failed"
    assert row["status"] == "in_progress", "the claim is still held by the (dead) worker"

    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)
    recovered = queue_db.run(maintenance._requeue_stale_claims_in_session())
    assert recovered["requeued"] == 1
    assert _task_row(queue_db, task_id)["status"] == "pending", "no wedge: the lease sweep recovers it"


# ── D6. the late settle on a reaped run (wave-7 W7-LD-03 contract) ─────────────────────────
def test_d06_a_late_settle_annotates_the_reaped_close_instead_of_silently_rewriting_it(queue_db):
    run_id = _create_running_run(
        queue_db,
        started_ago_s=AGENT_RUN_STALE_AFTER_SECONDS + 60,
        usage={"in_flight": {"provider_calls": 3}},
    )
    queue_db.run(_close_stale_runs_in_session())
    reaped = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    reaper_sentence = reaped["raw_log"]
    assert "E_RUN_STALE" in reaper_sentence

    _settle_late(
        queue_db, run_id, status="completed", output={"summary": "the run really finished"},
        duration_secs=12.5, cost_usd=Decimal("0.0012"),
    )
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]

    assert row["status"] == "completed", "the run's real outcome is the ledger's truth"
    assert reaper_sentence in (row["raw_log"] or ""), (
        "the reaper's sentence is preserved, not replaced — two writers, two facts"
    )
    usage = _values(row)
    assert usage["interrupted"]["reason_code"] == "E_RUN_STALE"
    assert usage["interrupted"]["superseded_by_status"] == "completed"
    assert usage["interrupted"]["superseded_at"]
    assert usage["in_flight"]["provider_calls"] == 3, "the live snapshot survives the late settle"
    assert usage["settlement_superseded"] == {
        "closed_status": "cancelled",
        "settled_status": "completed",
        "at": usage["settlement_superseded"]["at"],
    }


# ── D7. W8-LD-04: a late FAILED settle must keep the reaper's sentence ────────────────────
def test_d07_a_late_failed_settle_keeps_the_reapers_sentence_on_raw_log(queue_db):
    """RED on the wave-7 bytes: `error_message` REPLACED `raw_log`, deleting the reaper's
    `E_RUN_STALE` sentence while `usage_json.interrupted` still describes the close.  A reader of
    `raw_log` alone would see a plain agent failure and no trace of the earlier close."""
    run_id = _create_running_run(
        queue_db,
        started_ago_s=AGENT_RUN_STALE_AFTER_SECONDS + 60,
        usage={"in_flight": {"provider_calls": 1}},
    )
    queue_db.run(_close_stale_runs_in_session())
    reaped = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    reaper_sentence = reaped["raw_log"]
    assert "E_RUN_STALE" in reaper_sentence

    _settle_late(
        queue_db, run_id, status="failed",
        error_message="PolsiaError: E_CLI_TIMEOUT: the provider call exceeded 300s",
    )
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    raw_log = row["raw_log"] or ""

    assert row["status"] == "failed"
    assert reaper_sentence.strip() in raw_log, (
        "the earlier close is a FACT: a late failure must append to it, not delete it.  "
        f"raw_log={raw_log!r}"
    )
    assert "E_CLI_TIMEOUT" in raw_log, "the settle's own reason is recorded too"
    assert "settlement superseded" in raw_log, "and the supersession note names the order"
    usage = _values(row)
    assert usage["settlement_superseded"]["closed_status"] == "cancelled"
    assert usage["settlement_superseded"]["settled_status"] == "failed"


# ── D8. the same class, one field over: a SUPPLIED usage bag ──────────────────────────────
def test_d08_a_late_settle_that_supplies_usage_cannot_erase_the_interruption_record(queue_db):
    """RED on the wave-7 bytes: `finish_agent_run(usage_json=...)` ASSIGNED the bag, so a late
    settle carrying its own usage deleted `interrupted`/`in_flight` — and the annotation that is
    supposed to mark the close then had nothing left to annotate."""
    run_id = _create_running_run(
        queue_db,
        started_ago_s=AGENT_RUN_STALE_AFTER_SECONDS + 60,
        usage={"in_flight": {"provider_calls": 2}, "cost_scope": "unmeasured"},
    )
    queue_db.run(_close_stale_runs_in_session())

    _settle_late(
        queue_db, run_id, status="completed", duration_secs=4.0, cost_usd=Decimal("0.0003"),
        usage_json={"cost_usd": 0.0003, "usage": {"input_tokens": 10, "output_tokens": 2}},
    )
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    usage = _values(row)

    assert usage["cost_usd"] == 0.0003, "the settle's supplied keys still WIN"
    assert usage["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert usage["interrupted"]["reason_code"] == "E_RUN_STALE", (
        "keys the settle did not supply must survive — a supplied bag is a merge, like every "
        "other accounting write in this repo"
    )
    assert usage["interrupted"]["superseded_by_status"] == "completed"
    assert usage["in_flight"]["provider_calls"] == 2
    assert usage["settlement_superseded"]["closed_status"] == "cancelled"
