"""SV-W4-03 — a completed row must not SERVE a superseded attempt's error, and must not LOSE it.

Live defect (systems-validator wave 4): `GET /api/v1/tasks/313` answered `status:"completed"` AND
`error_message:"PolsiaError: E_AGENT_BAD_OUTPUT … twitter content must be <= 280 chars"` — the
attempt-1 validator failure, superseded by the attempt-3 run that actually completed.  Any surface
painting `error_message` showed a finished task as failed.  (The column-clearing half landed in
wave 2 as the F-23 residue fix; the live 313 row predates it, and the text was simply dropped from
the row with no trace beyond the run ledger.)

This file pins the SV-W4-03 shape — the failure history is RETIRED, never erased:

  * a completion moves a non-empty `error_message` into `metadata.last_attempt_error` with the
    message, the attempt count, who retired it and on which status, then NULLs the column;
  * `tasks.attempts` and the failed `agent_runs` row are untouched (attempt history stays honest);
  * both completed-writers behave identically (`update_task_status`, `transition_task_status`);
  * the control: a REQUEUE (`pending`) KEEPS the column — the retry has not happened yet;
  * a completion with nothing to retire invents no key (no fabricated history);
  * the retired text is BOUNDED, and the truncation is STATED in the kept text.

RED leg: restore `task.error_message = None` in place of `_retire_error_message(...)` in
`update_task_status` -> `test_completed_after_failure_retires_the_error_and_clears_the_column`
fails on the missing `last_attempt_error` key (raw output in `taskmaster/raw/w4/RED-A.txt`).
"""
from __future__ import annotations

import json

from app.core.errors import E_AGENT_BAD_OUTPUT
from app.models.task import AgentRun, Task

#: The live validator text's shape (task 313's attempt-1 failure), verbatim enough to be honest.
VALIDATOR_TEXT = (
    "social_media failed: POLSIA-E110 1 validation error for SocialMediaOutput posts Value error, "
    "twitter content must be <= 280 chars"
)
#: The shape the worker writes: `f"{type(failure).__name__}: {failure}"` (agent_tasks.py:2447).
ATTEMPT_ONE_ERROR = f"PolsiaError: {E_AGENT_BAD_OUTPUT}: {VALIDATOR_TEXT}"


def _metadata(row: dict) -> dict:
    raw = row.get("metadata")
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


async def _fail_attempt_one(queue_db, task_id: int, *, attempts: int = 2) -> None:
    """Leave the row exactly as a failed-first-attempt + retry would: error, attempts, run row."""
    async with queue_db.factory() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        task.attempts = attempts
        db.add(
            AgentRun(
                agent_type="social_media",
                run_type="task",
                status="failed",
                task_id=task_id,
                attempt=1,
                raw_log=ATTEMPT_ONE_ERROR,
            )
        )
        await db.flush()
        from app.services.task_service import update_task_status

        await update_task_status(db, task_id, "failed", error_message=ATTEMPT_ONE_ERROR)
        await db.commit()


async def _complete(queue_db, task_id: int, *, summary: str = "posted the shortened draft") -> None:
    async with queue_db.factory() as db:
        from app.services.task_service import update_task_status

        await update_task_status(db, task_id, "completed", result_summary=summary)
        await db.commit()


def test_completed_after_failure_retires_the_error_and_clears_the_column(queue_db):
    """The live 313 lie: `completed` + a stale attempt-1 error.  Both halves pinned."""
    task_id = queue_db.create_task(
        title="completed after a failed attempt", agent_type="social_media"
    )
    queue_db.run(_fail_attempt_one(queue_db, task_id))
    queue_db.run(_complete(queue_db, task_id))

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["result_summary"] == "posted the shortened draft"
    assert row["error_message"] is None, (
        "a completed row serving a superseded attempt's E_AGENT_BAD_OUTPUT text is the live 313 lie"
    )

    retired = _metadata(row).get("last_attempt_error")
    assert isinstance(retired, dict), "the failure history must be RETIRED, never dropped"
    assert retired["message"] == ATTEMPT_ONE_ERROR
    assert retired["attempts"] == 2, "the attempt the failure happened on is part of the history"
    assert retired["retired_by"] == "update_task_status"
    assert retired["retired_on_status"] == "completed"
    assert retired["retired_at"], "who/when must be auditable"


def test_attempt_history_survives_the_completion(queue_db):
    """`tasks.attempts` and the failed run row are the ledger of what happened — untouched."""
    task_id = queue_db.create_task(title="attempt history", agent_type="social_media")
    queue_db.run(_fail_attempt_one(queue_db, task_id, attempts=3))
    queue_db.run(_complete(queue_db, task_id))

    row = queue_db.row(task_id)
    assert row["attempts"] == 3, "the completion must not rewrite the attempt count"

    runs = queue_db.query(
        "SELECT status, attempt, raw_log FROM agent_runs WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    assert len(runs) == 1, "the failed attempt stays in the run ledger"
    assert runs[0]["status"] == "failed"
    assert runs[0]["attempt"] == 1
    assert VALIDATOR_TEXT in (runs[0]["raw_log"] or "")


def test_the_operator_completion_path_also_retires_the_error(queue_db):
    """Both completed-writers share the rule (`transition_task_status`, actor=owner)."""
    task_id = queue_db.create_task(title="operator completion", agent_type="finance")

    async def _run() -> None:
        from app.services.task_service import transition_task_status, update_task_status

        async with queue_db.factory() as db:
            # A queued row carrying an earlier attempt's failure text (PENDING keeps it).
            await update_task_status(db, task_id, "pending", error_message=ATTEMPT_ONE_ERROR)
            await db.commit()
        async with queue_db.factory() as db:
            await transition_task_status(
                db, task_id, "completed", result_summary="done by the operator", actor="owner"
            )
            await db.commit()

    queue_db.run(_run())

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["error_message"] is None
    retired = _metadata(row).get("last_attempt_error")
    assert isinstance(retired, dict)
    assert retired["message"] == ATTEMPT_ONE_ERROR
    assert retired["retired_by"] == "owner", "the actor that retired it is recorded"


def test_a_requeue_keeps_the_error_column(queue_db):
    """The control: a `pending` write has NOT ended the history — the column must keep saying it."""
    task_id = queue_db.create_task(title="retry still owed", agent_type="social_media")

    async def _requeue() -> None:
        from app.services.task_service import update_task_status

        async with queue_db.factory() as db:
            await update_task_status(db, task_id, "failed", error_message=ATTEMPT_ONE_ERROR)
            await db.commit()
        async with queue_db.factory() as db:
            await update_task_status(db, task_id, "pending")
            await db.commit()

    queue_db.run(_requeue())

    row = queue_db.row(task_id)
    assert row["status"] == "pending"
    assert row["error_message"] == ATTEMPT_ONE_ERROR, (
        "a requeued row keeps its error: the retry has not happened yet (F-19/u5 rule)"
    )
    assert "last_attempt_error" not in _metadata(row)


def test_a_clean_completion_invents_no_history(queue_db):
    """Nothing to retire -> no key.  A completion must never fabricate a prior failure."""
    task_id = queue_db.create_task(title="clean completion", agent_type="customer_support")
    queue_db.run(_complete(queue_db, task_id, summary="first attempt succeeded"))

    row = queue_db.row(task_id)
    assert row["status"] == "completed"
    assert row["error_message"] is None
    assert "last_attempt_error" not in _metadata(row)


def test_the_retired_text_is_bounded_with_a_stated_truncation(queue_db):
    """`tasks.error_message` is unbounded Text; the metadata bag must not grow without a bound."""
    from app.services.task_service import LAST_ATTEMPT_ERROR_MAX_CHARS

    huge = "E_AGENT_BAD_OUTPUT: " + ("x" * (LAST_ATTEMPT_ERROR_MAX_CHARS + 500))
    task_id = queue_db.create_task(title="oversized failure text", agent_type="social_media")

    async def _fail_then_complete() -> None:
        from app.services.task_service import update_task_status

        async with queue_db.factory() as db:
            await update_task_status(db, task_id, "failed", error_message=huge)
            await db.commit()
        async with queue_db.factory() as db:
            await update_task_status(db, task_id, "completed", result_summary="recovered")
            await db.commit()

    queue_db.run(_fail_then_complete())

    row = queue_db.row(task_id)
    kept = _metadata(row)["last_attempt_error"]["message"]
    assert kept.startswith("E_AGENT_BAD_OUTPUT: ")
    assert kept.endswith(f"… [truncated at {LAST_ATTEMPT_ERROR_MAX_CHARS} chars]"), (
        "truncation must be STATED in the kept text, never silent"
    )
    assert len(kept) == LAST_ATTEMPT_ERROR_MAX_CHARS + len(
        f"… [truncated at {LAST_ATTEMPT_ERROR_MAX_CHARS} chars]"
    )
