"""V24 / L1 — `POST /api/v1/tasks/{task_id}/run`: a created task can actually execute.

The capability gap this closes (V24-PLAN lane 1): a manual/created task sits `pending` forever
unless an agent cycle happens to claim it or the owner clicks "Run now" — which CREATES A NEW
task row instead of running the row in front of them.

The frozen interface pinned here:
  * `POST /api/v1/tasks/{task_id}/run` -> **202** `{task_id, status: "dispatched"}`;
  * an unknown id -> 404 `E_NOT_FOUND` (the same envelope every task read answers);
  * a row whose status is not `pending` -> `E_TASK_NOT_RUNNABLE` with the CURRENT status in
    `context` (in_progress / completed / failed are all pinned below);
  * a missing key -> 401 `E_AUTH`;
  * broker refusal -> 503 `E_TASK_NOT_RUNNABLE`, the row LEFT HONEST (still `pending`, no
    claim fields, `attempts` 0, no `error_message`) — a dispatch that no broker accepted must
    never mark the row running or failed;
  * the dispatch happens OFF the event loop (`asyncio.to_thread`, the `agents.py` pattern).

RED legs measured at base 51affcf (`raw/red-base.txt`): the route did not exist, so every
request answered 404/405 and the dispatch collector stayed empty.
"""
from __future__ import annotations

import asyncio
import time

import pytest

HEADERS = {"X-API-Key": "test-key"}


def _stub_delay(monkeypatch, calls: list[int]):
    """Replace the Celery task's `.delay` with a recorder (the shipped tests' idiom)."""
    import celery_app.tasks.agent_tasks as agent_tasks

    def delay(task_id: int) -> None:
        calls.append(int(task_id))

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", delay, raising=False)
    return delay


async def _seed_task(db, *, status: str = "pending", title: str = "V24 run probe"):
    from app.models.task import Task

    task = Task(title=title, agent_type="finance", status=status, priority=2)
    db.add(task)
    await db.commit()
    return task


async def _reload(db, task_id: int):
    from sqlalchemy import select

    from app.models.task import Task

    db.expire_all()
    return (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()


# ── the happy path: 202, dispatched once, row untouched before the worker claims it ───────────


@pytest.mark.asyncio
async def test_a_pending_task_is_dispatched_with_202_and_the_row_stays_pending(
    api_client, auth_headers, async_db_session, monkeypatch
):
    task = await _seed_task(async_db_session)
    calls: list[int] = []
    _stub_delay(monkeypatch, calls)

    response = await api_client.post(f"/api/v1/tasks/{task.id}/run", headers=auth_headers)

    assert response.status_code == 202, response.text
    assert response.json() == {"task_id": task.id, "status": "dispatched"}
    assert calls == [task.id], "the route must dispatch exactly the row it was addressed"

    row = await _reload(async_db_session, task.id)
    assert row.status == "pending", "only the worker's claim may move a row to in_progress"
    assert row.claimed_by is None and row.claimed_at is None and row.heartbeat_at is None
    assert (row.attempts or 0) == 0, "the claim increments attempts; dispatch must not"
    assert row.error_message is None and row.result_summary is None


@pytest.mark.asyncio
async def test_an_unknown_task_is_the_named_404_and_nothing_is_dispatched(
    api_client, auth_headers, monkeypatch
):
    calls: list[int] = []
    _stub_delay(monkeypatch, calls)

    response = await api_client.post("/api/v1/tasks/999999/run", headers=auth_headers)

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_NOT_FOUND"
    assert body["error"]["context"]["task_id"] == 999999
    assert response.headers.get("X-Polsia-Error-Code") == "E_NOT_FOUND"
    assert calls == [], "a missing row must never reach the broker"


# ── refusals by NAME: only a pending row is runnable ─────────────────────────────────────────


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
@pytest.mark.asyncio
async def test_a_non_pending_task_is_refused_by_name_with_its_current_status(
    api_client, auth_headers, async_db_session, monkeypatch, status
):
    task = await _seed_task(async_db_session, status=status, title=f"V24 {status}")
    calls: list[int] = []
    _stub_delay(monkeypatch, calls)

    response = await api_client.post(f"/api/v1/tasks/{task.id}/run", headers=auth_headers)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_TASK_NOT_RUNNABLE"
    assert body["error"]["context"]["task_id"] == task.id
    assert body["error"]["context"]["status"] == status, (
        "the refusal must carry the CURRENT status so the caller can see why"
    )
    assert status in body["error"]["message"]
    assert response.headers.get("X-Polsia-Error-Code") == "E_TASK_NOT_RUNNABLE"
    assert calls == [], "a refused row must never reach the broker"

    row = await _reload(async_db_session, task.id)
    assert row.status == status, "a refusal leaves the row byte-identical"


@pytest.mark.asyncio
async def test_without_a_key_the_route_is_the_named_401_and_nothing_is_dispatched(
    api_client, async_db_session, monkeypatch
):
    task = await _seed_task(async_db_session, title="V24 unauthorised")
    calls: list[int] = []
    _stub_delay(monkeypatch, calls)

    response = await api_client.post(f"/api/v1/tasks/{task.id}/run")

    assert response.status_code == 401, response.text
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"
    assert response.headers.get("X-Polsia-Error-Code") == "E_AUTH"
    assert calls == []

    row = await _reload(async_db_session, task.id)
    assert row.status == "pending"


# ── broker failure: named 503, row honest (never marked running without an accept) ────────────


@pytest.mark.asyncio
async def test_a_broker_refusal_is_a_named_503_and_leaves_the_row_pending(
    api_client, auth_headers, async_db_session, monkeypatch
):
    task = await _seed_task(async_db_session, title="V24 dead broker")
    task_id = task.id

    import celery_app.tasks.agent_tasks as agent_tasks

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    response = await api_client.post(f"/api/v1/tasks/{task.id}/run", headers=auth_headers)

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_TASK_NOT_RUNNABLE"
    assert body["error"]["how_to_fix"], "a named refusal must say how to fix it"
    assert body["error"]["context"]["task_id"] == task.id
    assert body["error"]["context"]["status"] == "pending"
    assert response.headers.get("X-Polsia-Error-Code") == "E_TASK_NOT_RUNNABLE"

    # the request-teardown rollback a real `get_db` runs on the raising path
    await async_db_session.rollback()
    row = await _reload(async_db_session, task_id)
    assert row is not None, "the row must survive the refusal"
    assert row.status == "pending", "no broker accept means NOTHING was marked running/failed"
    assert row.claimed_by is None and row.claimed_at is None and row.heartbeat_at is None
    assert (row.attempts or 0) == 0
    assert row.error_message is None, "the existing row must not be rewritten by a failed dispatch"


# ── the dispatch is off the event loop (the agents.py `asyncio.to_thread` pattern) ───────────

TICK_S = 0.02
BROKER_STALL_S = 0.4


@pytest.mark.asyncio
async def test_the_dispatch_does_not_freeze_the_event_loop(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """`.delay()` is blocking; a direct call would stall every concurrent request."""
    task = await _seed_task(async_db_session, title="V24 non-blocking")

    import celery_app.tasks.agent_tasks as agent_tasks

    def stalled_delay(*_args, **_kwargs):
        time.sleep(BROKER_STALL_S)  # stands in for the retry ladder

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", stalled_delay, raising=False)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_S)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    try:
        response = await api_client.post(f"/api/v1/tasks/{task.id}/run", headers=auth_headers)
    finally:
        tick_task.cancel()

    assert response.status_code == 202, response.text
    assert ticks >= 8, (
        f"the loop only ticked {ticks}x during a {BROKER_STALL_S}s blocking delay — the dispatch "
        "was not moved off the event loop"
    )


# ── the published route: POST + 202 + a typed response model ─────────────────────────────────


def test_the_route_is_published_as_a_typed_202_post():
    from fastapi.routing import APIRoute

    from app.main import create_app

    matches = [
        route
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.path == "/api/v1/tasks/{task_id}/run"
    ]
    assert len(matches) == 1, "the run route must be published exactly once"
    route = matches[0]
    assert "POST" in route.methods
    assert route.status_code == 202
    assert route.response_model is not None, "a /api/v1 route must declare a typed response"
    assert {"task_id", "status"} <= set(route.response_model.model_fields)
