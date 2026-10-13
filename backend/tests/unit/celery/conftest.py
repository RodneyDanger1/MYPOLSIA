"""Queue safety-core fixtures — a FILE-backed database driven on the Celery process loop.

Two fixture decisions the tests depend on, both measured:

* **FILE-backed SQLite, never `sqlite+aiosqlite:///:memory:`.**  The root conftest's in-memory
  engine is a `StaticPool`: every session hands out THE SAME connection (measured:
  `polsia-work/build/E-queue/stray/probe_pool_loop.py` → `pool class: StaticPool`,
  `count on loop1: 1`, `count on loop2: 1`).  Two sessions therefore cannot represent two
  writers — a claim/heartbeat race would be sequenced on one connection and pass for the wrong
  reason.  A file-backed engine (pool: `NullPool`, one connection per session) plus the app's
  30 s sqlite busy timeout (`app/core/database.py`) gives genuine contention: a second writer
  WAITS instead of raising `database is locked`.  Every test in this directory that could race
  two writers uses `queue_db`.
* **The Celery process loop, not the pytest-asyncio loop.**  `run_agent_task` drives its async
  body through `celery_app.tasks._common.run_async` — one loop on a module daemon thread, by
  design not the pytest loop.  These tests drive the database through the SAME loop
  (`run_async`), so every aiosqlite connection is created and used on one loop, and a test page
  never straddles two.

The app-wide session factory is what `_execute_agent_task` uses at call time, so the fixture
points it at the file by patching `settings.database_url` and letting the real lazy
`get_engine()` build the SQLite engine (the production construction path, busy timeout
included).

The shared fixtures below are RE-EXPOSED one level up in `tests/unit/conftest.py` (R20 item 3):
pytest 9.1.1's once-per-directory pending-conftest parse can hand the later celery-file items of
a mixed file-argument battery to a SECOND `tests/unit/celery` Package node that never received
this file's fixtures ("fixture 'queue_db' not found"); the parent-level copies keep fixture
availability order-independent.  Same fixture objects — keep both sides in sync (pinned by
`tests/unit/test_r20_conftest_ordering_guard.py`).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.models  # noqa: F401  — register EVERY table on Base.metadata before create_all
from app.agents.crew_factory import run_agent_for_task as ORIGINAL_RUN_AGENT_FOR_TASK
from celery_app.tasks._common import get_process_loop, run_async


class QueueDB:
    """A file-backed database behind the app-wide session factory.

    `.row()`/`.count()` read the FILE with the stdlib driver — no ORM, no pool, no identity
    map — so an assertion here is an assertion about the row that is actually on disk.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # ── ORM side (the app's own factory, on the process loop) ────────────────
    @property
    def factory(self):
        from app.core.database import get_session_factory

        return get_session_factory()

    def run(self, coro):
        return run_async(coro)

    def create_task(self, *, title: str = "queue test task", agent_type: str = "social_media", **kw) -> int:
        from app.services.task_service import create_task

        async def _make() -> int:
            async with self.factory() as db:
                task = await create_task(db, title=title, agent_type=agent_type, source="test", **kw)
                await db.commit()
                return int(task.id)

        return self.run(_make())

    def claim(self, task_id: int, worker: str, lease_seconds: int | None = None) -> None:
        from app.services.task_service import claim_task

        async def _claim() -> None:
            async with self.factory() as db:
                await claim_task(db, worker=worker, task_id=task_id, lease_seconds=lease_seconds)
                await db.commit()

        self.run(_claim())

    def release(self, task_id: int, worker: str) -> None:
        from app.services.task_service import release_claim

        async def _release() -> None:
            async with self.factory() as db:
                await release_claim(db, task_id, worker)
                await db.commit()

        self.run(_release())

    def backdate_heartbeat(self, task_id: int, seconds: int) -> None:
        """Move a claim's last sign of life into the past, dialect-naively (sqlite stores naive UTC)."""

        async def _backdate() -> None:
            from sqlalchemy import select

            from app.models.task import Task

            async with self.factory() as db:
                row = (
                    await db.execute(select(Task).where(Task.id == task_id))
                ).scalar_one()
                row.heartbeat_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds)
                await db.commit()

        self.run(_backdate())

    # ── raw file side (no ORM: what is actually stored) ──────────────────────
    def _read(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def row(self, task_id: int) -> dict[str, Any]:
        rows = self._read("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else {}

    def count(self, table: str, where: str = "1=1", params: tuple = ()) -> int:
        return self._read(f"SELECT count(*) AS n FROM {table} WHERE {where}", params)[0]["n"]

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self._read(sql, params)


class RunSpy:
    """Stands in for `app.agents.crew_factory.run_agent_for_task`.

    It is a SPY, not a stub, for the happy paths: it records what it sees and then delegates to
    the real function, so the real crew_factory + real agent + the repo's own CLAUDE_CLI_MOCK
    seam still run.  The record is the task row read from the file at call time — the
    claim-ordering evidence ("the claim happened before any work").

    `error_factory` makes the run fail with a fresh exception per call; `sleep_s` slows the work
    down (used by the lease test, where a heartbeat can only fire after its 5 s floor).
    """

    def __init__(
        self,
        db: QueueDB,
        *,
        delegate=None,
        error_factory=None,
        sleep_s: float = 0.0,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.delegate = delegate
        self.error_factory = error_factory
        self.sleep_s = sleep_s
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, agent_type, task_dict, context):
        task_id = (task_dict or {}).get("id")
        record = {
            "agent_type": agent_type,
            "task_id": task_id,
            "row_at_call": self.db.row(task_id) if task_id is not None else {},
        }
        self.calls.append(record)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.error_factory is not None:
            raise self.error_factory()
        if self.delegate is not None:
            return self.delegate(agent_type, task_dict, context)
        return self.result or {"summary": f"spy result for {agent_type}"}


@pytest.fixture
def queue_db(monkeypatch, tmp_path):
    """Point the app-wide session factory at a fresh file-backed SQLite database."""
    from app.core import database

    path = tmp_path / "queue.sqlite"
    monkeypatch.setattr(database.settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")

    async def _build() -> None:
        await database.dispose_engine()
        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    run_async(_build())
    yield QueueDB(path)
    run_async(database.dispose_engine())


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the activity publish goes to a mock (the root conftest does the same for API tests).

    Autouse in this directory: every test here runs the queue path, which logs activity, and the
    first real publish attempt against a refused Redis socket costs a bounded-but-real ~1 s.
    """
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture
def install_run_spy(monkeypatch, queue_db):
    """Install a RunSpy in place of crew_factory.run_agent_for_task (the per-call seam).

    The delegate is the ORIGINAL function captured once at import — never the attribute as it
    stands, which may already be another spy this test installed (chaining spies into a chain
    that raises is a silent way to test the wrong thing).
    """

    def _install(*, delegate: bool = True, error_factory=None, sleep_s: float = 0.0, result=None) -> RunSpy:
        import app.agents.crew_factory as crew_factory

        spy = RunSpy(
            queue_db,
            delegate=ORIGINAL_RUN_AGENT_FOR_TASK if delegate else None,
            error_factory=error_factory,
            sleep_s=sleep_s,
            result=result,
        )
        monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
        return spy

    return _install


@pytest.fixture
def execute_body():
    """Deliver one message by driving the async body directly (no Celery machinery)."""

    def _run(task_id: int):
        from celery_app.tasks.agent_tasks import _execute_agent_task

        return run_async(_execute_agent_task(int(task_id)))

    return _run


@pytest.fixture
def eager_delivery():
    """Deliver one message through the real Celery task in eager (`apply`) mode."""

    def _run(task_id: int, *, throw: bool = False):
        from celery_app.tasks.agent_tasks import run_agent_task

        return run_agent_task.apply(kwargs={"task_id": int(task_id)}, throw=throw)

    return _run


@pytest.fixture
def service():
    """The task service MODULE (so both its functions and its attributes are reachable)."""
    from app.services import task_service

    return task_service


@pytest.fixture
def worker_fn():
    from celery_app.tasks.agent_tasks import worker_id

    return worker_id


@pytest.fixture
def steal_claim(queue_db, worker_fn):
    """Schedule a concurrent 'another worker owns this row now' takeover on the process loop.

    Uses the service's own verbs: the operator releases the lost claim, another worker claims
    the row.  Returns a Future that resolves when the takeover has committed.
    """

    def _steal(task_id: int, *, after_s: float, thief: str = "thief-worker-1", lease_seconds: int = 3):
        import asyncio

        from app.services.task_service import claim_task, release_claim

        me = worker_fn()

        async def _takeover() -> None:
            await asyncio.sleep(after_s)
            async with queue_db.factory() as db:
                await release_claim(db, task_id, me)
                await db.commit()
            async with queue_db.factory() as db:
                await claim_task(db, worker=thief, task_id=task_id, lease_seconds=lease_seconds)
                await db.commit()

        return asyncio.run_coroutine_threadsafe(_takeover(), get_process_loop())

    return _steal
