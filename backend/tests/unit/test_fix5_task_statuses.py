"""FIXER-5 / L1-09 + L2-02 — `tasks.status` is asserted by every code path that writes
it, and the value set reconciles with the SHIPPED `ck_tasks_status` CHECK.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l2l3.txt):
`update_task_status(db, id, "definitely_not_a_status")` stored the bogus value on SQLite
(no CHECK there — every unit test), `create_task(status=...)` wrote anything, and
`GET /api/v1/tasks?status=complete` answered `200 []` while the sibling support route
refused the same typo with `E_VALIDATION`; `VALID_TASK_STATUSES` was read by nothing and
omitted `awaiting_approval`, which the DDL allows.

The F2 signature pin (`tests/unit/celery/test_service_contract.py`) is deliberately NOT
touched by this fix: the refusals are raises, not new parameters.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.services.task_service import (
    VALID_TASK_STATUSES,
    create_task,
    get_task,
    list_tasks,
    update_task_status,
)

BACKEND = Path(__file__).resolve().parents[2]
DDL_0002 = BACKEND / "alembic" / "versions" / "0002_queue_and_perf.py"


def _ck_tasks_status_values() -> set[str]:
    """The SHIPPED CHECK's value set, parsed from 0002's own DDL."""
    text = DDL_0002.read_text(encoding="utf-8")
    match = re.search(r'"ck_tasks_status",\s*"tasks",\s*"status IN \(([^)]+)\)"', text)
    assert match is not None, "ck_tasks_status not found in 0002's CHECK_CONSTRAINTS"
    return {part.strip().strip("'") for part in match.group(1).split(",")}


async def _make_task(db, title: str = "fix5 task", **kwargs):
    task = await create_task(db, title=title, agent_type="finance", **kwargs)
    await db.flush()
    return task


# ── the value set is real: it reconciles with the shipped DDL ─────────────────────────────────


def test_valid_task_statuses_reconciles_exactly_with_the_shipped_check():
    """The tuple and `ck_tasks_status` are one contract; drift in either fails HERE."""
    assert set(VALID_TASK_STATUSES) == _ck_tasks_status_values()
    assert "awaiting_approval" in VALID_TASK_STATUSES
    assert len(VALID_TASK_STATUSES) == len(set(VALID_TASK_STATUSES)), "no duplicates"


# ── the writer asserts before mutating ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_task_status_refuses_an_unknown_value_and_leaves_the_row_unchanged(
    async_db_session,
):
    task = await _make_task(async_db_session)
    with pytest.raises(PolsiaError) as excinfo:
        await update_task_status(async_db_session, task.id, "definitely_not_a_status")

    refusal = excinfo.value
    assert refusal.code == E_VALIDATION
    assert "definitely_not_a_status" in refusal.message
    assert set(refusal.context["valid"]) == set(VALID_TASK_STATUSES)

    fresh = await get_task(async_db_session, task.id)
    assert fresh.status == "pending"  # the refusal fired BEFORE the assignment


@pytest.mark.asyncio
async def test_update_task_status_still_accepts_every_value_in_the_set(async_db_session):
    task = await _make_task(async_db_session)
    for status in VALID_TASK_STATUSES:
        updated = await update_task_status(async_db_session, task.id, status)
        assert updated.status == status


@pytest.mark.asyncio
async def test_create_task_refuses_an_unknown_status(async_db_session):
    with pytest.raises(PolsiaError) as excinfo:
        await create_task(
            async_db_session, title="bogus", agent_type="finance", status="not_a_status"
        )
    assert excinfo.value.code == E_VALIDATION


@pytest.mark.asyncio
async def test_create_task_accepts_awaiting_approval(async_db_session):
    task = await _make_task(async_db_session, title="awaiting", status="awaiting_approval")
    assert task.status == "awaiting_approval"


# ── the read refuses the typo instead of returning [] ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_refuses_an_unknown_status(async_db_session):
    """`?status=complete` must not be indistinguishable from "no completed tasks"."""
    with pytest.raises(PolsiaError) as excinfo:
        await list_tasks(async_db_session, status="complete")
    refusal = excinfo.value
    assert refusal.code == E_VALIDATION
    assert "complete" in refusal.message
    assert set(refusal.context["valid"]) == set(VALID_TASK_STATUSES)


@pytest.mark.asyncio
async def test_list_tasks_still_filters_by_every_value_in_the_set(async_db_session):
    pending_task = await _make_task(async_db_session, title="pending one")
    assert [t.id for t in await list_tasks(async_db_session, status="pending")] == [pending_task.id]
    assert await list_tasks(async_db_session, status="awaiting_approval") == []


# ── the wire: the route refusal through the real ASGI stack ───────────────────────────────────


@pytest.mark.asyncio
async def test_route_refuses_unknown_status_with_e_validation(api_client, auth_headers):
    resp = await api_client.get("/api/v1/tasks?status=complete", headers=auth_headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_VALIDATION
    assert "complete" in body["error"]["message"]
    assert resp.headers.get("X-Polsia-Error-Code") == E_VALIDATION


@pytest.mark.asyncio
async def test_route_still_serves_a_known_status(api_client, auth_headers, async_db_session):
    task = await _make_task(async_db_session, title="visible")
    resp = await api_client.get("/api/v1/tasks?status=pending", headers=auth_headers)
    assert resp.status_code == 200
    assert [row["id"] for row in resp.json()] == [task.id]
