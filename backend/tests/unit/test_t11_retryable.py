"""T11 / D7 — `E_RATE_LIMITED` is NOT retryable, and the pricing residual is documented.

D7 (owner ruling): a daily-cap refusal is "refused before charge" — nothing was performed, and
re-delivering the run spends `agent_max_retries` more model calls working around a limit that
resets on its own schedule.  So `E_RATE_LIMITED` leaves `celery_app.tasks.agent_tasks.
RETRYABLE_CODES`; every other member stays exactly where it was.

RED leg (measured, receipt §RED): put `E_RATE_LIMITED` back into `RETRYABLE_CODES` and
`test_a_cap_refused_run_is_not_redelivered` fails (the eager task re-delivers: the spy is called
twice, and the outcome comes back as a `Retry` instead of the refusal).

The second half of the ticket is documentation with a test: `pricing`'s docstring now states the
L3-05 residual honestly — the token->cost estimate prices only the SUCCESSFUL attempt's envelope,
so tokens an aborted/timed-out attempt already burned (and earlier retry attempts' envelopes) are
NOT summed; a token-estimated `cost_usd` is a lower bound, never fabricated upward.
"""
from __future__ import annotations

from app.core.errors import (
    E_AGENT_BAD_OUTPUT,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_TIMEOUT,
    E_DB,
    E_INTEGRATION,
    E_RATE_LIMITED,
    E_REDIS,
    PolsiaError,
)

# OLD (pre-D7) set, recorded for the receipt:
#   {E_CLI_TIMEOUT, E_CLI_EXIT, E_CLI_JSON, E_AGENT_BAD_OUTPUT, E_INTEGRATION, E_RATE_LIMITED,
#    E_DB, E_REDIS}
# NEW: the same set with `E_RATE_LIMITED` removed and nothing else touched.
_EXPECTED_RETRYABLE = frozenset({
    E_CLI_TIMEOUT,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_AGENT_BAD_OUTPUT,
    E_INTEGRATION,
    E_DB,
    E_REDIS,
})


def test_the_retryable_set_is_exactly_the_pinned_one():
    from celery_app.tasks.agent_tasks import RETRYABLE_CODES

    assert RETRYABLE_CODES == _EXPECTED_RETRYABLE, (
        "the retryable set changed beyond D7: "
        f"added={sorted(RETRYABLE_CODES - _EXPECTED_RETRYABLE)} "
        f"removed={sorted(_EXPECTED_RETRYABLE - RETRYABLE_CODES)}"
    )
    assert E_RATE_LIMITED not in RETRYABLE_CODES


def test_the_decision_function_refuses_a_cap_refusal_and_keeps_the_rest():
    from celery_app.tasks.agent_tasks import is_retryable

    assert is_retryable(PolsiaError(E_RATE_LIMITED, "daily cap reached")) is False
    assert is_retryable(None) is True                      # no exception -> nothing failed
    assert is_retryable(PolsiaError(E_CLI_TIMEOUT, "hang")) is True
    assert is_retryable(PolsiaError(E_REDIS, "broker")) is True
    assert is_retryable(PolsiaError("E_POLICY_DENIED", "no")) is False
    assert is_retryable(RuntimeError("driver")) is True     # unexpected failures stay transient


# ── the end-to-end decision through the REAL eager task ────────────────────────────────────────


def _task_id(queue_db) -> int:
    return queue_db.create_task(agent_type="social_media", title="cap-refusal probe")


def test_a_cap_refused_run_is_not_redelivered(queue_db, eager_delivery, install_run_spy):
    """One delivery, one agent call: the refusal is terminal for this run."""
    spy = install_run_spy(
        delegate=False,
        error_factory=lambda: PolsiaError(E_RATE_LIMITED, "daily cap reached for twitter"),
    )
    task_id = _task_id(queue_db)

    result = eager_delivery(task_id, throw=False)

    assert spy.calls, "the run must actually happen once"
    assert len(spy.calls) == 1, (
        f"a cap-refused dispatch was re-delivered {len(spy.calls)} times (D7: it must not churn)"
    )
    # The eager result carries the failure (the task raised the refusal, not a Retry).
    assert result.failed() or result.result is not None
    row = queue_db.row(task_id)
    assert row["status"] == "failed", row


def test_a_still_retryable_failure_IS_redelivered(queue_db, eager_delivery, install_run_spy):
    """The control that proves the harness can SEE a retry (and that D7 changed only the cap).

    `E_CLI_TIMEOUT` (a RAISED timeout, the documented retryable shape) goes through
    `self.retry` -> eager mode re-delivers -> the spy is called more than once.
    """
    spy = install_run_spy(
        delegate=False,
        error_factory=lambda: PolsiaError(E_CLI_TIMEOUT, "provider hang"),
    )
    task_id = _task_id(queue_db)

    eager_delivery(task_id, throw=False)

    assert len(spy.calls) > 1, (
        "a retryable failure must still be re-delivered; without this control the D7 pin "
        "could pass for the wrong reason (a harness that never sees retries at all)"
    )


# ── the documented residual (L3-05 / fix-3 §5) ────────────────────────────────────────────────


def test_the_pricing_module_documents_the_aborted_attempt_residual():
    from app.agents import pricing

    doc = pricing.__doc__ or ""
    assert "RESIDUAL" in doc
    assert "aborted" in doc.lower()
    assert "lower bound" in doc.lower(), "the estimate's direction must be stated"
    # …and the site where the estimate becomes a ledger number points at it
    from app.agents import base_agent

    source = base_agent.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "L3-05 RESIDUAL" in text
