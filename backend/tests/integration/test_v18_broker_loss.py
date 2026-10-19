"""V18 / L3 proof 2 — accept-then-lose on the REAL Redis broker, re-dispatched EXACTLY once.

The V17 F-03 fix taught `reconcile_stranded_pending_tasks` to cover `pending` rows that carry the
pause re-arm's own `metadata.rearmed_at` evidence — the shape left behind when the broker ACCEPTED
a `.delay()` and then lost the message (`.delay()` returning only means the publish was taken).
The V17 pin proved it with a `.delay` recorder. This file proves it against a real Redis: the
message is really published, really flushed out of the queue, and the real reconciler really
publishes it again — once, not twice, and never for a row that was claimed.

No mocks: real Redis 7 container, real Postgres 16 container, the shipped tick functions.
Every test flushes the queue in a `finally` so nothing survives the file.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, make_url, select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

AGENTS_QUEUE = "agents"  # `task_routes["celery_app.tasks.agent_tasks.*"]`


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@pytest_asyncio.fixture(loop_scope="session")
async def engines(integration_db, postgres_url, redis_url, monkeypatch):
    """Bind the process seams to the containers: DB engine, Redis URL, Celery broker.

    Returns `(session_factory, redis_client)`. The guard assertion refuses to run if the process
    engine is bound anywhere but the test container (the developer default is a LIVE localhost DB).
    """
    from app.config import settings
    from app.core import database

    monkeypatch.setattr(settings, "database_url", postgres_url)
    monkeypatch.setattr(settings, "redis_url", redis_url)
    monkeypatch.setattr(settings, "celery_broker_url", redis_url)
    monkeypatch.setattr(settings, "celery_result_backend", redis_url)
    await database.dispose_engine()
    factory = database.get_session_factory()
    engine_url = database.get_engine().url
    expected = make_url(postgres_url)
    assert (engine_url.host, engine_url.port, engine_url.database) == (
        expected.host,
        expected.port,
        expected.database,
    ), "the process engine must be bound to the test container"

    from celery_app.worker import app as worker_app

    # Cached producers/connections would otherwise keep publishing to whatever broker was
    # configured first — on this host that is the LIVE stack. Drop the caches (do NOT touch the
    # live connection) so the next `.delay()` builds its producer from the NEW broker_url.
    worker_app.conf.update(
        broker_url=redis_url,
        broker_read_url=redis_url,
        broker_write_url=redis_url,
        result_backend=redis_url,
    )
    worker_app.__dict__.pop("pool", None)  # cached_property -> a closed/stale pool
    worker_app._pool = None
    amqp = worker_app.__dict__.get("amqp")
    if amqp is not None:
        amqp._producer_pool = None
    assert worker_app.conf.task_always_eager is False, "a real broker publish is the point"

    import redis as redis_lib

    client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
    try:
        client.flushall()
        yield factory, client
    finally:
        client.flushall()
        client.close()
        await database.dispose_engine()


async def _seed(factory, *, title: str, metadata: dict, **fields) -> int:
    from app.models.task import Task

    now = datetime.now(UTC)
    async with factory() as db:
        row = Task(
            title=title,
            agent_type="finance",
            status="pending",
            source="manual",
            metadata_json=metadata,
            created_at=now,
            updated_at=now,
            **fields,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return int(row.id)


async def _drop(factory, *task_ids: int) -> None:
    """Remove this file's rows (and its activity rows) — cleanup is part of the contract."""
    from app.models.report import ActivityLog
    from app.models.task import Task

    async with factory() as db:
        await db.execute(delete(Task).where(Task.id.in_(task_ids)))
        await db.execute(
            delete(ActivityLog).where(
                ActivityLog.action.in_(
                    ("task_pending_redispatched", "task_pending_redispatch_failed")
                )
            )
        )
        await db.commit()


def _queued_ids(client) -> list[int]:
    """The task ids in the real `agents` queue, read back from the broker itself."""
    ids: list[int] = []
    for raw in client.lrange(AGENTS_QUEUE, 0, -1):
        message = json.loads(raw)
        headers = message.get("headers") or {}
        argsrepr = str(message.get("argsrepr") or headers.get("argsrepr") or "")
        body = message.get("args") or []
        if isinstance(body, list) and body and isinstance(body[0], int):
            ids.append(int(body[0]))
            continue
        digits = "".join(ch for ch in argsrepr if ch.isdigit())
        if digits:
            ids.append(int(digits))
    return ids


async def test_a_message_the_broker_accepted_then_lost_is_redispatched_exactly_once(engines):
    """Publish for real, FLUSH the queue, then the real reconciler must re-dispatch ONCE."""
    from celery_app.tasks.agent_tasks import run_agent_task
    from celery_app.tasks.maintenance import _reconcile_stranded_pending

    factory, client = engines
    old = _iso(datetime.now(UTC) - timedelta(seconds=45 * 60))  # older than the 30-min window
    task_id = await _seed(
        factory,
        title="v18 broker accept-then-lose",
        metadata={"rearmed_at": old},
    )
    try:
        # 1. the REAL acceptance edge: the broker takes the message.
        run_agent_task.delay(task_id)
        assert _queued_ids(client) == [task_id], "the broker must really hold the message"

        # 2. accept-then-LOSE: the queue is emptied without any worker ever reading it. The row
        #    is untouched by a flush — it is still `pending`, unclaimed, with its `rearmed_at`.
        client.flushall()
        assert _queued_ids(client) == [], "the accepted message is gone, exactly as in the finding"

        async with factory() as db:
            from app.models.task import Task

            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            assert str(row.status) == "pending"
            assert row.claimed_at is None
            assert (row.metadata_json or {}).get("rearmed_at") == old

        # 3. the real tick re-dispatches it — once.
        first = await _reconcile_stranded_pending()
        assert first["redispatched"] == 1, first
        assert first["task_ids"] == [task_id], first
        assert first["rearmed_ids"] == [task_id], "the re-arm's own evidence is the route"
        assert _queued_ids(client) == [task_id], "the row is back on the real broker, once"

        # 4. a second tick inside the window re-dispatches NOTHING: the `dispatched_at` stamp the
        #    first tick wrote is the exactly-once guard.
        second = await _reconcile_stranded_pending()
        assert second["redispatched"] == 0, second
        assert task_id in second["skipped_recent"], second
        assert _queued_ids(client) == [task_id], "still ONE message — never twice"
    finally:
        client.flushall()
        await _drop(factory, task_id)


async def test_a_claimed_row_is_never_redispatched(engines):
    """A claimed row is proof of life: the same evidence must not put it back on the broker."""
    from celery_app.tasks.maintenance import _reconcile_stranded_pending

    factory, client = engines
    old = _iso(datetime.now(UTC) - timedelta(seconds=45 * 60))
    task_id = await _seed(
        factory,
        title="v18 broker claimed row",
        metadata={"rearmed_at": old},
        claimed_at=datetime.now(UTC) - timedelta(seconds=90 * 60),
        claimed_by="worker-1",
    )
    try:
        assert _queued_ids(client) == []
        result = await _reconcile_stranded_pending()
        assert task_id not in result["task_ids"], result
        assert result["redispatched"] == 0, result
        assert _queued_ids(client) == [], "a claimed row must never be re-dispatched"
    finally:
        client.flushall()
        await _drop(factory, task_id)


async def test_a_fresh_rearm_is_left_alone_until_its_window_expires(engines):
    """Inside the window the row is a LIVE hand-off, not a stranded one — no second message."""
    from celery_app.tasks.maintenance import _reconcile_stranded_pending

    factory, client = engines
    fresh = _iso(datetime.now(UTC) - timedelta(seconds=60))
    task_id = await _seed(
        factory,
        title="v18 broker fresh rearm",
        metadata={"rearmed_at": fresh},
    )
    try:
        result = await _reconcile_stranded_pending()
        assert task_id not in result["task_ids"], result
        assert _queued_ids(client) == [], "a live hand-off must not be fed twice"
    finally:
        client.flushall()
        await _drop(factory, task_id)
