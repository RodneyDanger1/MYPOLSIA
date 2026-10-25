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

T19 adds the two proofs the wave-4 receipt left open: the RE-ARMED row survives the
request-session rollback (`test_a_dead_broker_leaves_the_rearmed_row_committed_not_rolled_back`)
and the refusal is BOUNDED in wall time against the real transport configuration
(`test_the_dead_broker_ladder_elapses_bounded` + `test_the_broker_transport_is_bounded_by_configuration`).
Those two measure real seconds on purpose — the bound is the subject. Measured at T19: 12.758 s on
the uvicorn path (W4 scratch), 12.981 s through this test's own API path, 6.29–6.61 s for a bare
in-process `delay()` (probe) — the delta is the result-backend consumer's own bounded ladder. The
bound is the RETRY CAP, measured: widening the socket timeouts alone left the refused-port ladder
unchanged; defeating the retry cap (`max_retries` 1 -> 20, step 1.0 s) measured **129.5 s** through
the API path and the 20 s ceiling died — that is what the ceiling exists to catch.
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
    """When `.delay()` RAISES, the old 202/"queued" was a fake success — and so was `failed`.

    PIN REPAIRED (wave B1 / seat C1, D6-R2).  This test used to assert that the handler wrote the
    row `failed` through `update_task_status` (recorded == (4242, "failed"), error_message carrying
    E_REDIS) on the strength of "NO delivery was accepted".  A publish refusal cannot know that
    (A2-03: a connection that dies mid-publish leaves a live message behind, and `claim_task`
    accepts only `pending` rows), so the shipped contract is now: the row STAYS `pending` and is
    stamped with `metadata.rearmed_at` (the marker `maintenance._reconcile_stranded_pending`
    re-dispatches on), `error_message` keeps the refusal's name as the diagnostic, and the caller
    still gets the loud named refusal — which now says accepted-but-not-dispatched.  The response
    half of this test is unchanged; the row half is STRICTER (pending + marker instead of a
    terminal guess).

    RED leg (observed, wave B1/C1): revert `agents.py`'s except branch to `update_task_status(...,
    "failed", ...)` and this test fails on `assert rearmed == [((4242,), "enqueue_refused:ConnectionError")]`
    — the branch no longer stamps anything.
    """
    import celery_app.tasks._common as common_module
    import celery_app.tasks.agent_tasks as agent_tasks
    from app.api.v1 import agents as agents_module

    async def fake_create_task(*_args, **_kwargs):
        return SimpleNamespace(id=4242)

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    rearmed: list[tuple] = []

    async def fake_stamp_rearmed(db, task_ids, *, reason):
        rearmed.append((tuple(task_ids), reason))
        return list(task_ids)

    monkeypatch.setattr(agents_module, "create_task", fake_create_task, raising=False)
    # The handler imports `stamp_rearmed` lazily from its module, so the module attribute is what
    # the call resolves — patching it here intercepts the re-arm exactly where it happens.
    monkeypatch.setattr(common_module, "stamp_rearmed", fake_stamp_rearmed, raising=False)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    response = await api_client.post("/api/v1/agents/social_media/trigger", headers=auth_headers)

    assert response.status_code == 503, "a broker outage is a 503, not a 202 that will never come true"
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_REDIS", "the failure must name itself"
    assert body["error"]["how_to_fix"], "a named refusal must say how to fix it"
    assert body["error"]["context"]["task_id"] == 4242
    # ... and it says the honest thing: accepted (created, still queued), not dispatched.
    assert body["error"]["context"]["dispatched"] is False
    assert body["error"]["context"]["status"] == "pending"

    assert rearmed == [((4242,), "enqueue_refused:ConnectionError")], (
        "the refused delivery must RE-ARM the row, not terminalise it"
    )


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


# ── T19: the terminal row, committed — and the refusal, bounded against the real transport ─────


@pytest.mark.asyncio
async def test_a_dead_broker_leaves_the_rearmed_row_committed_not_rolled_back(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The response names a task id — after the request teardown, that row must still exist.

    PIN REPAIRED (wave B1 / seat C1, D6-R2).  Renamed from
    `test_a_dead_broker_leaves_the_failed_row_committed_not_rolled_back`: it used to assert the
    committed row was `failed` ("the row's terminal state is the reason it will never run") — the
    very claim a publish refusal cannot make.  The row is now committed `pending` WITH
    `metadata.rearmed_at` and the refusal in `error_message`, and it is STILL RUNNABLE by a second
    attempt (asserted below through the worker's own claim), which is strictly more than "the id
    exists": the work is owed, provably, and bounded by the reconciler's window.

    `get_db` rolls the request session back when the handler raises (its docstring; measured
    behavior). The handler only FLUSHES the created row and its marker, so without the explicit
    commit the id the 503 hands the caller points at nothing. Measured before the commit
    (W4 scratch DB, re-read by W2): rows 1..15 exist and row 16 — the one the 503 named — does not.
    The fixture's dependency override skips `get_db`, so this test applies the teardown rollback
    by hand: a missing commit is discarded here and fails the test; a present one survives it.

    OLD assertion: `assert row.status == "failed"` + `"E_REDIS" in row.error_message`.
    NEW assertion: `row.status == "pending"`, `metadata.rearmed_at` present with
    `rearm_reason == "enqueue_refused:ConnectionError"`, `"E_REDIS" in row.error_message`, and
    `claim_task(...)` still claims the row.
    """
    from sqlalchemy import select

    import celery_app.tasks.agent_tasks as agent_tasks
    from app.models.task import Task
    from app.services.task_service import claim_task

    def dead_delay(*_args, **_kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", dead_delay, raising=False)

    response = await api_client.post(
        "/api/v1/agents/social_media/trigger",
        headers=auth_headers,
        json={"task_title": "T19 persistence probe"},
    )
    assert response.status_code == 503
    task_id = response.json()["error"]["context"]["task_id"]

    # the request-teardown rollback a real `get_db` runs on the raising path
    await async_db_session.rollback()

    row = (
        await async_db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one_or_none()
    assert row is not None, "the id the 503 named must not point at a rolled-back ghost row"
    assert row.status == "pending", (
        "a refused .delay() cannot prove nothing was delivered: the row stays queued for a retry"
    )
    metadata = row.metadata_json or {}
    assert metadata.get("rearm_reason") == "enqueue_refused:ConnectionError"
    assert metadata.get("rearmed_at"), "the reconciler's marker is what re-dispatches it"
    assert "E_REDIS" in (row.error_message or ""), "the row must carry the refusal's name"

    # A second attempt can still run it: the worker's own claim (the only path a delivery takes)
    # accepts the row. The claim is the delivery's proof of life.
    claimed = await claim_task(async_db_session, worker="post-refusal-attempt", task_id=task_id)
    assert claimed is not None and claimed.status == "in_progress", (
        "the re-armed row must still be runnable by a later delivery"
    )


@pytest.mark.asyncio
async def test_the_dead_broker_ladder_elapses_bounded(api_client, auth_headers):
    """The refusal AND the clock, against the real transport configuration (no stub, no monkeypatch).

    The bound is configuration, not luck: `celery_app/celery_config.py` caps every broker/result
    socket operation at 1.0 s with at most one retry. Measured at T19: 12.758 s on the uvicorn path
    (W4 scratch line 137), 12.981 s through this exact API path (first run, 4-seat load), 6.29-6.61 s
    for a bare in-process `delay()` (probe) — the delta is the result-backend consumer climbing its
    own bounded ladder first. Which part of the config IS the bound was measured too: widening the
    socket timeouts alone left the refused-port ladder unchanged (ECONNREFUSED is instant; measured
    15.3 s total under 10.0 s timeouts, ceiling intact), while defeating the RETRY CAP
    (`max_retries` 1 -> 20, step 1.0 s) measured **129.5 s** through this same path and killed the
    ceiling — that is the failure this assertion exists to catch. The plan's draft `< 10 s` example
    is BELOW the measured API-path bound — re-decided here against the measurements, per the
    wave-4 conflict note.
    """
    import socket
    from urllib.parse import urlparse

    from celery_app.worker import app

    parsed = urlparse(app.conf.broker_url or "")
    with socket.socket() as probe:
        probe.settimeout(1.0)
        reachable = probe.connect_ex((parsed.hostname or "localhost", parsed.port or 6379)) == 0
    if reachable:
        pytest.skip("a live broker is reachable — the dead-broker bound is not measurable here")

    started = time.perf_counter()
    response = await api_client.post("/api/v1/agents/social_media/trigger", headers=auth_headers)
    elapsed = time.perf_counter() - started

    assert response.status_code == 503, "the dead broker is a named refusal, not a fake queue"
    assert response.json()["error"]["code"] == "E_REDIS"
    assert elapsed < 20.0, (
        f"the dead-broker refusal took {elapsed:.1f}s — the bounded transport "
        "(celery_config.py: 1.0 s socket bounds, one retry per transport) measured 12.8-13.0 s "
        "on the API path; widened to 10.0 s socket bounds it measured 63.8 s (T19 probe table)"
    )


def test_the_broker_transport_is_bounded_by_configuration():
    """Pin the CONFIG: the bound the test above measures is shipped, not incidental."""
    from celery_app.worker import app

    for name in ("broker_transport_options", "result_backend_transport_options"):
        options = dict(getattr(app.conf, name) or {})
        assert options.get("socket_connect_timeout") == 1.0, f"{name} lost its connect bound"
        assert options.get("socket_timeout") == 1.0, f"{name} lost its operation bound"
        retries = (options.get("retry_policy") or {}).get("max_retries")
        assert retries is not None and retries <= 1, f"{name} gained retries: {retries}"
