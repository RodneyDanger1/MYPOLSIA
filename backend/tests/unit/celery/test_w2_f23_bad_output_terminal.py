"""F-23 — a validator failure is a NAMED TERMINAL state, never a retried-then-wedged row.

Live incident (2026-10-28): task 313 (`social_media`) produced a twitter post longer than the
280-char validator, the run failed `E_AGENT_BAD_OUTPUT` (`MERIDIAN-E110`), and the worker spent TWO
more model calls re-asking the identical prompt (`attempts=2`, `row.error_message` naming the
validator).  The retry message then died with the worker, leaving a `pending` row with no delivery
and no second net — the wedge.  The repo's own agent-side doctrine already says the opposite of
what the worker did: `structured_output.SCHEMA_VALIDATION_FAILED` → "only then repair the payload"
(inside the run) and `REFUSAL` → "Never re-ask the identical prompt."

This file pins both halves of the fix:
  * `E_AGENT_BAD_OUTPUT` is terminal (one execution, the named failure, no retry);
  * a row requeued for a retry carries the reconciler's `rearmed_at` marker, so an accept-then-lost
    retry message is re-dispatched instead of stranding the row;
  * the retryable infrastructure codes still retry (the other side of the same pin).
"""
from __future__ import annotations

import json

import pytest

from app.core.errors import E_AGENT_BAD_OUTPUT, E_CLI_TIMEOUT, E_POLICY_DENIED, MeridianError

#: The live validator text (verbatim shape from task 313's `tasks.error_message`).
VALIDATOR_TEXT = (
    "social_media failed: MERIDIAN-E110 1 validation error for SocialMediaOutput posts Value error, "
    "twitter content must be <= 280 chars"
)


@pytest.fixture
def retry_recorder(monkeypatch):
    """Record every real `Task.retry(...)` call, then perform it (nothing is faked)."""
    from celery_app.worker import app as celery_app_object

    task = celery_app_object.tasks["celery_app.tasks.agent_tasks.run_agent_task"]
    real_retry = task.retry
    recorded: list[dict] = []

    def recording_retry(self, **kwargs):
        recorded.append({"countdown": kwargs.get("countdown"),
                         "exc_code": getattr(kwargs.get("exc"), "code", None)})
        return real_retry(**kwargs)

    monkeypatch.setattr(type(task), "retry", recording_retry)
    return recorded


def _metadata(db, task_id: int) -> dict:
    raw = db.row(task_id).get("metadata")
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def test_the_classifier_keeps_validator_failures_out_of_the_retryable_set():
    from celery_app.tasks.agent_tasks import RETRYABLE_CODES, is_retryable

    assert E_AGENT_BAD_OUTPUT not in RETRYABLE_CODES
    assert is_retryable(MeridianError(E_AGENT_BAD_OUTPUT, VALIDATOR_TEXT)) is False
    # The infrastructure set is untouched: one attempt at each, then the ladder (not this fix).
    for code in (E_CLI_TIMEOUT,):
        assert code in RETRYABLE_CODES
        assert is_retryable(MeridianError(code, "transient")) is True


def test_a_validator_failure_is_terminal_after_one_execution(queue_db, install_run_spy,
                                                            retry_recorder):
    """RED leg: restore `E_AGENT_BAD_OUTPUT` to RETRYABLE_CODES → the retry is requested.

    Observed before the fix (live shape): `retry_recorder` gets one call, the row is written
    `pending` for a retry that re-asks the identical prompt, and only the ladder cap ends it.
    """
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="oversized twitter post", agent_type="social_media")
    spy = install_run_spy(error_factory=lambda: MeridianError(E_AGENT_BAD_OUTPUT, VALIDATOR_TEXT))

    with pytest.raises(MeridianError) as raised:
        run_agent_task(task_id)

    assert raised.value.code == E_AGENT_BAD_OUTPUT
    assert retry_recorder == [], "a deterministic validator failure must never be retried"
    assert len(spy.calls) == 1, "exactly ONE execution — no second model call on the same prompt"

    row = queue_db.row(task_id)
    assert row["status"] == "failed", "the terminal state is the row's own, not a pending wedge"
    assert row["attempts"] == 1
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert E_AGENT_BAD_OUTPUT in (row["error_message"] or "")
    assert "280" in (row["error_message"] or ""), "the owner reads the validator's own reason"

    runs = queue_db.query("SELECT status FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,))
    assert [run["status"] for run in runs] == ["failed"], "one run ledger row, one attempt"


def test_an_eager_validator_failure_ends_failed_and_does_not_re_execute(queue_db,
                                                                        install_run_spy,
                                                                        eager_delivery):
    """Through Celery's own eager path: FAILURE after ONE call, no re-execution."""
    task_id = queue_db.create_task(title="oversized twitter post eager",
                                   agent_type="social_media")
    spy = install_run_spy(error_factory=lambda: MeridianError(E_AGENT_BAD_OUTPUT, VALIDATOR_TEXT))

    result = eager_delivery(task_id)

    assert result.state == "FAILURE"
    assert len(spy.calls) == 1
    assert queue_db.row(task_id)["status"] == "failed"


def test_a_retryable_failure_still_retries_and_carries_the_reconciler_marker(queue_db,
                                                                            install_run_spy,
                                                                            retry_recorder):
    """The other side: infrastructure failures keep the ladder, and the requeue is covered.

    The retry is a NEW message published after the row commit; if the process dies in that window
    the row would be `pending` with no delivery.  `metadata.rearmed_at` is the marker
    `reconcile_stranded_pending_tasks` re-dispatches after one window (`_common.stamp_rearmed`).
    """
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="transport failure")
    install_run_spy(error_factory=lambda: MeridianError(E_CLI_TIMEOUT, "test-only transport failure"))

    with pytest.raises(MeridianError):
        run_agent_task(task_id)

    assert len(retry_recorder) == 1
    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    assert E_CLI_TIMEOUT in (row["error_message"] or ""), (
        "a REQUEUED failure keeps its error text — the retry's history must survive"
    )
    marker = _metadata(queue_db, task_id)
    assert marker.get("rearm_reason") == "retry_requeued"
    assert marker.get("rearmed_at"), "a lost retry message must be recoverable by the reconciler"


def test_a_completed_row_drops_the_previous_attempts_error_text(queue_db):
    """F-23 residue (live: task 313 completed at 21:23 still carrying the validator error).

    A `completed` row must not serve an earlier attempt's failure as if it were the outcome.  The
    REQUEUE path keeps it (the retry has not happened yet) — pinned above.
    """
    from app.services.task_service import update_task_status

    task_id = queue_db.create_task(title="completed after a failed attempt", agent_type="social_media")

    async def _fail_then_complete() -> None:
        async with queue_db.factory() as session:
            await update_task_status(
                session, task_id, "failed",
                error_message=f"MeridianError: {E_AGENT_BAD_OUTPUT}: {VALIDATOR_TEXT}",
            )
            await session.commit()
        async with queue_db.factory() as session:
            await update_task_status(
                session, task_id, "completed", result_summary="posted the shortened draft"
            )
            await session.commit()

    queue_db.run(_fail_then_complete())

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["result_summary"] == "posted the shortened draft"
    assert row["error_message"] is None, (
        "a completed row keeping the previous attempt's E_AGENT_BAD_OUTPUT text is the live 313 lie"
    )


def test_the_operator_completion_path_also_clears_the_error(queue_db):
    """Both completed-writers agree: the API's transition path clears it too (PENDING→COMPLETED)."""
    from app.services.task_service import transition_task_status, update_task_status

    task_id = queue_db.create_task(title="operator completion after a retry", agent_type="finance")

    async def _run() -> None:
        async with queue_db.factory() as session:
            await update_task_status(
                session, task_id, "pending", error_message=f"{E_CLI_TIMEOUT}: transient"
            )
            await session.commit()
        async with queue_db.factory() as session:
            await transition_task_status(
                session, task_id, "completed", result_summary="done by the operator",
                actor="owner",
            )
            await session.commit()

    queue_db.run(_run())

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["error_message"] is None


def test_a_non_retryable_business_refusal_still_ends_failed_without_a_retry(queue_db,
                                                                           install_run_spy,
                                                                           retry_recorder):
    """Control: the terminal path itself is unchanged by the F-23 classification."""
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="policy refusal")
    install_run_spy(error_factory=lambda: MeridianError(E_POLICY_DENIED, "test-only refusal"))

    with pytest.raises(MeridianError) as raised:
        run_agent_task(task_id)

    assert raised.value.code == E_POLICY_DENIED
    assert retry_recorder == []
    assert queue_db.row(task_id)["status"] == "failed"
