"""F-07 — the run's own verdict is served BESIDE the lifecycle status (never as `completed` alone).

Live defect (visuals, 2026-10-28): tasks 311/312/314 carried `status: completed` and a
`[partial] `-prefixed summary; the stored run said `payload_status: "partial"`.  Every badge, ledger
row and dashboard card read COMPLETED.  The lifecycle status is true — the run ended and its work is
persisted — but a partial run is NOT a clean completion, and the API had no field that said so.

The fix deliberately does NOT add a value to `tasks.status` (frozen CHECK constraint
`ck_tasks_status` + the frontend's own 7-value status matrix): the worker stamps the verdict into
`tasks.metadata.result_status`, `task_service.task_execution_state` derives the served status, and
`TaskResponse` carries `execution_status` / `needs_attention` as ADDITIVE fields.  The badge is the
frontend's half (weaver).  The WORKER half (stamp + feed row) lives in
`tests/unit/celery/test_w2_f07_partial_verdict_worker.py`.

RED leg for the API test: make `task_execution_state` return `(None, False)` — the response then
says `status: completed, execution_status: null, needs_attention: false` for a partial row, which is
exactly the live lie.
"""
from __future__ import annotations

import pytest


def _task_row(metadata: dict | None = None, summary: str | None = None):
    """A detached `Task` shape for the pure derivation (no session needed)."""
    from app.models.task import Task, TaskStatus

    return Task(
        id=1,
        title="partial specimen",
        agent_type="competitor_research",
        status=TaskStatus.COMPLETED,
        result_summary=summary,
        metadata_json=metadata,
    )


# ── the derivation ──────────────────────────────────────────────────────────────────────────
def test_a_recorded_partial_verdict_is_served_as_partial_and_needs_attention():
    from app.services.task_service import task_execution_state

    task = _task_row(metadata={"result_status": "partial"}, summary="three of four sections")

    status, needs_attention = task_execution_state(task)

    assert status == "partial"
    assert needs_attention is True


def test_a_legacy_partial_summary_is_read_as_partial_for_rows_written_before_the_marker():
    """Tasks 311/312/314 carry the machine-written prefix but no metadata marker yet."""
    from app.services.task_service import task_execution_state

    task = _task_row(summary="[partial] Decision memo written; recommendation unverified")

    assert task_execution_state(task) == ("partial", True)


def test_a_clean_completion_is_not_flagged():
    from app.services.task_service import task_execution_state

    task = _task_row(summary="Decision memo written to notes/312.md")

    assert task_execution_state(task) == (None, False)


def test_a_refused_verdict_is_forwarded_and_needs_attention():
    from app.services.task_service import task_execution_state

    task = _task_row(metadata={"result_status": "refused"}, summary="[refused] no honest path")

    assert task_execution_state(task) == ("refused", True)


def test_prose_is_never_scanned_for_a_verdict():
    """No keyword heuristic: a summary that merely says "partial" is not a verdict."""
    from app.services.task_service import task_execution_state

    task = _task_row(summary="The market is partially served; report complete.")

    assert task_execution_state(task) == (None, False)


# ── the wire ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_tasks_api_serves_the_partial_verdict_beside_the_lifecycle_status(
    api_client, auth_headers, async_db_session
):
    from app.models.task import Task
    from app.services.task_service import create_task, update_task_status

    task = await create_task(
        async_db_session,
        title="partial wire specimen",
        agent_type="competitor_research",
    )
    await update_task_status(
        async_db_session,
        task.id,
        "completed",
        result_summary="[partial] two of three suppliers verified",
    )
    row = await async_db_session.get(Task, task.id)
    row.metadata_json = {"result_status": "partial"}
    await async_db_session.commit()

    response = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed", "the lifecycle status keeps its frozen meaning"
    assert body["execution_status"] == "partial"
    assert body["needs_attention"] is True

    listed = await api_client.get("/api/v1/tasks", headers=auth_headers)
    assert listed.status_code == 200
    entry = next(item for item in listed.json() if item["id"] == task.id)
    assert entry["execution_status"] == "partial"
    assert entry["needs_attention"] is True


@pytest.mark.asyncio
async def test_a_clean_task_serves_null_execution_status(api_client, auth_headers,
                                                         async_db_session):
    from app.services.task_service import create_task, update_task_status

    task = await create_task(
        async_db_session, title="clean wire specimen", agent_type="finance"
    )
    await update_task_status(async_db_session, task.id, "completed", result_summary="done")
    await async_db_session.commit()

    response = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] is None
    assert body["needs_attention"] is False
