"""WAVE 9 / taskmaster — F-W8-TM-3: the stale-claim sweep's counter must not lie.

The defect, measured live in wave 8 (`taskmaster/raw/w8/l4-result.txt`):

    task-lease sweep: {"requeued": 2, "scanned": 2}
    task 335: status failed, "lease expired after 3 attempts (worker died?)"
    task 336: status failed, "lease expired after 3 attempts (worker died?)"

Two rows were ENDED at the attempt bound and NONE was requeued, yet the sweep's only number said
`requeued: 2`.  Every receipt that quoted "requeued N" (waves 5, 7, 8) therefore overstated
recovery, and an operator reading one number could not tell a recovered claim from an ended one.

`requeue_stale_claims` (task_service) returns one count by contract, and its RETURNING split is not
exposed — so `celery_app.tasks.maintenance` MEASURES the split itself: it snapshots the ids the
sweep's own rule selects, runs the shipped sweep, and reads the status the DB now holds for exactly
those ids in the same transaction.  The result names three facts:

    recovered -> every claim moved out of `in_progress` (the sweep's own count)
    requeued  -> the ones back on the queue (genuinely recoverable work)
    ended     -> the ones the attempt ladder terminated

and refuses to report a split it could not measure (`outcomes: "unavailable"`, numbers None).

A check that has never been seen failing is not a check: the RED leg for this file is in
`raw/w9/RED-legs-w9.txt` (the old one-number return planted back into the file by byte replace).
"""
from __future__ import annotations

import logging

import pytest

from app.config import settings
from celery_app.tasks import maintenance
from celery_app.tasks.broker_recovery import Delivery


def _spend_attempts(queue_db, task_id: int) -> None:
    """Claim/release until the row has spent EVERY attempt and is claimed by the last worker."""
    for index in range(int(settings.task_max_attempts)):
        worker = f"dead-worker-{index}"
        queue_db.claim(task_id, worker)
        if index < int(settings.task_max_attempts) - 1:
            queue_db.release(task_id, worker)


def _dead_claim(queue_db, title: str) -> int:
    task_id = queue_db.create_task(title=title, agent_type="social_media")
    queue_db.claim(task_id, "dead-worker-1")
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)
    return task_id


def _exhausted_claim(queue_db, title: str) -> int:
    task_id = queue_db.create_task(title=title, agent_type="social_media")
    _spend_attempts(queue_db, task_id)
    queue_db.backdate_heartbeat(task_id, settings.task_claim_lease_seconds + 60)
    return task_id


# ── A. three facts, named separately (the wave-8 defect: one number wearing three names) ─────
def test_a_the_sweep_names_requeued_and_ended_separately(queue_db):
    requeued_row = _dead_claim(queue_db, "recoverable claim")
    ended_row = _exhausted_claim(queue_db, "crash-looping claim")

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert result["status"] == "completed", result
    assert result["scanned"] == 2, result
    assert result["recovered"] == 2, (
        "the sweep changed TWO rows; `recovered` is the sweep's own total: " f"{result}"
    )
    assert result["requeued"] == 1, (
        f"exactly ONE row went back on the queue; a counter that says 2 is the F-W8-TM-3 defect: "
        f"{result}"
    )
    assert result["ended"] == 1, f"exactly ONE row was ended at the attempt bound: {result}"
    assert result["outcomes"] == "measured", result
    assert result["requeued_ids"] == [requeued_row], result
    assert result["ended_ids"] == [ended_row], result
    assert result["left_ids"] == [], result
    assert queue_db.row(requeued_row)["status"] == "pending"
    assert queue_db.row(ended_row)["status"] == "failed"


# ── B. the exact live L4 shape: a sweep that ONLY ends rows must report requeued: 0 ───────────
def test_b_a_sweep_that_only_ends_rows_reports_zero_requeued(queue_db):
    first = _exhausted_claim(queue_db, "crash loop 1")
    second = _exhausted_claim(queue_db, "crash loop 2")

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert result["recovered"] == 2, result
    assert result["requeued"] == 0, (
        "the wave-8 bytes answered `requeued: 2` here while BOTH rows went terminal `failed`; "
        f"nothing is requeued: {result}"
    )
    assert result["ended"] == 2, result
    assert sorted(result["ended_ids"]) == sorted([first, second]), result
    assert result["requeued_ids"] == [], result
    for task_id in (first, second):
        row = queue_db.row(task_id)
        assert row["status"] == "failed"
        assert "lease expired" in (row["error_message"] or "")


# ── C. an idempotent tick reports zeros, not a stale number ──────────────────────────────────
def test_c_a_second_tick_reports_nothing_and_reports_it_honestly(queue_db):
    _dead_claim(queue_db, "recovered once")

    first = queue_db.run(maintenance._requeue_stale_claims_in_session())
    second = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert first["recovered"] == 1 and first["requeued"] == 1, first
    assert second["recovered"] == 0 and second["requeued"] == 0 and second["ended"] == 0, second
    assert second["outcomes"] == "measured", second
    assert second["scanned"] == 0, second


# ── D. an unmeasurable split is named, never guessed ─────────────────────────────────────────
def test_d_a_split_that_cannot_be_measured_is_reported_as_unavailable_not_a_number(
    queue_db, monkeypatch
):
    _dead_claim(queue_db, "row the sweep disagrees about")
    real = maintenance.requeue_stale_claims

    async def _wrong_count(db, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        changed = await real(db, *args, **kwargs)
        return changed + 1  # a service that reports a number its writes do not account for

    monkeypatch.setattr(maintenance, "requeue_stale_claims", _wrong_count)

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert result["recovered"] == 2, result
    assert result["requeued"] is None and result["ended"] is None, (
        "a split that does not add up must NOT be reported as a number: " f"{result}"
    )
    assert result["outcomes"] == "unavailable", result
    assert result["reason"] and "not reported as a number" in result["reason"], result


# ── E. the delivery-recovery pass reports the SAME split (it runs the same sweep) ────────────
class _EmptyBroker:
    def snapshot(self) -> list[Delivery]:
        return []

    def restore(self, tag: str) -> None:  # pragma: no cover - no delivery is restored
        raise AssertionError("no delivery may be restored by an empty snapshot")

    def drop(self, tag: str) -> None:  # pragma: no cover - no delivery is dropped
        raise AssertionError("no delivery may be dropped by an empty snapshot")

    def close(self) -> None:  # pragma: no cover - the caller owns this broker
        raise AssertionError("an injected broker must not be closed by the pass")


def test_e_delivery_recovery_names_the_same_split(queue_db):
    ended_row = _exhausted_claim(queue_db, "ended before the delivery pass")

    result = queue_db.run(maintenance._recover_orphaned_deliveries(broker=_EmptyBroker()))

    assert result["reaped_recovered"] == 1, result
    assert result["reaped_requeued"] == 0, (
        "the delivery pass inherited the same misleading name; it now reports the measured split: "
        f"{result}"
    )
    assert result["reaped_ended"] == 1, result
    assert result["reaped_outcomes"] == "measured", result
    assert queue_db.row(ended_row)["status"] == "failed"


# ── F. observability truth: the log line says which outcome, not one ambiguous number ────────
def test_f_the_log_line_names_both_outcomes(queue_db, caplog):
    _dead_claim(queue_db, "one recovered")
    _exhausted_claim(queue_db, "one ended")

    with caplog.at_level(logging.INFO, logger="celery_app.tasks.maintenance"):
        queue_db.run(maintenance._requeue_stale_claims_in_session())

    sweeps = [r for r in caplog.records if "task-lease sweep" in r.getMessage()]
    assert sweeps, "the sweep must say what it did"
    text = sweeps[-1].getMessage()
    assert "1 back to pending" in text, text
    assert "1 ended" in text, text
    assert "outcomes=measured" in text, text


# ── G. the wave-8 defect, pinned as arithmetic: one total cannot answer two questions ────────
def test_g_the_three_numbers_are_not_the_same_number(queue_db):
    """Two rows ended: `recovered` is 2 by definition, `requeued` is 0 by measurement."""
    _exhausted_claim(queue_db, "ended 1")
    _exhausted_claim(queue_db, "ended 2")

    result = queue_db.run(maintenance._requeue_stale_claims_in_session())

    assert (result["recovered"], result["requeued"], result["ended"]) == (2, 0, 2), result
    if result["outcomes"] == "measured":
        assert result["recovered"] == result["requeued"] + result["ended"], (
            "the measured split must account for every row the sweep changed: " f"{result}"
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    raise SystemExit(pytest.main([__file__, "-q"]))
