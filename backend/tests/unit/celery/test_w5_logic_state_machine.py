"""W5 / logic-debugger â€” the queue state machine under INJECTED failure (not the happy path).

One row per case of the wave-5 failure matrix.  Every case is driven through the PRODUCTION
bodies (`_execute_agent_task`, `task_service.claim_task`/`transition_task_status`/
`release_ready_dependents`), never through a test helper that re-implements the rule; the run
seam is the `install_run_spy` double the repo already uses (a real double per AGENTS.md Â§7 â€” the
model call is a third-party API, the logic under test is ours).

The matrix (each test names its row):

   1. double-claim                 a second delivery of a LIVE claim runs nothing
   2. stale-claim takeover         an EXPIRED claim is taken over by the redelivery; the attempt
                                   ladder ends a crash-looping row by name instead of re-running it
   3. retry-after-partial          a retryable failure requeues the row, keeps its bag, and the
                                   retry's completion still retires the earlier attempt's error
   4. DAG parent rejection         a service-leg `rejected` parent fails its dependents BY NAME
   5. dependency release           a DIAMOND waits for its WHOLE dependency set (a half-satisfied
                                   edge releases nothing) and receives every upstream result
   6. replay of a completed row    a redelivered message for a terminal row writes nothing
   7. terminal-state guard         an owner CANCEL during the run is not stomped by the outcome
                                   (W5-LD-03; RED on the pre-fix tree)
   8. vocabulary coherence         a stale `partial` verdict does not outlive the attempt
                                   (W5-LD-04; RED on the pre-fix tree)
   9. approval deny / re-park      a denied parked row is terminal, its parked set and token are
                                   spent with it, and a later delivery cannot reopen it
  10. junk metadata                a JSON ARRAY in `tasks.metadata` (the live `[null]` shape) no
                                   longer kills the completion (A-W4-03; RED on the pre-fix tree)
"""
from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.core.errors import E_CLI_TIMEOUT, MeridianError
from celery_app.tasks import agent_tasks
from celery_app.tasks._common import get_process_loop

# â”€â”€ helpers (the FILE is the evidence: no ORM, no identity map) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _create(queue_db, *, title: str, metadata: dict | None = None,
            agent_type: str = "finance") -> int:
    return queue_db.create_task(title=title, agent_type=agent_type, metadata=metadata)


def _row(queue_db, task_id: int) -> dict:
    return queue_db.row(task_id)


def _metadata(queue_db, task_id: int) -> dict:
    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


def _record_deliveries(monkeypatch) -> list[int]:
    """Record â€” never perform â€” the broker hand-off.

    BOTH shapes the product uses are recorded: `run_agent_task.delay` (one id) and
    `enqueue_created` (the DAG hook's batched hand-off, which publishes a `group` for >1 id and so
    would otherwise reach the real broker).
    """
    delivered: list[int] = []

    async def _enqueue_created(task_ids, *, label=""):  # noqa: ANN001 - `_common`'s signature
        delivered.extend(int(task_id) for task_id in task_ids)

    monkeypatch.setattr(agent_tasks, "enqueue_created", _enqueue_created)
    monkeypatch.setattr(
        agent_tasks.run_agent_task, "delay", lambda task_id: delivered.append(int(task_id)),
        raising=False,
    )
    return delivered


def _run_on_loop(queue_db, coro_fn):
    """Drive one async write on the Celery process loop (the loop every worker write uses)."""

    async def _wrapper():
        async with queue_db.factory() as db:
            return await coro_fn(db)

    return queue_db.run(_wrapper())


# â”€â”€ 1. double-claim â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_1_a_second_delivery_of_a_live_claim_runs_nothing(queue_db, install_run_spy, execute_body):
    """Two messages for one row: the second finds a LIVE claim and exits with no side effects.

    `claim_task` is the atomic half (`UPDATE â€¦ WHERE id = (SELECT â€¦ LIMIT 1)`), so the invariant the
    worker relies on is not "the first worker wins the race" but "the loser cannot run": it never
    touches the row, never opens a ledger row and never calls the agent.
    """
    task_id = _create(queue_db, title="double claim")
    queue_db.claim(task_id, "worker-alpha")
    spy = install_run_spy(delegate=False, result={"summary": "must not run"})

    outcome = execute_body(task_id)

    assert outcome == {"status": "skipped", "task_id": task_id, "reason": "E_TASK_CLAIMED"}
    assert spy.calls == [], "the losing delivery ran the agent"
    row = _row(queue_db, task_id)
    assert row["status"] == "in_progress"
    assert row["claimed_by"] == "worker-alpha", "the loser rewrote the claim"
    assert row["attempts"] == 1, "the loser spent a retry-ladder slot on work it never ran"
    assert queue_db.count("agent_runs", "task_id = ?", (task_id,)) == 0
    assert queue_db.count("activity_log", "detail LIKE ?", (f'%"task_id": {task_id}%',)) == 0


# â”€â”€ 2. stale-claim takeover â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_2_an_expired_claim_is_taken_over_by_the_redelivery(queue_db, install_run_spy, execute_body):
    """A message that comes back after its worker died must be able to take the EXPIRED claim.

    Without the takeover the delivery is refused `E_TASK_CLAIMED`, is acked, and the row is
    stranded: the delivery is the only copy and the lease sweep is a Beat tick.
    """
    task_id = _create(queue_db, title="stale claim")
    queue_db.claim(task_id, "worker-dead")
    queue_db.backdate_heartbeat(task_id, seconds=settings.task_claim_lease_seconds + 60)
    spy = install_run_spy(delegate=False, result={"summary": "the redelivery did the work"})

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    assert len(spy.calls) == 1
    row = _row(queue_db, task_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2, "the takeover is an execution: the claim counts exactly one"
    recovered = queue_db.query(
        "SELECT * FROM activity_log WHERE action = 'task_claim_recovered'"
    )
    assert len(recovered) == 1, recovered
    assert "EXPIRED claim" in recovered[0]["summary"]
    assert recovered[0]["level"] == "warning"


def test_2b_a_stale_claim_that_spent_the_ladder_ends_by_name(
    queue_db, install_run_spy, execute_body, service
):
    """The crash-loop bound: no fourth execution â€” the row is ended with the reason recorded.

    This is the terminal-state guard on the TAKEOVER path: a re-delivery must not resurrect work
    the attempt ladder has already given up on, and the refusal the worker sees must name the row's
    real state (`failed`), not "claimed".
    """

    async def _spend_the_ladder(db):
        row = await service.get_task(db, task_id)
        row.attempts = settings.task_max_attempts
        await db.commit()

    task_id = _create(queue_db, title="crash loop")
    queue_db.claim(task_id, "worker-dead")
    _run_on_loop(queue_db, _spend_the_ladder)
    queue_db.backdate_heartbeat(task_id, seconds=settings.task_claim_lease_seconds + 60)
    spy = install_run_spy(delegate=False, result={"summary": "must not run"})

    outcome = execute_body(task_id)

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == "E_TASK_NOT_RUNNABLE", (
        "the refusal must describe the row the takeover just ended, not the claim it no longer holds"
    )
    assert spy.calls == []
    row = _row(queue_db, task_id)
    assert row["status"] == "failed"
    assert f"lease expired after {settings.task_max_attempts} attempts" in (row["error_message"] or "")
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert row["attempts"] == settings.task_max_attempts, "the ladder is not spent a second time"
    ended = queue_db.query(
        "SELECT * FROM activity_log WHERE action = 'task_claim_recovery_exhausted'"
    )
    assert len(ended) == 1 and ended[0]["level"] == "error"


# â”€â”€ 3. retry-after-partial â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_3_a_retryable_failure_requeues_the_row_and_the_retry_settles_it(
    queue_db, install_run_spy, execute_body
):
    """The partial attempt leaves a RE-RUNNABLE row: same bag, one ladder slot, then a clean retry.

    Three facts the shipped ladder has to keep, all measured here: the row goes back to `pending`
    (the only state `claim_task` accepts), the failure text stays on the row while `pending` (it is
    the retry's own evidence, and `pending` is not a completion), and the retry's COMPLETION retires
    it â€” so the finished row never serves the first attempt's failure.
    """
    task_id = _create(
        queue_db, title="retry after partial", metadata={"plan_day": "2026-10-28", "keep": 1}
    )
    install_run_spy(error_factory=lambda: MeridianError(E_CLI_TIMEOUT, "the transport died"))

    first = execute_body(task_id)

    assert first["status"] == "failed"
    assert first["retryable"] is True and first["requeued"] is True
    row = _row(queue_db, task_id)
    assert row["status"] == "pending", "a retryable failure must leave a claimable row"
    assert row["attempts"] == 1, "the claim counts the execution; the failure adds none"
    assert "E_CLI_TIMEOUT" in (row["error_message"] or "")
    assert row["result_summary"] is None, "a failed attempt produced no summary"
    assert row["claimed_by"] is None and row["heartbeat_at"] is None
    meta = _metadata(queue_db, task_id)
    assert meta["keep"] == 1, "the per-task bag must survive the requeue"
    assert meta["rearmed_at"], "the requeue owes a delivery and says so"
    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,))
    assert [(run["status"], run["attempt"]) for run in runs] == [("failed", 1)]

    install_run_spy(delegate=False, result={"summary": "the retry produced the brief"})
    second = execute_body(task_id)

    assert second["status"] == "completed", second
    row = _row(queue_db, task_id)
    assert row["status"] == "completed"
    assert row["attempts"] == 2
    assert row["error_message"] is None, (
        "the completed row still serves the first attempt's failure (the retirement is the rule)"
    )
    meta = _metadata(queue_db, task_id)
    assert meta["keep"] == 1
    assert "E_CLI_TIMEOUT" in meta["last_attempt_error"]["message"]
    assert meta["last_attempt_error"]["supplied_on_completion"] is False
    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,))
    assert [(run["status"], run["attempt"]) for run in runs] == [("failed", 1), ("completed", 2)]


# â”€â”€ 4. DAG parent REJECTION -> child cascade (the service leg) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_4_a_service_rejected_parent_fails_its_dependents_by_name(queue_db, service):
    """`PATCH /tasks/{id}/status {"status":"rejected"}` is a terminal writer too.

    The worker's budget/replay refusals are pinned (wave 3); the SERVICE leg for `rejected` had no
    test.  A dependent of a refused row can never run, so it is failed BY NAME â€” never left
    `pending` waiting on a row that will not move.
    """
    parent = _create(queue_db, title="parent the budget refused")
    child = _create(queue_db, title="child", metadata={"depends_on_ids": [parent]})

    async def _reject(db):
        await service.transition_task_status(
            db, parent, "rejected", error_message="E_BUDGET_EXCEEDED: the day cap is spent",
            actor="orchestrator",
        )
        await db.commit()

    _run_on_loop(queue_db, _reject)

    assert _row(queue_db, parent)["status"] == "rejected"
    child_row = _row(queue_db, child)
    assert child_row["status"] == "failed", "the dependent of a refused row must never stay pending"
    assert "dependency_blocked" in (child_row["error_message"] or "")
    assert f"#{parent}" in (child_row["error_message"] or "")
    assert "'rejected'" in (child_row["error_message"] or "")
    assert "day cap" in (child_row["error_message"] or ""), (
        "the blocked reason carries the upstream's own refusal, not just its id"
    )
    blocked = _metadata(queue_db, child)["dependency_blocked"]
    assert blocked["upstream_id"] == parent
    assert blocked["upstream_status"] == "rejected"
    assert queue_db.count("activity_log", "action = 'task_dependency_blocked'") == 1


# â”€â”€ 5. dependency release on completion: the DIAMOND â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_5_a_diamond_dependent_waits_for_its_whole_dependency_set(
    queue_db, install_run_spy, execute_body, monkeypatch
):
    """A -> (B, C) -> D: D is released by the LAST completion only, with BOTH upstream results.

    The half-satisfied edge is the case a one-parent plan never exercises: releasing D when B
    finishes would hand a worker a node whose second input does not exist yet.
    """
    delivered = _record_deliveries(monkeypatch)
    a = _create(queue_db, title="A")
    b = _create(queue_db, title="B", metadata={"depends_on_ids": [a]})
    c = _create(queue_db, title="C", metadata={"depends_on_ids": [a]})
    d = _create(queue_db, title="D", metadata={"depends_on_ids": [b, c]})

    install_run_spy(delegate=False, result={"summary": "A result"})
    outcome_a = execute_body(a)
    assert outcome_a["dag_dependents"] == {"released": [b, c], "dispatched": True}, outcome_a
    assert delivered == [b, c]

    install_run_spy(delegate=False, result={"summary": "B result"})
    outcome_b = execute_body(b)
    assert outcome_b["dag_dependents"] == {"released": [], "dispatched": False}, (
        "D was released while C was still pending: the edge is only satisfied by the WHOLE set"
    )
    assert "dag_released_at" not in _metadata(queue_db, d)
    assert _row(queue_db, d)["status"] == "pending"
    assert delivered == [b, c], "nothing was dispatched for the half-satisfied node"

    install_run_spy(delegate=False, result={"summary": "C result"})
    outcome_c = execute_body(c)
    assert outcome_c["dag_dependents"] == {"released": [d], "dispatched": True}, outcome_c
    assert delivered == [b, c, d]

    meta_d = _metadata(queue_db, d)
    assert meta_d["dag_released_by"] == c
    assert [item["task_id"] for item in meta_d["upstream_results"]] == [b, c], (
        "the node records BOTH upstream results, in the plan's own order"
    )
    assert [item["result_summary"] for item in meta_d["upstream_results"]] == [
        "B result",
        "C result",
    ]
    assert _row(queue_db, d)["status"] == "pending"


# â”€â”€ 6. replay of a completed row â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_6_a_redelivered_message_for_a_completed_row_writes_nothing(
    queue_db, install_run_spy, execute_body
):
    """At-least-once delivery means a terminal row WILL be redelivered; it must be inert.

    Nothing is claimed, no second run row is opened, no second `task_completed` row is written and
    the recorded result does not change â€” the message is ack-and-ignore, by name.
    """
    task_id = _create(queue_db, title="replay of a completed row")
    install_run_spy(delegate=False, result={"summary": "the first attempt's result"})
    first = execute_body(task_id)
    assert first["status"] == "completed"
    before = _row(queue_db, task_id)

    spy = install_run_spy(delegate=False, result={"summary": "a second result"})
    again = execute_body(task_id)

    assert again["status"] == "skipped" and again["reason"] == "E_TASK_NOT_RUNNABLE"
    assert spy.calls == []
    after = _row(queue_db, task_id)
    assert after["status"] == "completed"
    assert after["result_summary"] == before["result_summary"] == "the first attempt's result"
    assert after["attempts"] == before["attempts"] == 1

    assert queue_db.count("agent_runs", "task_id = ?", (task_id,)) == 1
    assert queue_db.count(
        "activity_log", "action = 'task_completed' AND summary LIKE ?", ("%first attempt%",)
    ) == 1


# â”€â”€ 7. terminal-state guard: an owner decision that lands DURING the run â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _cancel_while_running(queue_db, service, task_id: int, *, to: str = "cancelled", summary: str):
    """A `RunSpy` delegate: the owner's decision lands between the claim and the outcome write.

    `seen_at_decision` is the row the FILE holds at the instant the delegate returns — i.e. after
    the cancel committed and before the worker has written anything.  Recording it makes the
    injected failure an observable fact instead of an assumption about ordering.
    """
    seen_at_decision: list[str] = []

    def _delegate(agent_type, task_dict, context):  # noqa: ANN001 - the seam's own signature
        async def _cancel() -> None:
            async with queue_db.factory() as db:
                await service.transition_task_status(
                    db, task_id, to, result_summary=summary, actor="owner"
                )
                await db.commit()

        asyncio.run_coroutine_threadsafe(_cancel(), get_process_loop()).result(timeout=30)
        seen_at_decision.append(queue_db.row(task_id).get("status"))
        return {"summary": "the run finished after the owner had already decided"}

    _delegate.seen_at_decision = seen_at_decision  # type: ignore[attr-defined]
    return _delegate


def test_7_an_owner_cancel_during_the_run_is_not_stomped_by_the_outcome(
    queue_db, install_run_spy, execute_body, service
):
    """W5-LD-03 â€” RED: the completion write overwrote `cancelled` with `completed`.

    The lease-lost guard only fires on a HEARTBEAT tick, and the interval is
    `max(5 s, lease/3)` â€” 300 s at the live lease of 900 s.  An owner who cancels a run that
    finishes inside that window had their decision erased by the worker, with the run's summary and
    a `task_completed` "success" row on top.  The outcome write now CONFIRMS it still owns the claim
    and, when the row has moved on, keeps the owner's status, preserves the run's own evidence and
    records the supersession by name.
    """
    task_id = _create(queue_db, title="cancelled mid-run")
    dependent = _create(queue_db, title="dependent", metadata={"depends_on_ids": [task_id]})
    delegate = _cancel_while_running(
        queue_db, service, task_id, summary="the owner's own reason"
    )
    # `install_run_spy`'s own `delegate` argument is a BOOLEAN ("also run the real agent"); the
    # callable seam is the `RunSpy.delegate` attribute, so the injection is installed there.
    spy = install_run_spy(delegate=False)
    spy.delegate = delegate

    outcome = execute_body(task_id)

    assert delegate.seen_at_decision == ["cancelled"], (  # type: ignore[attr-defined]
        "the injected cancel did not land before the outcome write — the injection itself failed, "
        f"so this test would prove nothing (saw {getattr(delegate, 'seen_at_decision', None)!r})"
    )
    assert len(spy.calls) == 1, "the injected run seam was never used"
    row = _row(queue_db, task_id)
    assert row["status"] == "cancelled", (
        "the worker's completion stomped the owner's cancel â€” a human decision disappeared"
    )
    assert row["result_summary"] == "the owner's own reason", (
        "the worker wrote its own summary over the owner's"
    )
    assert row["error_message"] is None

    # The run's EVIDENCE is preserved on the ledger â€” the work really did happen.
    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["output"], "the finished run's output was destroyed with the superseded outcome"

    # The supersession is NAMED, and no success row claims the task completed.
    assert queue_db.count("activity_log", "action = 'task_outcome_superseded'") == 1
    assert queue_db.count("activity_log", "action = 'task_completed'") == 0

    # The delivery tells the truth about what it did.
    assert outcome["status"] == "completed", outcome
    assert outcome["superseded_by"] == "cancelled", outcome
    assert outcome["dag_dependents"] is None, (
        "the cancelled row's DAG was settled by the cancel; the abandoned outcome must not re-settle"
    )

    # The cancel itself already failed the dependent BY NAME (the service leg), and the worker's
    # abandoned outcome did not resurrect or re-release it.
    dependent_row = _row(queue_db, dependent)
    assert dependent_row["status"] == "failed"
    assert "dependency_blocked" in (dependent_row["error_message"] or "")


def test_7b_a_run_that_still_owns_its_claim_writes_its_outcome_normally(
    queue_db, install_run_spy, execute_body
):
    """The guard must not refuse the ordinary case (the ownership check is not a blanket skip)."""
    task_id = _create(queue_db, title="ordinary completion")
    install_run_spy(delegate=False, result={"summary": "all good"})

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    assert "superseded_by" not in outcome
    row = _row(queue_db, task_id)
    assert row["status"] == "completed" and row["result_summary"] == "all good"
    assert queue_db.count("activity_log", "action = 'task_completed'") == 1
    assert queue_db.count("activity_log", "action = 'task_outcome_superseded'") == 0


# â”€â”€ 8. vocabulary coherence: a stale verdict must not outlive its attempt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_8_a_stale_partial_verdict_does_not_outlive_the_attempt(
    queue_db, install_run_spy, execute_body, service
):
    """W5-LD-04 â€” RED: `execution_status: "partial"` + `needs_attention: true` survived completion.

    The verdict is per ATTEMPT (`metadata.result_status`) and served as the ROW's state, so a row
    that recorded `partial` and then ran again (a re-park, a resume) advertised unfinished work for
    ever.  The completion retires it; the row's served state is the completing attempt's.
    """
    task_id = _create(
        queue_db,
        title="stale verdict",
        metadata={"result_status": "partial", "verdict": {"status": "partial"}, "keep": 1},
    )
    before = _run_on_loop(queue_db, lambda db: service.get_task(db, task_id))
    assert service.task_execution_state(before) == ("partial", True)

    install_run_spy(delegate=False, result={"summary": "the later attempt finished cleanly"})
    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    assert outcome["payload_status"] is None
    meta = _metadata(queue_db, task_id)
    assert "result_status" not in meta, (
        "the superseded verdict is still served as the row's execution_status"
    )
    assert meta["last_attempt_verdict"]["status"] == "partial"
    assert meta["keep"] == 1, "the retirement must merge, not replace"
    after = _run_on_loop(queue_db, lambda db: service.get_task(db, task_id))
    assert service.task_execution_state(after) == (None, False)


def test_8b_a_partial_verdict_from_THIS_attempt_is_still_served(queue_db, install_run_spy, execute_body, service):
    """The other half of the rule: the completing attempt's OWN verdict must survive.

    The worker merges `result_status` AFTER the completion write retired the stale one, so the two
    rules compose instead of fighting.
    """
    task_id = _create(queue_db, title="partial now", metadata={"result_status": "partial"})
    install_run_spy(delegate=False, result={"summary": "[partial] the plan is half done", "payload_status": "partial"}
    )

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    assert outcome["payload_status"] == "partial"
    meta = _metadata(queue_db, task_id)
    assert meta["result_status"] == "partial"
    row_after = _run_on_loop(queue_db, lambda db: service.get_task(db, task_id))
    assert service.task_execution_state(row_after) == ("partial", True)


# â”€â”€ 9. approval deny: the parked row is spent, and a re-park needs a fresh token â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_9_a_denied_parked_row_is_terminal_and_cannot_be_reopened(
    queue_db, install_run_spy, execute_body, service
):
    """The owner REFUSES a parked approval (`awaiting_approval -> cancelled`).

    The denial must spend everything the park minted â€” the parked set, the token and its binding â€”
    in the SAME transaction, so the served `approval_required` flag stops saying "pending" and no
    stale bearer credential can ride a later write.  And the refusal is final: a redelivery cannot
    reopen the row.
    """
    parked = _create(
        queue_db,
        title="parked for approval",
        metadata={
            "approval_required": [
                {"action": "send", "ref": "a@example.com", "channel": "email",
                 "fingerprint": "fp-denied", "reason": "context tainted"},
            ],
            "approval_token": "a-token-the-owner-never-gets-to-use",
            "approval_token_fingerprint": "fp-denied",
        },
    )
    _run_on_loop(queue_db, lambda db: _set_parked(db, service, parked))

    async def _deny(db):
        await service.transition_task_status(db, parked, "cancelled", actor="owner")
        await db.commit()

    _run_on_loop(queue_db, _deny)

    row = _row(queue_db, parked)
    assert row["status"] == "cancelled"
    meta = _metadata(queue_db, parked)
    assert meta["approval_required"] == [], "the served approval flag still says 'pending'"
    assert "approval_token" not in meta, "a bearer credential survived the denial"
    assert "approval_token_fingerprint" not in meta
    assert meta["approval_cleared"]["status"] == "cancelled"
    assert meta["approval_cleared"]["fingerprints"] == ["fp-denied"]

    spy = install_run_spy(delegate=False, result={"summary": "must not run"})
    outcome = execute_body(parked)
    assert outcome["status"] == "skipped"
    assert spy.calls == []
    assert _row(queue_db, parked)["status"] == "cancelled"
    assert queue_db.count("agent_runs", "task_id = ?", (parked,)) == 0


def test_the_terminal_vocabulary_matches_the_service(queue_db):
    """The worker's terminal-status copy must not drift from the service's set.

    `_TERMINAL_STATUSES` is a literal copy (the service's set is private and the caller's
    from-import set is a pinned contract), so the two are pinned EQUAL here rather than trusted.
    """
    from app.services import task_service

    assert agent_tasks._TERMINAL_STATUSES == task_service._TERMINAL_TASK_STATUSES


async def _set_parked(db, service, task_id: int) -> None:
    """Move a `pending` row to `awaiting_approval` (the state the worker's parking branch writes)."""
    row = await service.get_task(db, task_id)
    row.status = "awaiting_approval"
    await db.commit()


# â”€â”€ 10. junk metadata: the live `[null]` shape must not kill the delivery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def test_10_a_json_array_in_metadata_does_not_kill_the_completion(
    queue_db, install_run_spy, execute_body, service
):
    """W5-LD-04's neighbour, A-W4-03's REACHABLE half â€” RED: `TypeError: cannot convert â€¦ [None]`.

    A live row was observed carrying the JSON array `[null]` in `tasks.metadata` (wave-4 taskmaster's
    repair of task 313; the writer was never identified).  The worker's outcome merge did
    `dict(row.metadata_json or {})`, so that ONE blob turned the row's next completion into an
    unnamed `TypeError` INSIDE the outcome transaction â€” no task write, no activity row, and the
    delivery died.  The normaliser preserves the blob (bounded, labelled) instead.
    """

    async def _plant(db):
        row = await service.get_task(db, task_id)
        row.metadata_json = [None]           # the observed live shape, byte for byte
        await db.commit()

    task_id = _create(queue_db, title="junk metadata row")
    _run_on_loop(queue_db, _plant)
    assert queue_db.row(task_id)["metadata"] == "[null]"
    install_run_spy(delegate=False, result={"summary": "done anyway", "payload_status": "partial"})

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    row = _row(queue_db, task_id)
    assert row["status"] == "completed"
    meta = _metadata(queue_db, task_id)
    assert isinstance(meta, dict)
    assert meta["result_status"] == "partial", "the attempt's own verdict still lands"
    foreign = meta["foreign_metadata"]
    assert foreign["stored_type"] == "list"
    assert foreign["value"] == "[null]", "the blob was destroyed instead of preserved"
    assert foreign["recorded_by"] == "task_metadata_bag"


def test_10b_a_junk_metadata_blob_does_not_kill_a_paused_release(
    queue_db, install_run_spy, execute_body, service, monkeypatch
):
    """The pause gate merges the same bag on EVERY dispatch, before any model call.

    It is the widest-reach merge site in the worker (and the cheapest to hit): a paused company
    releases the claim and stamps `metadata.paused_release`.  On the pre-fix tree the release
    raised, so the row stayed `pending` with a burned attempt and no marker â€” the exact "work
    written off" defect the pause gate was built to avoid.
    """

    async def _plant(db):
        row = await service.get_task(db, task_id)
        row.metadata_json = [None]
        await db.commit()

    async def _paused(_db):  # noqa: ANN001 - the gate's own seam (`resolve_dispatch_paused(db)`)
        return True

    task_id = _create(queue_db, title="paused junk metadata")
    _run_on_loop(queue_db, _plant)
    spy = install_run_spy(delegate=False, result={"summary": "must not run"})
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "resolve_dispatch_paused", _paused)

    outcome = execute_body(task_id)

    assert outcome["status"] == "skipped", outcome
    assert outcome["requeued"] is True
    assert spy.calls == []
    row = _row(queue_db, task_id)
    assert row["status"] == "pending", "the pause release must still leave a claimable row"
    assert row["attempts"] == 0, "a pre-execution refusal must refund the claim's attempt"
    meta = _metadata(queue_db, task_id)
    assert meta["foreign_metadata"]["stored_type"] == "list"
    assert meta["paused_release"]["reason"]
