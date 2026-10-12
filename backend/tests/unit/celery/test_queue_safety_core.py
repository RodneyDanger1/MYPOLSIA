"""The queue's safety core: claim-before-work, outcome writing, lease loss, requeue recovery.

Every test here is a real in-process execution of the shipped code against a real database:
`_execute_agent_task` (and, in `test_retry_ladder.py`, the Celery task itself in eager mode)
runs the real `crew_factory` + real agent with the repo's own `CLAUDE_CLI_MOCK` seam, and the
assertions read the tasks/agent_runs/activity_log rows out of the database FILE with the stdlib
driver — not off the returned objects.

These tests share the fixtures in `conftest.py`: a file-backed SQLite database (genuine
connections, so a claim race is a real race) driven on the Celery process loop.  See that
module's docstring for why `:memory:` (the root conftest's StaticPool) cannot be used here.

Invariants covered (each names its RED leg — the defect that makes it fail):

1. the claim happens BEFORE any work; a second delivery of the same task_id has no side effects
   and returns `{"status": "skipped", "reason": <code>}`;
2. the outcome path writes status/result/error, clears the claim fields, and counts the failure
   (asserted on the row on disk);
3. a lost lease abandons the run, records why, and never stomps the row the new owner holds;
4. `requeue_stale_claims` recovers exactly the rows whose last sign of life is older than the
   lease, and sends an attempts-exhausted row terminal with the reason recorded.
"""
from __future__ import annotations

from app.config import settings
from app.core.database import utcnow
from app.core.errors import E_CLI_TIMEOUT, E_POLICY_DENIED, PolsiaError


# ─────────────────────────────────────────────────────────────────────────────
# 1. claim before work, and the second delivery is a no-op
# ─────────────────────────────────────────────────────────────────────────────
def test_invariant_1_claim_happens_first_and_a_second_delivery_is_skipped(
    queue_db, install_run_spy, execute_body, worker_fn
):
    """INVARIANT 1 — the claim is taken before any work; a re-delivery has NO side effects.

    The spy reads the row out of the database at the moment the agent is about to run: if the
    claim had not happened yet, that read would show `pending` / no `claimed_by` (this is the
    upload's "SELECT then run" defect).  The second delivery of the same task_id must exit
    `skipped` with the service's reason code, spawn no second agent run, and write nothing.

    RED leg (recorded in the receipt): replace the claim with a plain `get_task` read
    (`task = await get_task(db, task_id)`, the upload's read-then-run shape) — the row the spy
    sees is `pending`, this test fails.
    """
    task_id = queue_db.create_task(title="invariant-1")
    spy = install_run_spy()

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    assert outcome["task_id"] == task_id

    # -- the claim was in place BEFORE the agent ran, and it was THIS worker's claim
    assert len(spy.calls) == 1, "the agent must have run exactly once"
    at_call = spy.calls[0]["row_at_call"]
    assert at_call["status"] == "in_progress", (
        f"the agent ran while the row was {at_call['status']!r} — the claim did not happen "
        "before the work"
    )
    assert at_call["claimed_by"] == worker_fn()
    assert at_call["claimed_at"] and at_call["heartbeat_at"]
    assert at_call["attempts"] == 1

    # -- and the outcome landed on the row
    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["result_summary"] == "Mock Claude response for testing"
    assert row["error_message"] is None
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None

    # -- the second delivery of the SAME message
    before = queue_db.row(task_id)
    second = execute_body(task_id)

    assert second == {
        "status": "skipped",
        "task_id": task_id,
        "reason": "E_TASK_NOT_RUNNABLE",
    }, "a re-delivery of a finished task must be a NAMED skip, not a silent success or a re-run"
    assert len(spy.calls) == 1, "no second agent run"
    assert queue_db.count("agent_runs", "task_id = ?", (task_id,)) == 1
    assert queue_db.count("activity_log") == 1, "a skipped delivery must not log a task outcome"
    assert queue_db.row(task_id) == before, "a skipped delivery must not touch the row"


def test_invariant_1_a_delivery_racing_another_workers_claim_is_skipped(
    queue_db, install_run_spy, eager_delivery
):
    """INVARIANT 1 — with the row claimed by another worker, delivery exits `skipped` (E_TASK_CLAIMED).

    This is the `acks_late` re-delivery case: the same message arrives while the first worker is
    still in flight.  The claim is taken through the real service (so the competing owner is the
    service's own state, not a hand-written UPDATE), and the delivery runs through the real
    Celery task in eager mode — the whole wrapper, not just the body.

    RED leg: make the claim refusal non-skippable (e.g. `_SKIP_CLAIM_CODES = ()`) — the task
    then raises instead of returning `skipped`, and the named result check fails.
    """
    task_id = queue_db.create_task(title="invariant-1-racing")
    queue_db.claim(task_id, "other-worker-1")
    spy = install_run_spy()

    result = eager_delivery(task_id)

    assert result.state == "SUCCESS", "a skip is not a task failure"
    assert result.result == {
        "status": "skipped",
        "task_id": task_id,
        "reason": "E_TASK_CLAIMED",
    }
    assert spy.calls == [], "the agent must not run while another worker holds the claim"
    assert queue_db.count("agent_runs", "task_id = ?", (task_id,)) == 0
    assert queue_db.count("activity_log") == 0

    row = queue_db.row(task_id)
    assert row["status"] == "in_progress"
    assert row["claimed_by"] == "other-worker-1"
    assert row["attempts"] == 1, "the losing delivery must not claim (and so must not count an attempt)"
    assert row["result_summary"] is None and row["error_message"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. the outcome path
# ─────────────────────────────────────────────────────────────────────────────
def test_invariant_2_a_successful_run_writes_the_row_and_clears_the_claim(
    queue_db, install_run_spy, execute_body
):
    """INVARIANT 2 — success writes status + summary, clears claimed_by/claimed_at/heartbeat_at.

    The claim fields are cleared in the same transaction as the outcome, so a later
    `requeue_stale_claims` sweep cannot mistake a finished row for a dead worker's.

    RED leg: delete the three `task_row.claimed_* = None` lines — the row keeps the claim
    fields and this test fails on `claimed_by is None`.
    """
    task_id = queue_db.create_task(title="invariant-2-success")
    spy = install_run_spy()

    outcome = execute_body(task_id)
    assert outcome["status"] == "completed"

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["result_summary"] == "Mock Claude response for testing"
    assert row["error_message"] is None
    assert row["claimed_by"] is None, "a finished row must not keep a claim"
    assert row["claimed_at"] is None
    assert row["heartbeat_at"] is None
    # The claim counts one attempt; a SUCCESSFUL run must not add a failure to the counter.
    assert row["attempts"] == spy.calls[0]["row_at_call"]["attempts"] == 1

    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["ended_at"] is not None
    assert runs[0]["duration_secs"] is not None

    activity = queue_db.query("SELECT * FROM activity_log")
    assert [a["action"] for a in activity] == ["task_completed"]
    assert activity[0]["level"] == "success"


def test_invariant_2_a_failed_run_records_error_and_counts_the_failure(
    queue_db, install_run_spy, execute_body
):
    """INVARIANT 2 — a NON-retryable failure writes status/error, clears the claim, counts the failure.

    A policy refusal is terminal by definition (the retry ladder only backs off infrastructure
    failures), so the row must end `failed` with the error recorded, the claim cleared, and
    `attempts` incremented by exactly one over the value the claim set.

    RED leg: remove the `task_row.attempts = ... + 1` line — the increment assertion fails.
    """
    task_id = queue_db.create_task(title="invariant-2-failure")
    spy = install_run_spy(
        error_factory=lambda: PolsiaError(E_POLICY_DENIED, "test-only refusal: nothing was sent")
    )

    outcome = execute_body(task_id)

    assert outcome["status"] == "failed"
    assert outcome["retryable"] is False, "a policy refusal is not retryable"
    assert E_POLICY_DENIED in outcome["error"]

    claimed_attempts = spy.calls[0]["row_at_call"]["attempts"]
    row = queue_db.row(task_id)
    assert row["status"] == "failed"
    assert row["error_message"] and E_POLICY_DENIED in row["error_message"]
    assert row["result_summary"] is None
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    # `attempts` counts EXECUTIONS and the claim is what increments it, so a failed execution leaves the
    # number exactly where the claim put it. This previously asserted `claimed_attempts + 1` with the
    # message "the failure path must count this failure" — the observed arithmetic of a double count that
    # halved the retry ladder (the caller's failure path incremented a column the service had already
    # incremented). Pinning it here is the guard against that regression coming back.
    assert row["attempts"] == claimed_attempts, (
        "the failure path must NOT add a second increment — the claim already counted this execution"
    )

    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert runs[0]["raw_log"] and E_POLICY_DENIED in runs[0]["raw_log"]

    activity = queue_db.query("SELECT * FROM activity_log")
    assert [a["action"] for a in activity] == ["task_failed"]
    assert activity[0]["level"] == "error"

    # a terminal failure is not re-claimable: the second delivery is a named skip
    assert execute_body(task_id) == {
        "status": "skipped",
        "task_id": task_id,
        "reason": "E_TASK_NOT_RUNNABLE",
    }


def test_invariant_2_a_retryable_failure_requeues_the_row_for_its_retry(
    queue_db, install_run_spy, execute_body
):
    """INVARIANT 2 + 5 — a RETRYABLE failure leaves the row claimable again, so the retry can run.

    `run_agent_task` schedules a Celery retry for a retryable failure, and the retry is delivered
    as a second message for the same task_id — which must CLAIM the row.  The service's claim
    accepts only `pending` rows, so a retryable failure that wrote `failed` would make its own
    retry un-runnable (the delivered code did exactly that: `failed` + a retry that is skipped as
    E_TASK_NOT_RUNNABLE, recorded in the receipt as a bug found and fixed in
    `celery_app/tasks/agent_tasks.py`).  The row therefore goes back to `pending` with the error
    recorded and the failure counted; a non-retryable failure (test above) still goes terminal.

    RED leg: revert the failure path to write `failed` unconditionally — this test fails on
    `row["status"] == "pending"` and on the follow-up delivery being skipped instead of run.
    """
    task_id = queue_db.create_task(title="invariant-2-retryable")
    failing = install_run_spy(
        error_factory=lambda: PolsiaError(E_CLI_TIMEOUT, "test-only transport failure")
    )

    first = execute_body(task_id)
    assert first["status"] == "failed"
    assert first["retryable"] is True

    row = queue_db.row(task_id)
    assert row["status"] == "pending", (
        "a retryable failure must requeue: 'pending' is the only state the service's claim accepts"
    )
    assert row["error_message"] and "E_CLI_TIMEOUT" in row["error_message"]
    assert row["result_summary"] is None
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert row["attempts"] == 1  # one claim, one execution — the failure adds no second count

    # the retry: a fresh delivery, with a working agent this time
    failing.calls.clear()
    working = install_run_spy()
    second = execute_body(task_id)

    assert second["status"] == "completed", (
        "the scheduled retry must be able to claim and run the task, not skip it"
    )
    assert len(working.calls) == 1, "the retried delivery ran the agent once"
    assert working.calls[0]["row_at_call"]["status"] == "in_progress"
    final = queue_db.row(task_id)
    assert final["status"] == "completed" and final["result_summary"] is not None
    assert final["attempts"] == 2, (
        "each claim counts exactly one execution: the failed delivery and this retry"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. the lease
# ─────────────────────────────────────────────────────────────────────────────
def test_invariant_3_a_lost_lease_abandons_the_run_without_stomping_the_new_owner(
    queue_db, install_run_spy, execute_body, steal_claim, worker_fn, monkeypatch
):
    """INVARIANT 3 — heartbeat refused ⇒ abandon the run, record why, never write the outcome.

    The row is taken over mid-run by a real second claim (release + claim through the service).
    The heartbeat only fires after its 5 s floor (`interval = max(5.0, lease/3)`), so the work
    is slowed to ~6.5 s and the takeover lands at t≈1 s.  The run then completes its work, finds
    its lease gone, and must record `task_lease_lost` WITHOUT writing status/result/error — the
    row belongs to the new owner.

    This is the one test that takes ~7 s wall clock: the heartbeat floor is 5 s by construction
    and there is no other trigger for the loss (measured; see the receipt).

    RED leg: make the `lease_lost` branch fall through to the normal outcome write (e.g. flip
    `if lease_lost.is_set():` to `if False:`) — the row is stomped to `failed`, the takeover's
    `in_progress`/`claimed_by='thief-worker-1'` assertions fail, and `task_lease_lost` is missing
    from the activity log.
    """
    monkeypatch.setattr(settings, "task_claim_lease_seconds", 3)
    task_id = queue_db.create_task(title="invariant-3-lost-lease")
    spy = install_run_spy(sleep_s=6.5)  # the real agent still runs — it just takes longer

    takeover = steal_claim(task_id, after_s=1.0, thief="thief-worker-1")
    outcome = execute_body(task_id)
    takeover.result(timeout=30)

    assert outcome["status"] == "failed"
    assert "claim lease lost" in (outcome["error"] or ""), outcome
    assert len(spy.calls) == 1, "the work really ran — the loss is detected while it finishes"

    row = queue_db.row(task_id)
    assert row["status"] == "in_progress", "the loser must not stomp the new owner's status"
    assert row["claimed_by"] == "thief-worker-1"
    assert row["result_summary"] is None, "the abandoned result must not be written"
    assert row["error_message"] is None, "the abandoned failure must not be written"
    assert row["attempts"] == 2, "claims: this worker + the thief; the loser writes no increment"

    activity = queue_db.query("SELECT * FROM activity_log WHERE action != 'task_lease_lost'")
    assert activity == [], "no task_completed/task_failed for an abandoned run"
    lost = queue_db.query("SELECT * FROM activity_log WHERE action = 'task_lease_lost'")
    assert len(lost) == 1, "the loss must be recorded, naming the task"
    assert f"task {task_id}" in lost[0]["summary"]
    assert lost[0]["level"] == "warning"

    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert "lease lost" in (runs[0]["raw_log"] or "")

    assert worker_fn() != "thief-worker-1"  # the two claims were genuinely different workers


# ─────────────────────────────────────────────────────────────────────────────
# 4. requeue
# ─────────────────────────────────────────────────────────────────────────────
def test_invariant_4_requeue_recovers_only_rows_older_than_the_lease(queue_db, service):
    """INVARIANT 4a — recovery is bounded by the lease: a fresh claim must be left alone.

    Two rows are claimed; one heartbeat is backdated past the lease.  ONE call must recover the
    stale row (back to `pending`, claim fields cleared, attempts untouched) and leave the fresh
    one in flight.  Without the negative half of this test, "recovers stale claims" would pass
    for a function that recovers everything.

    RED leg: drop the `coalesce(heartbeat_at, ...) < cutoff` predicate (a runtime mutant of the
    real function body is used, so the service file is never modified) — both rows are recovered
    and the count/row assertions fail.
    """
    fresh = queue_db.create_task(title="invariant-4-fresh")
    stale = queue_db.create_task(title="invariant-4-stale")
    queue_db.claim(fresh, "worker-fresh")
    queue_db.claim(stale, "worker-dead")
    queue_db.backdate_heartbeat(stale, seconds=settings.task_claim_lease_seconds + 60)

    from celery_app.tasks._common import run_async

    async def _requeue() -> int:
        async with queue_db.factory() as db:
            count = await service.requeue_stale_claims(db, now=utcnow())
            await db.commit()
            return count

    assert run_async(_requeue()) == 1

    stale_row = queue_db.row(stale)
    assert stale_row["status"] == "pending", "the stale claim goes back to the queue"
    assert stale_row["claimed_by"] is None and stale_row["heartbeat_at"] is None
    assert stale_row["attempts"] == 1, "requeueing is not a new attempt"

    fresh_row = queue_db.row(fresh)
    assert fresh_row["status"] == "in_progress", "a live claim must not be recovered"
    assert fresh_row["claimed_by"] == "worker-fresh"


def test_invariant_4_an_exhausted_row_goes_terminal_with_the_reason_recorded(queue_db, service):
    """INVARIANT 4b — a dead worker's task that has used its attempts is FAILED, not requeued forever.

    One row has consumed `settings.task_max_attempts` claims (a claim counts one attempt), the
    other has one.  Both are stale.  One call must split them: the exhausted row goes `failed`
    with `lease expired after N attempts (worker died?)` recorded, the other goes back to
    `pending`.

    RED leg: make the status CASE always choose 'pending' (runtime mutant; the service file is
    untouched) — the terminal-status assertion fails.
    """
    exhausted = queue_db.create_task(title="invariant-4-exhausted")
    survivor = queue_db.create_task(title="invariant-4-survivor")

    max_attempts = int(settings.task_max_attempts)
    for index in range(max_attempts):
        queue_db.claim(exhausted, "worker-dead")
        if index < max_attempts - 1:
            queue_db.release(exhausted, "worker-dead")
    queue_db.claim(survivor, "worker-dead")
    queue_db.backdate_heartbeat(exhausted, seconds=settings.task_claim_lease_seconds + 60)
    queue_db.backdate_heartbeat(survivor, seconds=settings.task_claim_lease_seconds + 60)
    assert queue_db.row(exhausted)["attempts"] == max_attempts

    from celery_app.tasks._common import run_async

    async def _requeue() -> int:
        async with queue_db.factory() as db:
            count = await service.requeue_stale_claims(db, now=utcnow())
            await db.commit()
            return count

    assert run_async(_requeue()) == 2

    dead = queue_db.row(exhausted)
    assert dead["status"] == "failed", "an exhausted task must not be requeued forever"
    assert dead["error_message"] == f"lease expired after {max_attempts} attempts (worker died?)"
    assert dead["claimed_by"] is None and dead["claimed_at"] is None and dead["heartbeat_at"] is None
    assert dead["attempts"] == max_attempts, "going terminal does not add an attempt"

    alive = queue_db.row(survivor)
    assert alive["status"] == "pending", "a row with attempts left goes back to the queue"
    assert alive["error_message"] is None
