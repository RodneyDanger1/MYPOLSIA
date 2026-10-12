"""The retry ladder: what `run_agent_task` does with a retryable vs a non-retryable failure.

The ladder lives in the sync wrapper (`run_agent_task`), not in the async body:
`outcome["retryable"] and attempts <= settings.agent_max_retries` asks Celery for a retry with
exponential backoff; anything else is terminal and is RAISED (a failed run must be a FAILURE
task state, never a success-shaped dict the result backend stores as SUCCESS).

`retry_countdown(attempts) = min(600, task_retry_backoff_seconds * 2**(attempts-1))`, where
`attempts` is the row's counter after this failure — with the shipped defaults (20 s base,
`agent_max_retries=2`) the first retry waits 40 s.

The ladder is exercised two ways: the Celery task called directly (recording the real
`retry` call), and through the real eager path (`apply()`), where Celery's eager retry
re-executes the task body — the closest thing to a worker redelivery that exists in-process.
"""
from __future__ import annotations

import pytest

from app.core.errors import E_CLI_TIMEOUT, E_POLICY_DENIED, PolsiaError


@pytest.fixture
def retry_recorder(monkeypatch):
    """Record every real `Task.retry(...)` call, then perform it (nothing is faked)."""
    from celery_app.worker import app as celery_app_object

    # The module-level `run_agent_task` is a lazy Celery proxy; the registry holds the real
    # task instance (and it is the one `.apply()` resolves to).
    task = celery_app_object.tasks["celery_app.tasks.agent_tasks.run_agent_task"]
    real_retry = task.retry
    recorded: list[dict] = []

    def recording_retry(self, **kwargs):
        recorded.append(
            {
                "countdown": kwargs.get("countdown"),
                "exc": kwargs.get("exc"),
                "exc_code": getattr(kwargs.get("exc"), "code", None),
            }
        )
        return real_retry(**kwargs)

    monkeypatch.setattr(type(task), "retry", recording_retry)
    return recorded


def test_invariant_5_a_retryable_failure_asks_for_a_retry_with_the_ladder_backoff(
    queue_db, install_run_spy, retry_recorder
):
    """INVARIANT 5a — retryable ⇒ exactly one retry requested, with the ladder's countdown.

    The task is called directly, so Celery's `retry()` takes its `called_directly` branch and
    re-raises the original exception (the same exception object the ladder passed in) — which is
    also what pins that the ORIGINAL `PolsiaError` is what the worker would see.

    RED leg: make the wrapper treat every failure as terminal (drop the
    `outcome.get("retryable") and attempts <= ...` condition) — no retry is requested and this
    test fails on `len(retry_recorder) == 1`.
    """
    from celery_app.tasks.agent_tasks import retry_countdown, run_agent_task

    task_id = queue_db.create_task(title="ladder-retryable")
    install_run_spy(error_factory=lambda: PolsiaError(E_CLI_TIMEOUT, "test-only transport failure"))

    with pytest.raises(PolsiaError) as raised:
        run_agent_task(task_id)

    assert raised.value.code == E_CLI_TIMEOUT, "the original failure must propagate"
    assert len(retry_recorder) == 1, "a retryable failure must ask for exactly one retry"
    assert retry_recorder[0]["exc_code"] == E_CLI_TIMEOUT
    assert retry_recorder[0]["countdown"] == retry_countdown(2) == 40.0, (
        "the backoff is the ladder's, computed from the attempts the row now carries"
    )

    row = queue_db.row(task_id)
    assert row["attempts"] == 2, "one claim + one recorded failure"
    assert row["status"] == "pending", "the row is requeued for the retry it just scheduled"


def test_invariant_5_a_non_retryable_failure_is_terminal_and_never_asks_for_a_retry(
    queue_db, install_run_spy, retry_recorder
):
    """INVARIANT 5b — a business/policy failure is terminal: no retry, the failure is raised.

    RED leg: add `or True` to the retry condition (i.e. retry everything) — this test fails on
    `retry_recorder == []`.
    """
    from celery_app.tasks.agent_tasks import run_agent_task

    task_id = queue_db.create_task(title="ladder-terminal")
    install_run_spy(
        error_factory=lambda: PolsiaError(E_POLICY_DENIED, "test-only refusal: nothing was sent")
    )

    with pytest.raises(PolsiaError) as raised:
        run_agent_task(task_id)

    assert raised.value.code == E_POLICY_DENIED
    assert retry_recorder == [], "a terminal failure must never be retried"

    row = queue_db.row(task_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 2


def test_invariant_5_the_ladder_retries_once_and_the_second_failure_is_terminal(
    queue_db, install_run_spy, eager_delivery, retry_recorder
):
    """INVARIANT 5c — end to end through Celery eager mode: retry #1 re-runs, failure #2 stops.

    `apply()` runs the task in-process; when the body asks for a retry, Celery's eager path
    re-executes the task body with `retries` incremented — exactly the redelivery the ladder
    depends on.  With an always-failing agent the ladder must run the agent exactly twice (the
    first execution and its retry), then go terminal: `apply()` reports FAILURE, the retry
    recorder holds exactly one request, and the row carries the second failure.

    RED leg (observed before the requeue fix in the receipt): with a retryable failure writing
    `failed`, the redelivered body cannot claim its own row, so it returns
    `{"status": "skipped", "reason": "E_TASK_NOT_RUNNABLE"}` — the agent runs once, `apply()`
    reports SUCCESS (a failure turned into a success-shaped result), and this test fails on both
    counts.
    """
    task_id = queue_db.create_task(title="ladder-e2e")
    spy = install_run_spy(
        error_factory=lambda: PolsiaError(E_CLI_TIMEOUT, "test-only transport failure")
    )

    result = eager_delivery(task_id)

    assert len(spy.calls) == 2, "the retry must have re-run the agent (first attempt + retry)"
    assert result.state == "FAILURE", "the second failure is terminal and must be a FAILURE"
    assert len(retry_recorder) == 1, "the ladder asks for exactly one retry (the row's counter is spent)"

    row = queue_db.row(task_id)
    assert row["status"] == "failed", "after the retry's failure the row is terminal"
    assert row["attempts"] == 4, "two claims + two recorded failures"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert E_CLI_TIMEOUT in (row["error_message"] or "")

    runs = queue_db.query("SELECT status FROM agent_runs WHERE task_id = ? ORDER BY id", (task_id,))
    assert [r["status"] for r in runs] == ["failed", "failed"], "one run ledger row per execution"
