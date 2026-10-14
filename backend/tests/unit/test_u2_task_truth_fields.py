"""Wave U2 (B1) — TaskResponse truth fields: completed_at / cost_usd / tokens_in / tokens_out.

The frozen contract: four ADDITIVE nullable fields on `TaskResponse` (default None, no model or
DDL change), filled wherever TaskResponse is constructed from real rows.

RECON of the construction sites (`grep -rn "TaskResponse" backend/app`) — all four live in
`app/api/v1/tasks.py` and all four are exercised here so none can silently skip the fill:

    1. `list_tasks_endpoint`  -> test_list_serves_the_ledger_sums_for_each_task
    2. `create_task_endpoint` -> test_a_freshly_created_task_serves_the_four_nullable_fields
    3. `get_task_endpoint`    -> test_detail_serves_the_ledger_sums (and the null legs)
    4. `update_task_status_endpoint` (PATCH) -> test_patch_serves_the_ledger_sums_and_the_completion_time

The row that would betray a skipped fill is the one WITH runs: a defined-but-never-called field
serves null there.  That is the RED leg this file was written against — reverting the list site to
a bare `TaskResponse.model_validate(task)` makes the list test fail with `None == 2.0` while every
null-discipline test stays green (null and never-filled look identical without runs behind them —
which is exactly why the sums leg cannot be omitted).

NULL discipline (the contract's second half): "unmeasured is never zero".
  * no runs          -> cost/tokens are NULL (never 0);
  * runs but no value ever recorded -> NULL;
  * a MEASURED zero (runs exist, cost 0) -> 0.0, served as the real number it is.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.task_service import create_agent_run

TRUTH_FIELDS = ("completed_at", "cost_usd", "tokens_in", "tokens_out")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _task_with_runs(session, *, title: str = "ran twice"):
    """A pending task plus two runs: 1.25 + 0.75 USD, 15 in / 27 out tokens."""
    from app.services.task_service import create_task

    task = await create_task(session, title=title, agent_type="finance")
    await create_agent_run(
        session, agent_type="finance", task_id=task.id,
        tokens_input=10, tokens_output=20, cost_usd=Decimal("1.25"),
    )
    await create_agent_run(
        session, agent_type="finance", task_id=task.id,
        tokens_input=5, tokens_output=7, cost_usd=Decimal("0.75"),
    )
    await session.commit()
    return task


# ── the null legs: a task with no runs ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_freshly_created_task_serves_the_four_nullable_fields(api_client, auth_headers):
    """Construction site 2 (POST): the fields are present and null — not 500, not omitted.

    A just-created task cannot have runs yet (runs are keyed by the id this call mints), so null
    is the measured answer here, and the create site must still run the fill rather than skip it.
    """
    resp = await api_client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={"title": "brand new", "agent_type": "finance"},
    )
    assert resp.status_code == 201, resp.text[:300]
    data = resp.json()
    for field in TRUTH_FIELDS:
        assert field in data, f"the additive field {field!r} must be on the wire"
        assert data[field] is None
    # existing fields are untouched by the additive change
    assert data["title"] == "brand new" and data["status"] == "pending"


@pytest.mark.asyncio
async def test_cost_is_null_for_a_task_with_no_runs(api_client, auth_headers, async_db_session):
    """Construction site 3 (GET): no runs -> NULL sums, i.e. unmeasured is never zero."""
    from app.services.task_service import create_task

    task = await create_task(async_db_session, title="never ran", agent_type="finance")
    await async_db_session.commit()

    resp = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert data["cost_usd"] is None
    assert data["tokens_in"] is None and data["tokens_out"] is None
    assert data["completed_at"] is None


@pytest.mark.asyncio
async def test_runs_with_no_recorded_values_leave_the_sums_null(
    api_client, auth_headers, async_db_session
):
    """A run row that recorded nothing is NOT a zero-cost run — SUM over NULLs is NULL."""
    from app.services.task_service import create_task

    task = await create_task(async_db_session, title="ran blind", agent_type="finance")
    await create_agent_run(async_db_session, agent_type="finance", task_id=task.id)
    await async_db_session.commit()

    resp = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)
    data = resp.json()
    assert data["cost_usd"] is None, "a run with no recorded cost is unmeasured, not free"
    assert data["tokens_in"] is None and data["tokens_out"] is None


@pytest.mark.asyncio
async def test_a_measured_zero_is_served_as_zero_not_null(
    api_client, auth_headers, async_db_session
):
    """The other side of the rule: 0 recorded is a REAL value and must not collapse to null."""
    from app.services.task_service import create_task

    task = await create_task(async_db_session, title="recorded zero", agent_type="finance")
    await create_agent_run(
        async_db_session, agent_type="finance", task_id=task.id,
        tokens_input=0, tokens_output=0, cost_usd=Decimal("0.0000"),
    )
    await async_db_session.commit()

    resp = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)
    data = resp.json()
    assert data["cost_usd"] == 0.0 and data["cost_usd"] is not None
    assert data["tokens_in"] == 0 and data["tokens_out"] == 0


# ── the sums legs: a task WITH runs (the skipped-fill detector) ───────────────────────────────


@pytest.mark.asyncio
async def test_detail_serves_the_ledger_sums(api_client, auth_headers, async_db_session):
    """Construction site 3 (GET): cost = 1.25 + 0.75, tokens = 10+5 in, 20+7 out."""
    task = await _task_with_runs(async_db_session)

    resp = await api_client.get(f"/api/v1/tasks/{task.id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert data["cost_usd"] == pytest.approx(2.0)
    assert data["tokens_in"] == 15
    assert data["tokens_out"] == 27
    # a pending task did not complete — the field stays null even though its runs measured money
    assert data["completed_at"] is None


@pytest.mark.asyncio
async def test_list_serves_the_ledger_sums_for_each_task(api_client, auth_headers, async_db_session):
    """Construction site 1 (GET /tasks): the page carries each task's own sums, in ONE grouped read."""
    from app.services.task_service import create_task

    with_runs = await _task_with_runs(async_db_session, title="has runs")
    without_runs = await create_task(async_db_session, title="no runs", agent_type="finance")
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/tasks", headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    rows = {row["id"]: row for row in resp.json()}
    assert rows[with_runs.id]["cost_usd"] == pytest.approx(2.0)
    assert rows[with_runs.id]["tokens_in"] == 15 and rows[with_runs.id]["tokens_out"] == 27
    assert rows[without_runs.id]["cost_usd"] is None, "the sibling's runs must not leak onto this row"
    assert rows[without_runs.id]["tokens_in"] is None


@pytest.mark.asyncio
async def test_patch_serves_the_ledger_sums_and_the_completion_time(
    api_client, auth_headers, async_db_session
):
    """Construction site 4 (PATCH): the write response is filled like every read — the patch site
    is the one the UI sees right after it acts, so an unfilled response here would null the sums
    on exactly the row the user just changed."""
    task = await _task_with_runs(async_db_session)

    resp = await api_client.patch(
        f"/api/v1/tasks/{task.id}/status",
        headers=auth_headers,
        json={"status": "completed", "result_summary": "done"},
    )
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert data["status"] == "completed"
    assert data["cost_usd"] == pytest.approx(2.0)
    assert data["tokens_in"] == 15 and data["tokens_out"] == 27
    # completed by THAT write (update_task_status stamps updated_at) — completed_at is that instant
    assert data["completed_at"] is not None
    assert _naive_utc(datetime.fromisoformat(data["completed_at"])) == _naive_utc(task.updated_at)


@pytest.mark.asyncio
async def test_completed_at_is_filled_only_for_completed_tasks(
    api_client, auth_headers, async_db_session
):
    """`completed_at` is the completion timestamp — a failed task did not complete, so it stays
    null rather than carrying a renamed `updated_at`."""
    from app.services.task_service import create_task, update_task_status

    completed = await create_task(async_db_session, title="completed", agent_type="finance")
    failed = await create_task(async_db_session, title="failed", agent_type="finance")
    await update_task_status(async_db_session, completed.id, "completed")
    await update_task_status(async_db_session, failed.id, "failed", error_message="boom")
    await async_db_session.commit()

    been = await api_client.get(f"/api/v1/tasks/{completed.id}", headers=auth_headers)
    bleh = await api_client.get(f"/api/v1/tasks/{failed.id}", headers=auth_headers)
    assert been.json()["completed_at"] is not None
    assert _naive_utc(datetime.fromisoformat(been.json()["completed_at"])) == _naive_utc(
        completed.updated_at
    )
    assert bleh.json()["completed_at"] is None
