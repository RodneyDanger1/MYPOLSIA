"""W5 / logic-debugger — the task-state rules, proven on the states a happy path never reaches.

Three classes of defect, all reachable without a model call:

  * **A-W4-01 — a completion that SUPPLIES an `error_message`.**  SV-W4-03 taught the two
    completed-writers to RETIRE a row's existing error, but both branches were `elif`: a caller
    that *supplies* an error together with `completed` skipped the retirement entirely, so the row
    was served `status: completed` + `error_message: "<the failure>"` — the exact lie SV-W4-03
    closed for the no-error path.  Reachable through `PATCH /tasks/{id}/status`.
  * **A-W4-03 — a non-object `tasks.metadata` blob.**  Thirteen product sites merged that column
    with `dict(metadata_json or {})`; a JSON **array**/string/number raises `TypeError` on the
    next merge, which is a 500 (or a dead delivery) long after the write that caused it.  A live
    row was observed carrying `[null]` (see the auditor's A-W4-03 and the wave-4 taskmaster
    repair), so the state is not hypothetical.
  * **the stale attempt verdict** — `metadata.result_status` is written per ATTEMPT but served as
    the ROW's state.  A row that recorded `partial`, then ran again and finished cleanly, kept
    saying `partial`/`needs_attention` for ever: the same "superseded evidence outlives its
    attempt" class as the retired error, one key over.

Every test was RED on the pre-fix tree; the RED transcript is in `logic-debugger/raw/w5/`.
"""
from __future__ import annotations

import pytest

from app.core.errors import E_TASK_NOT_RUNNABLE, E_VALIDATION, MeridianError

# ── helpers ────────────────────────────────────────────────────────────────────────────────────


async def _new_task(db, **overrides):
    """One pending row (the service's own creator — the production boundary)."""
    from app.services.task_service import create_task

    kwargs = {"title": "w5 state rule", "agent_type": "finance", "source": "test"}
    kwargs.update(overrides)
    return await create_task(db, **kwargs)


async def _claim(db, task_id: int, worker: str = "w5-worker"):
    from app.services.task_service import claim_task

    return await claim_task(db, worker=worker, task_id=task_id)


def _error_code(excinfo) -> str:
    return str(getattr(excinfo.value, "code", ""))


# ── A-W4-01: a completion never SERVES a supplied error ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_worker_writer_retires_an_error_supplied_with_a_completion(async_db_session):
    """RED: `error_message` stayed on the row, so a finished task rendered as failed.

    The product's rule (SV-W4-03) is "a completion ENDS the failure history": the text is kept in
    `metadata.last_attempt_error` and the COLUMN is cleared, because every surface paints
    `error_message`.  A supplied error must follow the same rule — retiring, never serving.
    """
    from app.services import task_service

    task = await _new_task(async_db_session)
    await async_db_session.flush()

    updated = await task_service.update_task_status(
        async_db_session,
        task.id,
        "completed",
        result_summary="the brief was written",
        error_message="E_AGENT_BAD_OUTPUT: attempt 1 produced no plan",
    )

    assert updated is not None
    assert str(updated.status) == "completed"
    assert updated.error_message is None, (
        "a completed row still serves an error_message — the completion is contradicted by its "
        f"own evidence: {updated.error_message!r}"
    )
    bag = updated.metadata_json
    retired = bag["last_attempt_error"]
    assert retired["message"] == "E_AGENT_BAD_OUTPUT: attempt 1 produced no plan"
    assert retired["retired_on_status"] == "completed"
    # The new fact this fix adds: the text was SUPPLIED BY this write, not inherited from an
    # earlier attempt — the audit must be able to tell those apart.
    assert retired["supplied_on_completion"] is True
    assert updated.result_summary == "the brief was written"


@pytest.mark.asyncio
async def test_an_inherited_error_is_still_retired_when_the_completion_supplies_none(
    async_db_session,
):
    """The shipped SV-W4-03 path keeps working (the fix must not replace it).

    Control half of the pair: a completion with NO supplied error retires the column's existing
    text — and the retired record says the text was NOT supplied by this write.
    """
    from app.services import task_service

    task = await _new_task(async_db_session)
    await _claim(async_db_session, task.id)
    await task_service.update_task_status(
        async_db_session, task.id, "failed", error_message="E_INTEGRATION: attempt 1 timed out"
    )
    await async_db_session.flush()

    updated = await task_service.update_task_status(async_db_session, task.id, "completed")
    assert updated is not None
    assert updated.error_message is None
    retired = updated.metadata_json["last_attempt_error"]
    assert retired["message"] == "E_INTEGRATION: attempt 1 timed out"
    assert retired["supplied_on_completion"] is False


@pytest.mark.asyncio
async def test_the_api_writer_refuses_an_error_supplied_with_a_completion(async_db_session):
    """RED: `PATCH /tasks/{id}/status {"status":"completed","error_message":…}` answered 200.

    The caller-facing writer REFUSES the contradiction by name (the caller can re-issue the
    request without it) and writes NOTHING — the alternative, silently dropping the text the
    caller sent, is a success-shaped no-op.  The worker's own writer cannot refuse (an exception
    there would roll back the run's ledger row); it retires instead, which is the pair above.
    """
    from app.services import task_service

    task = await _new_task(async_db_session)
    await _claim(async_db_session, task.id)
    await async_db_session.flush()

    with pytest.raises(MeridianError) as excinfo:
        await task_service.transition_task_status(
            async_db_session,
            task.id,
            "completed",
            error_message="E_AGENT_BAD_OUTPUT: a contradiction",
            actor="operator",
        )

    assert _error_code(excinfo) == E_VALIDATION
    assert "completed" in str(excinfo.value)
    assert excinfo.value.context.get("task_id") == task.id

    # Nothing was written: the row is still the claim, with no error and no completion.
    current = await task_service.get_task(async_db_session, task.id)
    assert str(current.status) == "in_progress"
    assert current.error_message is None
    assert current.result_summary is None


@pytest.mark.asyncio
async def test_an_accepted_transition_still_supplies_an_error_where_it_is_legal(async_db_session):
    """The refusal is not blanket: `failed`/`pending` writes keep writing the error as supplied."""
    from app.services import task_service

    task = await _new_task(async_db_session)
    await _claim(async_db_session, task.id)
    await async_db_session.flush()

    failed = await task_service.transition_task_status(
        async_db_session, task.id, "failed", error_message="E_INTEGRATION: nope", actor="operator"
    )
    assert failed is not None
    assert failed.error_message == "E_INTEGRATION: nope"
    assert "last_attempt_error" not in (failed.metadata_json or {})

    rearmed = await task_service.transition_task_status(
        async_db_session, task.id, "pending", actor="operator"
    )
    assert rearmed is not None and str(rearmed.status) == "pending"
    assert rearmed.error_message == "E_INTEGRATION: nope", (
        "a requeue keeps the failure text — `pending` is not a completion"
    )


# ── A-W4-03: a non-object metadata blob is never a TypeError ───────────────────────────────────


@pytest.mark.parametrize(
    ("blob", "stored_type"),
    [([None], "list"), (["a", "b"], "list"), ("just a string", "str"), (42, "int"), (0.5, "float")],
)
def test_the_bag_normaliser_never_raises_on_a_non_object_blob(blob, stored_type):
    """RED: `dict([None])` / `dict("…")` / `dict(42)` raise TypeError at every merge site."""
    from app.services.task_service import FOREIGN_METADATA_KEY, task_metadata_bag

    bag = task_metadata_bag(blob)

    assert isinstance(bag, dict)
    foreign = bag[FOREIGN_METADATA_KEY]
    assert foreign["stored_type"] == stored_type
    assert foreign["recorded_by"] == "task_metadata_bag"
    assert isinstance(foreign["value"], str) and foreign["value"]


def test_the_bag_normaliser_passes_an_object_through_as_a_copy():
    from app.services.task_service import task_metadata_bag

    source = {"a": 1, "nested": {"b": 2}}
    bag = task_metadata_bag(source)

    assert bag == source
    assert bag is not source
    bag["a"] = 99
    assert source["a"] == 1, "a merge must not mutate the ORM-loaded bag in place"
    assert task_metadata_bag(None) == {}
    assert task_metadata_bag(False)["foreign_metadata"]["stored_type"] == "bool"


def test_the_bag_normaliser_bounds_what_it_preserves():
    from app.services.task_service import (
        FOREIGN_METADATA_KEY,
        FOREIGN_METADATA_MAX_CHARS,
        task_metadata_bag,
    )

    foreign = task_metadata_bag(["x" * (FOREIGN_METADATA_MAX_CHARS * 2)])[FOREIGN_METADATA_KEY]

    assert len(foreign["value"]) == FOREIGN_METADATA_MAX_CHARS
    assert foreign["truncated"] is True


@pytest.mark.asyncio
async def test_create_task_refuses_a_non_object_metadata_by_name(async_db_session):
    """RED: `create_task(metadata=[None])` wrote the array (the unidentified live writer's door).

    The boundary asserts its own input: `metadata` is the per-task PAYLOAD channel (an object), so
    a list/string/number is the caller's mistake and is refused by name BEFORE a row exists.
    """
    from sqlalchemy import func, select

    from app.models.task import Task
    from app.services.task_service import create_task

    with pytest.raises(MeridianError) as excinfo:
        await create_task(async_db_session, title="bad", agent_type="finance", metadata=[None])

    assert _error_code(excinfo) == E_VALIDATION
    assert "metadata" in str(excinfo.value)
    assert excinfo.value.context.get("metadata_type") == "list"

    count = await async_db_session.scalar(select(func.count()).select_from(Task))
    assert count == 0, "a refused create must not have written a row"


@pytest.mark.asyncio
async def test_create_task_still_accepts_an_object_or_none(async_db_session):
    from app.services.task_service import create_task

    with_metadata = await create_task(
        async_db_session, title="ok", agent_type="finance", metadata={"inbox_item": {"id": 1}}
    )
    without = await create_task(async_db_session, title="ok too", agent_type="finance")

    assert with_metadata.metadata_json == {"inbox_item": {"id": 1}}
    assert without.metadata_json is None, (
        "the additive promise: no metadata argument still writes SQL NULL, byte-for-byte"
    )


# ── the stale attempt verdict (execution_status vs status coherence) ──────────────────────────


@pytest.mark.asyncio
async def test_a_clean_completion_retires_a_verdict_from_an_earlier_attempt(async_db_session):
    """RED: `execution_status: partial` + `needs_attention: true` survived a clean completion.

    The verdict is written per ATTEMPT but served as the ROW's state, so an attempt that recorded
    `partial`/`refused` and was then re-run (a re-park, a resume) labelled the finished row for
    ever.  The completion RETIRES it — `metadata.last_attempt_verdict`, never destroyed — exactly
    as `_retire_error_message` retires the superseded error.
    """
    from app.services import task_service

    task = await _new_task(async_db_session, metadata={"result_status": "partial", "keep": 1})
    await _claim(async_db_session, task.id)
    await async_db_session.flush()
    assert task_service.task_execution_state(task) == ("partial", True)

    updated = await task_service.update_task_status(
        async_db_session, task.id, "completed", result_summary="the resume did the work"
    )

    assert updated is not None
    bag = updated.metadata_json
    assert "result_status" not in bag, "the superseded verdict is still served as the row's state"
    assert bag["keep"] == 1, "the merge must keep another writer's keys"
    retired = bag[task_service.LAST_ATTEMPT_VERDICT_KEY]
    assert retired["status"] == "partial"
    assert retired["retired_on_status"] == "completed"
    assert task_service.task_execution_state(updated) == (None, False)


@pytest.mark.asyncio
async def test_a_completion_with_no_stale_verdict_invents_no_key(async_db_session):
    from app.services import task_service

    task = await _new_task(async_db_session, metadata={"keep": 1})
    await async_db_session.flush()

    updated = await task_service.update_task_status(async_db_session, task.id, "completed")

    assert updated is not None
    assert task_service.LAST_ATTEMPT_VERDICT_KEY not in (updated.metadata_json or {})
    assert updated.metadata_json == {"keep": 1}


@pytest.mark.asyncio
async def test_the_api_writer_retires_a_stale_verdict_too(async_db_session):
    """Both completed-writers share the rule (the `_retire_error_message` precedent)."""
    from app.services import task_service

    task = await _new_task(async_db_session, metadata={"result_status": "refused"})
    await _claim(async_db_session, task.id)
    await async_db_session.flush()

    updated = await task_service.transition_task_status(
        async_db_session, task.id, "completed", result_summary="operator closed it", actor="owner"
    )

    assert updated is not None
    assert "result_status" not in (updated.metadata_json or {})
    assert (
        updated.metadata_json[task_service.LAST_ATTEMPT_VERDICT_KEY]["status"] == "refused"
    )
    assert task_service.task_execution_state(updated) == (None, False)


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_verdict_alone(async_db_session):
    """Only a COMPLETION ends the attempt's story: a failure still needs its own verdict."""
    from app.services import task_service

    task = await _new_task(async_db_session, metadata={"result_status": "partial"})
    await _claim(async_db_session, task.id)
    await async_db_session.flush()

    failed = await task_service.transition_task_status(
        async_db_session, task.id, "failed", error_message="E_INTEGRATION: gone", actor="worker"
    )

    assert failed is not None
    assert (failed.metadata_json or {})["result_status"] == "partial"


# ── the API leg: what the owner actually reads ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_patch_route_never_serves_an_error_on_a_completed_row(
    api_client, auth_headers, async_db_session
):
    """RED (the A-W4-01 wire shape): 200 + `error_message` on a `completed` row."""
    created = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "Patched task", "agent_type": "finance"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    # 1. the contradiction is REFUSED by name, and nothing is written
    refused = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status",
        headers=auth_headers,
        json={"status": "completed", "error_message": "E_AGENT_BAD_OUTPUT: nope"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == E_VALIDATION
    unchanged = await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert unchanged.json()["status"] == "pending"
    assert unchanged.json()["error_message"] is None

    # 2. the legal completion serves a clean row
    completed = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status",
        headers=auth_headers,
        json={"status": "completed", "result_summary": "done by the owner"},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "completed"
    assert body["error_message"] is None
    assert body["result_summary"] == "done by the owner"
    assert body["execution_status"] is None and body["needs_attention"] is False

    # 3. the control: a FAILED row still serves the error it was given
    second = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "Failing task", "agent_type": "finance"},
    )
    second_id = second.json()["id"]
    failed = await api_client.patch(
        f"/api/v1/tasks/{second_id}/status",
        headers=auth_headers,
        json={"status": "failed", "error_message": "E_INTEGRATION: the provider went away"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["error_message"] == "E_INTEGRATION: the provider went away"


@pytest.mark.asyncio
async def test_the_patch_route_keeps_its_terminal_state_guards(api_client, auth_headers):
    """The A-W4-01 fix must not widen the matrix (F-17's terminal guard stays intact)."""
    created = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "Terminal", "agent_type": "finance"},
    )
    task_id = created.json()["id"]
    assert (
        await api_client.patch(
            f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "completed"}
        )
    ).status_code == 200

    reopened = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "pending"}
    )
    assert reopened.status_code == 409, reopened.text
    assert reopened.json()["error"]["code"] == E_TASK_NOT_RUNNABLE
    assert reopened.json()["error"]["context"]["accepted"] == []

    same = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "completed"}
    )
    assert same.status_code == 409
    unknown = await api_client.patch(
        f"/api/v1/tasks/{task_id}/status", headers=auth_headers, json={"status": "complete"}
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == E_VALIDATION
