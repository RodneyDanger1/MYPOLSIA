"""A-W4-02 (S3, wave 4 -> wave 5) — the RETIRED failure history must be READABLE, not write-only.

SV-W4-03 (wave 4) stopped a completed task from serving a superseded attempt's `error_message` by
MOVING the text into `tasks.metadata.last_attempt_error` (the column is nulled).  The wave-4 audit
then found the preservation was WRITE-ONLY: `TaskResponse` served neither `metadata` nor
`attempts`, so nothing on any wire could read the history the fix claimed to keep — and the raw
bag cannot be served wholesale because `metadata.approval_token` is the operator's credential.

This file pins the minimal read shape that closes it:

  * `TaskResponse.last_attempt_error` — typed (`TaskLastAttemptError`), bounded, and unmistakably
    PAST TENSE (`retired_on_status` names the transition that ended the failure);
  * `TaskResponse.attempts` — the live execution counter (a real column, no second source);
  * `error_message` stays null on a completed row whose failure was retired — the record is never
    served as a CURRENT error;
  * absence/junk reads as null with a log line (the L1-11 rule: one bad JSON bag value must not
    500 the task list), and the bag + the approval token stay off the wire.

COORDINATION (A-W4-01): the WRITER half — a completion that SUPPLIES an `error_message` must not
serve it either — is `logic-debugger`'s W5 fix in `task_service`.  This read shape deliberately
does NOT mask a column error left by that writer: fabricating a retirement on the read side would
hide the write defect instead of fixing it (see the last test).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.task import Task
from app.schemas.task import LAST_ATTEMPT_ERROR_KEY, LAST_ATTEMPT_ERROR_WIRE_CHARS, TaskResponse


def _retired(**overrides) -> dict:
    record = {
        "message": "PolsiaError: E_AGENT_BAD_OUTPUT: the reply exceeded 280 chars",
        "attempts": 2,
        "retired_at": "2026-10-29T00:51:57.351322+00:00",
        "retired_by": "update_task_status",
        "retired_on_status": "completed",
    }
    record.update(overrides)
    return record


async def _add_task(session, *, status: str = "completed", metadata: dict | None = None,
                    error_message: str | None = None) -> int:
    row = Task(
        title="A-W4-02 read shape",
        agent_type="social_media",
        status=status,
        source="test",
        attempts=3,
        error_message=error_message,
        metadata_json=metadata,
    )
    session.add(row)
    await session.commit()
    return int(row.id)


@pytest.mark.asyncio
async def test_the_task_detail_serves_the_retired_failure_as_history(
    async_db_session, api_client, auth_headers
):
    task_id = await _add_task(
        async_db_session,
        metadata={LAST_ATTEMPT_ERROR_KEY: _retired(), "approval_token": "w5-secret-token"},
    )

    response = await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    record = body["last_attempt_error"]
    assert record["message"].startswith("PolsiaError: E_AGENT_BAD_OUTPUT"), record
    assert record["attempts"] == 2, record
    assert record["retired_on_status"] == "completed", record
    assert record["retired_by"] == "update_task_status", record
    assert record["retired_at"].startswith("2026-10-29T00:51:57"), record
    assert record["truncated"] is False and record["message_chars"] == len(record["message"])

    # The live counter is served from the real column (no second source).
    assert body["attempts"] == 3, body

    # The RETIRED failure is history, never the current error.
    assert body["error_message"] is None, body["error_message"]
    # The bag and the credential inside it stay off the wire (S7-R1 / A5-02 intact).
    assert "metadata" not in body and "metadata_json" not in body, sorted(body)
    assert "w5-secret-token" not in response.text, "the approval token leaked onto the task wire"


@pytest.mark.asyncio
async def test_the_task_list_serves_the_same_shape(async_db_session, api_client, auth_headers):
    """All four construction sites share `TaskResponse`; the list must agree with the detail."""
    await _add_task(async_db_session, metadata={LAST_ATTEMPT_ERROR_KEY: _retired()})

    response = await api_client.get("/api/v1/tasks", headers=auth_headers)

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1, rows
    assert rows[0]["last_attempt_error"]["retired_on_status"] == "completed"
    assert rows[0]["attempts"] == 3


@pytest.mark.asyncio
async def test_a_task_with_no_retired_failure_serves_none(async_db_session, api_client, auth_headers):
    """Absence is an answer: no key, no record invented (and the live error field is null)."""
    task_id = await _add_task(async_db_session, metadata={"plan_day": "2026-10-29"})

    response = await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)

    body = response.json()
    assert body["last_attempt_error"] is None, body["last_attempt_error"]
    assert body["attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bag_value",
    [
        [None],                                  # A-W4-03's observed array shape
        "a string, not a record",
        {},                                      # a mapping with no message at all
        {"message": ""},
        {"message": None},
        {"message": 42},
    ],
)
async def test_a_malformed_retired_record_reads_as_null_not_a_500(
    async_db_session, api_client, auth_headers, bag_value
):
    """One bad JSON value must not take down the whole task list (L1-11 / the A-W4-03 class)."""
    task_id = await _add_task(async_db_session, metadata={LAST_ATTEMPT_ERROR_KEY: bag_value})

    response = await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["last_attempt_error"] is None


@pytest.mark.asyncio
async def test_the_served_message_is_bounded_with_a_stated_cut(
    async_db_session, api_client, auth_headers
):
    """`limit=100` pages must not carry unbounded failure history; the cut is MEASURABLE."""
    stored = "E_LONG: " + ("x" * (LAST_ATTEMPT_ERROR_WIRE_CHARS + 500))
    task_id = await _add_task(
        async_db_session, metadata={LAST_ATTEMPT_ERROR_KEY: _retired(message=stored)}
    )

    body = (await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)).json()

    record = body["last_attempt_error"]
    assert record["truncated"] is True, record
    assert record["message_chars"] == len(stored), record
    assert record["message"].endswith(f"[read cut at {LAST_ATTEMPT_ERROR_WIRE_CHARS} chars]"), record
    assert len(record["message"]) <= LAST_ATTEMPT_ERROR_WIRE_CHARS + 64


@pytest.mark.asyncio
async def test_an_error_the_writer_left_on_the_column_is_not_papered_over(
    async_db_session, api_client, auth_headers
):
    """COORDINATION LEG (A-W4-01): the read does not invent a retirement for a column error.

    A completion that SUPPLIES an `error_message` still writes it today (logic-debugger's W5 fix is
    the writer half).  When that happens the wire serves the column as `error_message` and NO
    retired record — the contradiction stays visible instead of being silently "retired" by the
    read model, which would hide the write defect rather than fix it.
    """
    task_id = await _add_task(
        async_db_session, status="completed", error_message="E_SUPPLIED: a completion carried this"
    )

    body = (await api_client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)).json()

    assert body["error_message"] == "E_SUPPLIED: a completion carried this", body["error_message"]
    assert body["last_attempt_error"] is None, "the read must not fabricate a retirement"


def test_the_schema_literal_agrees_with_the_producer_constant():
    """Cross-check: a renamed bag key on the writer side reddens THIS test, not a silent null."""
    from app.services import task_service

    assert LAST_ATTEMPT_ERROR_KEY == task_service.LAST_ATTEMPT_ERROR_KEY


@pytest.mark.asyncio
async def test_the_retired_record_survives_a_deeply_nested_bag(async_db_session):
    """The schema reads its ONE key and ignores everything else (plan/approval keys untouched)."""
    bag = {
        "plan_day": "2026-10-29",
        "approval_required": [{"fingerprint": "abc", "action": "send"}],
        LAST_ATTEMPT_ERROR_KEY: _retired(),
        "upstream_results": [{"task_id": 5, "title": "up", "status": "completed"}],
    }
    row = (
        await async_db_session.execute(select(Task).limit(0))
    )  # keep the session warm for the assertion below
    assert row is not None
    task_id = await _add_task(async_db_session, metadata=bag)

    task = (
        await async_db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    served = TaskResponse.model_validate(task)

    assert served.last_attempt_error is not None
    assert served.last_attempt_error.retired_on_status == "completed"
    assert served.approval_fingerprints == ["abc"]
    assert served.plan is not None and served.plan["plan_day"] == "2026-10-29"
    assert [item.task_id for item in served.upstream_results] == [5]
    assert json.dumps(served.model_dump(), default=str)  # the model is serialisable
