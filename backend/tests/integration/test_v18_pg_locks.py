"""V18 / L3 proof 1 — the F-04 row lock under the REAL PostgreSQL engine.

`_clear_paused_markers` merges the whole `tasks.metadata` JSON blob, so it must take the row
lock BEFORE it reads the payload: a concurrent metadata writer that commits inside its
read -> write window would otherwise have its keys silently erased (JSON is last-write-wins).

The V17 pin ran on SQLite (WAL). This file runs the SAME claim on Postgres 16:

* `test_row_lock_holds_a_concurrent_writer_until_the_merged_payload_lands` — writer B holds the
  row lock and has read the pre-clear payload; the REAL `_clear_paused_markers` is started and
  must WAIT (an ungranted lock is observed in `pg_locks`), then merge on top of B's commit. Both
  writers' disjoint keys survive.
* `test_control_an_unlocked_read_modify_write_loses_the_other_writers_key` — CONTROL LEG, not a
  production path: the same two sessions, the same commit instant, with the clear's protocol
  reproduced WITHOUT the lock (read -> B commits -> write the stale blob). B's key is LOST. That
  is what the shipped lock prevents, measured on this engine.

No mocks: real asyncpg sessions against the testcontainers Postgres; the only non-production
code is the control leg, which is labelled as such in its docstring and its assertions.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import make_url, select, text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def app_factory(integration_db, postgres_url, monkeypatch):
    """The PROCESS session factory (`get_session_factory`) bound to the real Postgres container.

    The shipped task functions build their own sessions from `app.core.database`, so the proof
    has to move that seam onto the container instead of handing them a session: `database_url`
    is re-pointed at the container and the laziest engine is rebuilt. A guard assertion fails
    loudly if the engine is ever bound anywhere else (the developer default points at a LIVE
    localhost stack — a test must never write there).
    """
    from app.config import settings
    from app.core import database

    monkeypatch.setattr(settings, "database_url", postgres_url)
    await database.dispose_engine()
    factory = database.get_session_factory()
    engine_url = database.get_engine().url
    expected = make_url(postgres_url)
    got = (engine_url.host, engine_url.port, engine_url.database)
    assert got == (expected.host, expected.port, expected.database), (
        f"the process engine must be bound to the test container, got {got}"
    )
    try:
        yield factory
    finally:
        await database.dispose_engine()


async def _seed(factory, *, title: str, metadata: dict) -> int:
    from app.models.task import Task

    async with factory() as db:
        row = Task(
            title=title,
            agent_type="finance",
            status="pending",
            source="manual",
            metadata_json=metadata,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return int(row.id)


async def _metadata(factory, task_id: int) -> dict:
    from app.models.task import Task

    async with factory() as db:
        row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
        return dict(row.metadata_json or {})


async def _ungranted_locks(factory) -> int:
    """How many locks the server currently reports as NOT granted (waiters)."""
    async with factory() as db:
        return int((await db.execute(text("select count(*) from pg_locks where not granted"))).scalar_one())


async def _wait_for_a_waiter(factory, deadline_seconds: float = 10.0) -> int:
    """Poll until the server reports at least one waiting lock (or the deadline passes)."""
    waited = 0.0
    seen = 0
    while waited < deadline_seconds:
        seen = await _ungranted_locks(factory)
        if seen:
            return seen
        await asyncio.sleep(0.05)
        waited += 0.05
    return seen


async def test_row_lock_holds_a_concurrent_writer_until_the_merged_payload_lands(app_factory):
    """The clear WAITS for the row lock, then merges on top of the other writer's commit."""
    from app.models.task import Task
    from celery_app.tasks import maintenance

    task_id = await _seed(
        app_factory,
        title="v18 pg row lock",
        metadata={
            "paused_release": {"released_at": datetime.now(UTC).isoformat(), "reason": "paused"},
            "keep": "a",
        },
    )

    # Writer B is the well-behaved concurrent writer: it takes the row lock BEFORE it reads.
    # It holds the lock across the clear's start, so the clear cannot read a stale payload.
    writer_b = app_factory()
    try:
        locked = (
            await writer_b.execute(select(Task).where(Task.id == task_id).with_for_update())
        ).scalar_one()
        assert (locked.metadata_json or {}).get("paused_release"), "B read the pre-clear payload"

        clear = asyncio.create_task(maintenance._clear_paused_markers([task_id]))
        waiters = await _wait_for_a_waiter(app_factory)
        assert waiters >= 1, (
            "the clear must be WAITING on the row lock while B holds it "
            "(an unlocked read-modify-write would not block here)"
        )
        assert not clear.done(), "the clear must not finish while the row is locked by B"

        # B merges its own disjoint key and commits — the instant the finding describes.
        locked.metadata_json = {**(locked.metadata_json or {}), "b_key": "b"}
        await writer_b.commit()
    finally:
        await writer_b.close()

    await asyncio.wait_for(clear, timeout=60)

    meta = await _metadata(app_factory, task_id)
    assert meta.get("keep") == "a", "the clear must not drop a key it does not own"
    assert meta.get("b_key") == "b", "a concurrent writer's key must survive the clear (no lost update)"
    assert meta.get("rearmed_at"), "the clear still stamps its own acceptance evidence"
    assert "paused_release" not in meta, "the marker the clear OWNS is removed"


async def test_control_an_unlocked_read_modify_write_loses_the_other_writers_key(app_factory):
    """CONTROL LEG (not a production path): without the lock, the same interleave LOSES B's key.

    This is the pre-fix algorithm reproduced statement for statement — read the payload with no
    lock, let B commit, then write the stale blob back. It exists so the test above is measured
    against a protocol that actually can lose the update on THIS engine, rather than trusting
    that a passing lock test would have failed without the lock.
    """
    from app.models.task import Task

    task_id = await _seed(
        app_factory,
        title="v18 pg control (unlocked)",
        metadata={"keep": "a"},
    )

    stale_writer = app_factory()
    try:
        stale_row = (
            await stale_writer.execute(select(Task).where(Task.id == task_id))
        ).scalar_one()  # NO lock: exactly the pre-F-04 read
        stale_payload = dict(stale_row.metadata_json or {})
        stale_payload["rearmed_at"] = datetime.now(UTC).isoformat()

        # B commits its disjoint key inside the unlocked writer's read -> write window.
        async with app_factory() as writer_b:
            row_b = (
                await writer_b.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one()
            row_b.metadata_json = {**(row_b.metadata_json or {}), "b_key": "b"}
            await writer_b.commit()

        stale_row.metadata_json = stale_payload  # last write wins: B's key is overwritten
        await stale_writer.commit()
    finally:
        await stale_writer.close()

    meta = await _metadata(app_factory, task_id)
    assert meta.get("keep") == "a"
    assert "rearmed_at" in meta
    assert "b_key" not in meta, (
        "the CONTROL must lose B's key: this is the lost update the shipped row lock prevents"
    )
