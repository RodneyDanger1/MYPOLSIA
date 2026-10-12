"""POST /agents/{agent_type}/trigger must not freeze the event loop while the broker is unavailable.

`.delay()` is a BLOCKING call — against the Redis broker it walks redis-py's connection retry ladder —
and it used to be called straight from the async handler. One "Run Now" click therefore stalled
uvicorn's loop, and the repo's own e2e run measured the consequence: `GET /api/v1/config` timed out at
20.0 s while the backend log filled with `celery.backends.redis: Connection to Redis lost: Retry (n/20)`.

`asyncio.to_thread` does not make the response FASTER (the handler still awaits the call); it makes the
loop RESPONSIVE. So the test measures ticks of a concurrently-scheduled coroutine, not elapsed time.

Row creation is stubbed on purpose: the subject here is the broker call's treatment, and the DB write is
covered by the task-service tests. (A variant that let the handler write to the fixture DB hit
`no such table: tasks` once the Celery task module was imported inside the test body — a fixture-ordering
interaction this test does not depend on, recorded rather than chased.)

RED legs, both observed: revert the handler to a bare `run_agent_task.delay(task.id)`, or monkeypatch
`asyncio.to_thread` to call its target inline — either way the ticker observes **0** ticks and the
`ticks >= 5` assertion fails, so the check can fail.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

TICK_S = 0.05
BROKER_STALL_S = 0.75


@pytest.mark.asyncio
async def test_a_stalled_broker_does_not_freeze_the_event_loop(api_client, auth_headers, monkeypatch):
    import celery_app.tasks.agent_tasks as agent_tasks
    from app.api.v1 import agents as agents_module

    async def fake_create_task(*_args, **_kwargs):
        return SimpleNamespace(id=4242)

    def stalled_delay(*_args, **_kwargs):
        time.sleep(BROKER_STALL_S)  # stands in for the retry ladder against a dead broker

    monkeypatch.setattr(agents_module, "create_task", fake_create_task, raising=False)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", stalled_delay, raising=False)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_S)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        response = await api_client.post("/api/v1/agents/social_media/trigger", headers=auth_headers)
    finally:
        task.cancel()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued", (
        "this stub only SLEEPS: a slow-but-working broker really did accept the delivery, so "
        "202/'queued' is the honest answer here. The dead-broker case is the next test."
    )
    assert body["task_id"] == 4242
    # 0.75s / 0.05s == ~15 ticks if the loop stayed free; a blocked loop manages 0 or 1.
    assert ticks >= 5, (
        f"the event loop only ticked {ticks}x during a {BROKER_STALL_S}s broker stall — it was blocked "
        "by a synchronous .delay() call"
    )


@pytest.mark.asyncio
async def test_a_broker_outage_is_a_named_refusal_and_the_row_is_not_left_looking_queued(
    api_client, auth_headers, monkeypatch
):
    """When `.delay()` RAISES, the old 202/"queued" was a fake success.

    Nothing accepted the delivery: `claim_task`'s only caller is broker delivery, and no reconciler
    picks up a row that was never enqueued — so a row left `pending` would sit in the ledger looking
    queued forever while the caller waited for work no worker knew about. The measured behaviour now:
    HTTP 503, the refusal envelope with `E_REDIS` and its `how_to_fix`, and the row itself carries the
    terminal state and the reason.

    RED leg (observed): restore the swallow-and-return-202 branch and this test fails on
    `assert response.status_code == 503` — the response is 202 with `status:"queued"`.
    """
    import celery_app.tasks.agent_tasks as agent_tasks
    from app.api.v1 import agents as agents_module

    async def fake_create_task(*_args, **_kwargs):
        return SimpleNamespace(id=4242)

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    recorded: list[tuple] = []

    async def fake_update_task_status(db, task_id, new_status, **kwargs):
        recorded.append((task_id, new_status, kwargs.get("error_message", "")))
        return None

    monkeypatch.setattr(agents_module, "create_task", fake_create_task, raising=False)
    monkeypatch.setattr(agents_module, "update_task_status", fake_update_task_status, raising=False)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    response = await api_client.post("/api/v1/agents/social_media/trigger", headers=auth_headers)

    assert response.status_code == 503, "a broker outage is a 503, not a 202 that will never come true"
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_REDIS", "the failure must name itself"
    assert body["error"]["how_to_fix"], "a named refusal must say how to fix it"
    assert body["error"]["context"]["task_id"] == 4242

    assert recorded, "the row must record why it will never run"
    task_id, new_status, error_message = recorded[0]
    assert (task_id, new_status) == (4242, "failed")
    assert "E_REDIS" in error_message


@pytest.mark.asyncio
async def test_the_trigger_reports_queued_with_the_created_task_id(api_client, auth_headers, monkeypatch):
    """The happy path, unpinned from any broker behaviour: 202 + the created row's id."""
    import celery_app.tasks.agent_tasks as agent_tasks
    from app.api.v1 import agents as agents_module

    async def fake_create_task(*_args, **kwargs):
        assert kwargs.get("agent_type") == "social_media"
        assert kwargs.get("source") == "manual"
        return SimpleNamespace(id=99)

    monkeypatch.setattr(agents_module, "create_task", fake_create_task, raising=False)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", lambda *a, **k: None, raising=False)

    response = await api_client.post(
        "/api/v1/agents/social_media/trigger",
        headers=auth_headers,
        json={"task_title": "unit-test trigger"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"] == 99
    assert "99" in body["message"]
