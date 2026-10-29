"""WAVE A3 / S5 — the state machine's decided transitions (A2A2-01, F-02, F-15, A2A6-01, A2A6-02, A2A7-02).

One block per defect, each written RED-first against the real services on a real database:

* **A2A2-01** — `POST /agents/{type}/trigger` handed the id to the broker BEFORE the row was
  committed, so a worker could claim a row that did not exist yet and a failed commit left a
  message for a row that never existed.  The two legs here are the two-connection proof: a
  second engine reads (and claims) the row from INSIDE `.delay()`, and a poisoned commit must
  dispatch nothing and answer a coded refusal.
* **F-02** — the plan's `depends_on` was dropped at materialisation and every entry was
  dispatched flat.  A 3-node plan must dispatch its one root, then the dependent, never all
  three at once — and a dependency this cycle did not create is RECORDED, never guessed.
* **F-15** — a stale-claim requeue wrote `pending` and nothing else, so an
  orchestrator/manual/chat row went to a state nobody watched.  The reconciler's own marker
  (`metadata.rearmed_at`) is the evidence, and it must be written for the rows that were
  requeued — never for the ones that went terminal.
* **A2A6-01** — `get_runnable_task` must answer the question `claim_task` answers, schedule
  window included (the paired matrix below is the contract: same answer, both directions).
* **A2A6-02** — `release_claim` must not rewrite a human `cancelled` back to `pending`.
  Both directions are pinned (a live claim still releases; a cancelled row does not revive).
* **A2A7-02** — a task whose runs are only PARTLY priced has no complete total: the cost is
  null and the unpriced run is counted, never dropped by `SUM`.

Every stand-in here is a process boundary (the broker hand-off, the model call), never the
behaviour under test.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import utcnow
from app.core.errors import E_DB, E_TASK_CLAIMED, E_TASK_NOT_RUNNABLE, MeridianError
from app.models.task import AgentRun, Task
from app.services.task_service import (
    claim_task,
    create_task,
    get_runnable_task,
    get_task,
    get_task_run_totals,
    release_claim,
    update_task_status,
)

# ══ A2A2-01 — the row is committed BEFORE the broker is told ═══════════════════════════════════


class DispatchProbe:
    """The broker edge, with a WORKER's eyes: it reads the row from a SECOND connection.

    `.delay` runs inside the `asyncio.to_thread` the route uses, so this probe stands exactly
    where a worker stands at delivery time.  It records what a fresh SELECT can see and, when
    the row is there, runs the REAL `claim_task` through its own session — the transition the
    worker performs on arrival.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.delivered: list[int] = []
        self.seen: list[Task | None] = []
        self.status_at_read: list[str] = []
        self.claimed: list[int] = []
        self.claim_refusals: list[str] = []

    def delay(self, task_id: int) -> None:
        self.delivered.append(int(task_id))
        asyncio.run(self._probe(int(task_id)))

    async def _probe(self, task_id: int) -> None:
        engine = create_async_engine(self.url)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with session_factory() as db:
                row = (
                    await db.execute(select(Task).where(Task.id == task_id))
                ).scalar_one_or_none()
                self.seen.append(row)
                if row is None:
                    return
                # Recorded BEFORE the claim: `claim_task` refreshes this same identity-map row.
                self.status_at_read.append(str(row.status))
                try:
                    claimed = await claim_task(db, worker="probe-worker-1", task_id=task_id)
                except MeridianError as exc:
                    self.claim_refusals.append(exc.code)
                    return
                assert claimed is not None
                await db.commit()
                self.claimed.append(int(claimed.id))
        finally:
            await engine.dispose()


class PoisonedSession(AsyncSession):
    """Fails the FIRST `commit()` — a store outage at the one write the route must own."""

    poisoned = True

    async def commit(self) -> None:
        if self.poisoned:
            self.poisoned = False
            raise OperationalError(
                "COMMIT", {}, RuntimeError("database is locked (poisoned commit probe)")
            )
        await super().commit()


async def _file_db(tmp_path):
    import app.models  # noqa: F401  (register every table on Base.metadata)
    from app.core.database import Base

    path = tmp_path / "agents-trigger.sqlite"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return url, engine


def _install_trigger_app(monkeypatch, url: str, *, session_class=AsyncSession):
    """Point the app's DB dependency at the file DB and return (engine, AsyncClient)."""
    from httpx import ASGITransport, AsyncClient

    from app.api.deps import get_db
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "api_key", "test-key")
    write_engine = create_async_engine(url)
    session_factory = async_sessionmaker(write_engine, class_=session_class, expire_on_commit=False)

    async def _override_get_db():
        # The production shape (`app.core.database.get_db`): one session per request, commit
        # after the body, rollback on a raise.  The route's OWN commit is what the probe inside
        # `.delay()` observes — this teardown commit happens after the response body exists.
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    # `setitem` (not a plain assignment) so monkeypatch REMOVES the override at teardown: a bare
    # `app.dependency_overrides[get_db] = …` outlives the test and every later request in the
    # process then reads this test's empty tmp file DB — measured as
    # `test_daily_cycle_window::test_a_configured_hour_drives_the_window_through_the_api_round_trip`
    # failing with `E_NOT_FOUND … company configuration has not been seeded yet` in the full gate
    # while passing alone.
    monkeypatch.setitem(app.dependency_overrides, get_db, _override_get_db)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return write_engine, client


@pytest.mark.asyncio
async def test_the_dispatched_task_is_visible_to_a_worker_session(tmp_path, monkeypatch):
    """The 202 is only honest when a worker picking the message up NOW can claim the row."""
    import celery_app.tasks.agent_tasks as agent_tasks

    url, admin_engine = await _file_db(tmp_path)
    probe = DispatchProbe(url)
    write_engine, client = _install_trigger_app(monkeypatch, url)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", probe.delay)

    try:
        async with client:
            response = await client.post(
                "/api/v1/agents/social_media/trigger",
                headers={"X-API-Key": "test-key"},
                json={"task_title": "committed before dispatch"},
            )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        assert probe.delivered == [task_id], "the hand-off names the created row"
        assert probe.seen, "the probe never ran — the dispatch it stands for did not happen"
        assert probe.seen[0] is not None, (
            "a worker picking the message up at .delay() time read NO row: the hand-off "
            "happened before the commit"
        )
        assert probe.status_at_read == ["pending"], (
            "the row a worker read at delivery time is not a claimable `pending` row"
        )
        assert probe.claimed == [task_id], (
            f"the worker's claim did not take the row: {probe.claim_refusals}"
        )

        # …and the claim is the durable transition, read here through the admin engine.
        async with async_sessionmaker(admin_engine, class_=AsyncSession)() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            assert str(row.status) == "in_progress"
            assert row.claimed_by == "probe-worker-1"
    finally:
        await write_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_a_refused_commit_dispatches_nothing_and_refuses_by_name(tmp_path, monkeypatch):
    """A refused commit must produce NO message: a delivered id whose row never existed is
    unclaimable for ever, and nothing in the tree re-dispatches a `manual` row."""
    import celery_app.tasks.agent_tasks as agent_tasks

    url, admin_engine = await _file_db(tmp_path)
    probe = DispatchProbe(url)
    write_engine, client = _install_trigger_app(monkeypatch, url, session_class=PoisonedSession)
    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", probe.delay)

    try:
        async with client:
            response = await client.post(
                "/api/v1/agents/social_media/trigger",
                headers={"X-API-Key": "test-key"},
                json={"task_title": "poisoned commit probe"},
            )
        assert probe.delivered == [], (
            "the broker was handed an id for a row whose commit was REFUSED — that message can "
            "never be claimed"
        )
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == E_DB, "a store failure must name itself"
        assert body["error"]["how_to_fix"], "a named refusal says how to fix it"

        async with async_sessionmaker(admin_engine, class_=AsyncSession)() as db:
            ghosts = (
                (await db.execute(select(Task).where(Task.title == "poisoned commit probe")))
                .scalars()
                .all()
            )
        assert ghosts == [], "the rolled-back row must not survive the refusal"
    finally:
        await write_engine.dispose()
        await admin_engine.dispose()


# ══ F-02 — the plan's DAG is persisted, and only its roots are dispatched ══════════════════════


class FanOutRecorder:
    """`run_agent_task` stand-in at the broker edge: records both hand-off shapes, no broker."""

    def __init__(self) -> None:
        self.delivered: list[int] = []

    def delay(self, task_id: int) -> None:
        self.delivered.append(int(task_id))

    def s(self, task_id: int) -> tuple[str, int]:
        return ("sig", int(task_id))


def _install_fanout(monkeypatch) -> FanOutRecorder:
    import celery

    rec = FanOutRecorder()
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)

    def _group(sigs):
        return SimpleNamespace(apply_async=lambda: rec.delivered.extend(s[1] for s in sigs))

    monkeypatch.setattr(celery, "group", _group)
    return rec


def _now_utc_hour() -> int:
    return datetime.now(UTC).hour


def _seed_company(qdb, hour: int) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            db.add(CompanyConfig(name="S5 Co", timezone="UTC", daily_cycle_hour=hour))
            await db.commit()

    qdb.run(_go())


def _set_company_hour(qdb, hour: int) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            row = (await db.execute(select(CompanyConfig))).scalars().first()
            row.daily_cycle_hour = hour
            await db.commit()

    qdb.run(_go())


def _run_morning_inside_window(qdb) -> dict[str, Any]:
    """Run the real morning cycle inside its window (re-pinning if the minute rolls over)."""
    from celery_app.tasks import daily_cycle

    for _attempt in range(3):
        _set_company_hour(qdb, _now_utc_hour())
        result = daily_cycle.run_morning_cycle()
        if result.get("reason") != "outside_morning_window":
            return result
    raise AssertionError("could not land inside the morning-cycle window")


def _install_plan_spy(monkeypatch, entries: list[dict[str, Any]]) -> None:
    import app.agents.crew_factory as crew_factory

    def _spy(agent_type, task_dict, context):
        return {"summary": "S5 plan", "tasks": entries}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)


def _rows_by_title(qdb) -> dict[str, dict[str, Any]]:
    return {row["title"]: row for row in qdb.query("SELECT id, title, status, metadata FROM tasks")}


def _meta_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata")
    if raw in (None, "null", ""):
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _complete(qdb, task_id: int) -> None:
    async def _go() -> None:
        async with qdb.factory() as db:
            await update_task_status(db, task_id, "completed")
            await db.commit()

    qdb.run(_go())


def _release_and_dispatch(qdb, completed_id: int) -> list[int]:
    """The completion hook's own three steps: release, COMMIT, then hand off (F-02)."""
    from app.services.task_service import release_ready_dependents
    from celery_app.tasks._common import enqueue_created

    async def _release() -> list[int]:
        async with qdb.factory() as db:
            ready = await release_ready_dependents(db, completed_id=completed_id)
            await db.commit()
            return ready

    ready = qdb.run(_release())
    if ready:
        qdb.run(enqueue_created(ready, label=f"dag dependents of {completed_id}"))
    return ready


def test_a_plan_dag_dispatches_its_root_then_releases_the_dependent(
    queue_db, monkeypatch, mock_redis
):
    """3 nodes in a chain: 1 dispatch at cycle time, then the dependent — never all three."""
    _seed_company(queue_db, _now_utc_hour())
    rec = _install_fanout(monkeypatch)
    _install_plan_spy(
        monkeypatch,
        [
            {"title": "Draft the launch brief", "agent_type": "business_planning", "depends_on": []},
            {
                "title": "Review the launch brief",
                "agent_type": "code_generation",
                "depends_on": ["Draft the launch brief"],
            },
            {
                "title": "Publish the launch brief",
                "agent_type": "social_media",
                "depends_on": ["Review the launch brief"],
            },
        ],
    )

    result = _run_morning_inside_window(queue_db)
    assert result["status"] == "completed", result

    rows = _rows_by_title(queue_db)
    a = rows["Draft the launch brief"]
    b = rows["Review the launch brief"]
    c = rows["Publish the launch brief"]
    planned = {a["id"], b["id"], c["id"]}
    assert [tid for tid in rec.delivered if tid in planned] == [a["id"]], (
        "the cycle dispatched every planned entry at once (or none): the DAG was dropped"
    )
    assert _meta_of(a)["depends_on_ids"] == []
    assert _meta_of(b)["depends_on_ids"] == [a["id"]], _meta_of(b)
    assert _meta_of(c)["depends_on_ids"] == [b["id"]], _meta_of(c)
    assert {"plan_day", "depends_on_titles", "side_effect", "acceptance"} <= set(_meta_of(b))

    # The parent completes: its dependent is released and dispatched exactly once.
    _complete(queue_db, a["id"])
    assert _release_and_dispatch(queue_db, a["id"]) == [b["id"]]
    assert rec.delivered.count(b["id"]) == 1
    assert c["id"] not in rec.delivered, "the grandchild waits for its OWN parent"

    # The grandchild's parent completes: now it is released.
    _complete(queue_db, b["id"])
    assert _release_and_dispatch(queue_db, b["id"]) == [c["id"]]
    assert rec.delivered.count(c["id"]) == 1


def test_a_dependency_the_cycle_did_not_create_is_recorded_not_guessed(
    queue_db, monkeypatch, mock_redis
):
    """A title this cycle did not create cannot become an edge — record it, release the node."""
    _seed_company(queue_db, _now_utc_hour())
    rec = _install_fanout(monkeypatch)
    _install_plan_spy(
        monkeypatch,
        [
            {
                "title": "Orphaned acceptance brief",
                "agent_type": "business_planning",
                "depends_on": ["A brief yesterday's cycle created"],
            }
        ],
    )

    result = _run_morning_inside_window(queue_db)
    assert result["status"] == "completed", result

    row = _rows_by_title(queue_db)["Orphaned acceptance brief"]
    meta = _meta_of(row)
    assert meta["depends_on_ids"] == [], "an absent title must not be guessed onto any row"
    assert meta["depends_on_unresolved"] == ["A brief yesterday's cycle created"]
    assert row["id"] in rec.delivered, (
        "a node whose only dependency does not exist in this cycle must not be stranded"
    )


def test_a_dependency_cycle_is_released_and_named_instead_of_stranded(
    queue_db, monkeypatch, mock_redis
):
    """A model-authored cycle cannot be topologically ordered: release it, and say so on the row."""
    _seed_company(queue_db, _now_utc_hour())
    rec = _install_fanout(monkeypatch)
    _install_plan_spy(
        monkeypatch,
        [
            {
                "title": "Cyclic brief alpha",
                "agent_type": "business_planning",
                "depends_on": ["Cyclic brief beta"],
            },
            {
                "title": "Cyclic brief beta",
                "agent_type": "code_generation",
                "depends_on": ["Cyclic brief alpha"],
            },
        ],
    )

    result = _run_morning_inside_window(queue_db)
    assert result["status"] == "completed", result

    rows = _rows_by_title(queue_db)
    for title, partner in (
        ("Cyclic brief alpha", "Cyclic brief beta"),
        ("Cyclic brief beta", "Cyclic brief alpha"),
    ):
        row = rows[title]
        meta = _meta_of(row)
        assert meta["depends_on_ids"] == [], "a cycle's edges can never be satisfied"
        assert meta["depends_on_cycle"] == [partner], (
            "the row must name the edge that made it unreleasable"
        )
        assert row["id"] in rec.delivered, (
            "work must not vanish because the model's plan was not a DAG"
        )


# ══ F-15 — a requeued row is re-armed for ALL four sources ═════════════════════════════════════


class RecordingTask:
    """`run_agent_task` stand-in for the reconciler's per-row hand-off."""

    def __init__(self) -> None:
        self.delivered: list[int] = []

    def delay(self, task_id: int) -> None:
        self.delivered.append(int(task_id))


def _claim_and_stale(qdb, task_id: int, *, worker: str = "worker-A", lease: int = 1) -> None:
    qdb.claim(task_id, worker, lease_seconds=lease)
    qdb.backdate_heartbeat(task_id, seconds=lease + 60)


def _requeue(qdb, *, lease: int = 1) -> int:
    from app.services.task_service import requeue_stale_claims

    async def _go() -> int:
        async with qdb.factory() as db:
            recovered = await requeue_stale_claims(db, lease_seconds=lease)
            await db.commit()
            return recovered

    return qdb.run(_go())


def _seed_task(qdb, *, title: str, source: str) -> int:
    """One committed row from the real service (the conftest helper pins `source='test'`)."""

    async def _go() -> int:
        async with qdb.factory() as db:
            row = await create_task(
                db, title=title, agent_type="email_outreach", source=source
            )
            await db.commit()
            return int(row.id)

    return qdb.run(_go())


@pytest.mark.asyncio
async def test_a_requeued_orchestrator_row_is_marked_and_redispatched(
    queue_db, monkeypatch, mock_redis
):
    """The sweep recovers the row; the marker is what gets it back to the broker (no 24 h wait)."""
    from celery_app.tasks import maintenance

    task_id = _seed_task(queue_db, title="orchestrator row", source="orchestrator")
    _claim_and_stale(queue_db, task_id)
    assert _requeue(queue_db) == 1

    row = queue_db.row(task_id)
    assert row["status"] == "pending" and row["claimed_by"] is None
    meta = _meta_of(row)
    assert meta.get("rearmed_at"), (
        "the sweep requeued the row with no re-arm marker: only `scheduler` rows are watched"
    )
    assert meta.get("rearm_reason") == "stale_claim_requeued"

    ledger = queue_db.query(
        "SELECT detail FROM activity_log WHERE action = 'stale_claims_requeued'"
    )
    assert len(ledger) == 1
    detail = json.loads(ledger[0]["detail"]) if isinstance(ledger[0]["detail"], str) else {}
    assert detail.get("rearmed") is True, detail

    # One window later the reconciler — which reads exactly this marker — hands it back off.
    stale = dict(meta)
    stale["rearmed_at"] = (
        datetime.now(UTC) - timedelta(seconds=maintenance.PENDING_RECONCILE_AFTER_SECONDS + 60)
    ).isoformat()

    async def _set_meta() -> None:
        async with queue_db.factory() as db:
            row_obj = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            row_obj.metadata_json = stale
            await db.commit()

    queue_db.run(_set_meta())

    rec = RecordingTask()
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)
    result = queue_db.run(maintenance._reconcile_stranded_pending())
    assert result["redispatched"] == 1, result
    assert rec.delivered == [task_id], "a requeued non-scheduler row must be re-fed by the window"


@pytest.mark.asyncio
async def test_the_requeue_marker_reaches_every_source(queue_db, mock_redis):
    """All four writers of `tasks.source` park work the same way — the marker must cover each."""
    seeded = {
        source: _seed_task(queue_db, title=f"{source} row", source=source)
        for source in ("scheduler", "manual", "chat", "orchestrator")
    }
    for task_id in seeded.values():
        _claim_and_stale(queue_db, task_id)

    assert _requeue(queue_db) == len(seeded)

    for source, task_id in seeded.items():
        row = queue_db.row(task_id)
        assert row["status"] == "pending", source
        assert _meta_of(row).get("rearmed_at"), (
            f"{source}: requeued without the reconciler's marker — it waits for `night_task`"
        )


@pytest.mark.asyncio
async def test_a_terminal_sweep_row_is_not_marked_for_retry(queue_db, mock_redis):
    """`failed` is terminal by the attempts rule — a re-arm marker would resurrect it."""
    task_id = _seed_task(queue_db, title="exhausted row", source="manual")
    _claim_and_stale(queue_db, task_id)

    async def _burn_attempts() -> None:
        async with queue_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            row.attempts = 3
            await db.commit()

    queue_db.run(_burn_attempts())
    assert _requeue(queue_db) == 1

    row = queue_db.row(task_id)
    assert row["status"] == "failed"
    assert "rearmed_at" not in _meta_of(row), (
        "a terminal row must not be handed to the broker again"
    )


# ══ A2A6-01 — the run check asks the claim's question ══════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_run_check_and_the_claim_agree_on_every_schedule_window(async_db_session):
    """Same row set, both verbs: runnable ⟺ claimable. A 202 for a row the claim refuses is a lie."""
    now = utcnow()
    schedules: dict[str, datetime | None] = {
        "unscheduled": None,
        "due_since_yesterday": now - timedelta(days=1),
        "due_in_two_days": now + timedelta(days=2),
    }
    for name, scheduled in schedules.items():
        task = await create_task(
            async_db_session,
            title=f"window probe {name}",
            agent_type="finance",
            scheduled_date=scheduled,
        )
        runnable_refusal: MeridianError | None = None
        try:
            await get_runnable_task(async_db_session, task.id)
        except MeridianError as exc:
            runnable_refusal = exc
        claim_refusal: MeridianError | None = None
        try:
            await claim_task(
                async_db_session, worker=f"worker-{name}", task_id=task.id, lease_seconds=30
            )
        except MeridianError as exc:
            claim_refusal = exc
        assert (runnable_refusal is None) == (claim_refusal is None), (
            f"{name}: the run check answered {runnable_refusal and runnable_refusal.code} while "
            f"the claim answered {claim_refusal and claim_refusal.code}"
        )


@pytest.mark.asyncio
async def test_a_future_scheduled_row_is_refused_with_the_window_named(async_db_session):
    scheduled = utcnow() + timedelta(days=2)
    task = await create_task(
        async_db_session,
        title="future row",
        agent_type="finance",
        scheduled_date=scheduled,
    )
    with pytest.raises(MeridianError) as excinfo:
        await get_runnable_task(async_db_session, task.id)
    refusal = excinfo.value
    assert refusal.code == E_TASK_NOT_RUNNABLE
    assert refusal.context["reason"] == "scheduled_in_future", refusal.context
    stamped = datetime.fromisoformat(refusal.context["scheduled_date"])
    assert stamped.replace(tzinfo=UTC) == scheduled, "the refused instant is the row's own"
    assert refusal.how_to_fix, "a named refusal says how to fix it"
    fresh = await get_task(async_db_session, task.id)
    assert str(fresh.status) == "pending" and fresh.claimed_by is None


@pytest.mark.asyncio
async def test_a_due_row_is_still_runnable(async_db_session):
    for name, scheduled in {"unscheduled": None, "overdue": utcnow() - timedelta(minutes=5)}.items():
        task = await create_task(
            async_db_session,
            title=f"due probe {name}",
            agent_type="finance",
            scheduled_date=scheduled,
        )
        runnable = await get_runnable_task(async_db_session, task.id)
        assert runnable.id == task.id


# ══ A2A6-02 — a release is a from-`in_progress` transition ═════════════════════════════════════


@pytest.mark.asyncio
async def test_a_pause_release_does_not_revive_a_cancelled_row(async_db_session):
    """The owner's `cancelled` is a human decision; the claim holder must not write over it."""
    task = await create_task(async_db_session, title="cancelled mid-run", agent_type="finance")
    task_id = int(task.id)
    await claim_task(async_db_session, worker="worker-A", task_id=task_id, lease_seconds=30)
    await update_task_status(
        async_db_session, task_id, "cancelled", error_message="owner cancelled"
    )
    await async_db_session.commit()

    with pytest.raises(MeridianError) as excinfo:
        await release_claim(async_db_session, task_id, "worker-A", release_kind="paused")
    assert excinfo.value.code == E_TASK_CLAIMED, (
        "a release that matched the cancelled row would have put it back in the queue"
    )

    async_db_session.expire_all()  # read the FILE, not the identity map the cancel left behind
    fresh = await get_task(async_db_session, task_id)
    assert str(fresh.status) == "cancelled"
    assert fresh.error_message == "owner cancelled"
    assert int(fresh.attempts or 0) == 1, "a refused release must not refund the attempt either"


@pytest.mark.asyncio
async def test_a_pause_release_still_returns_a_live_claim_to_pending(async_db_session):
    """The other direction: the transition the pause gate NEEDS still happens."""
    task = await create_task(async_db_session, title="paused live run", agent_type="finance")
    task_id = int(task.id)
    await claim_task(async_db_session, worker="worker-B", task_id=task_id, lease_seconds=30)
    await async_db_session.commit()

    assert (
        await release_claim(async_db_session, task_id, "worker-B", release_kind="paused")
    ) is True
    await async_db_session.commit()
    async_db_session.expire_all()  # read the FILE, not the identity map the UPDATE left behind
    fresh = await get_task(async_db_session, task_id)
    assert str(fresh.status) == "pending"
    assert fresh.claimed_by is None and fresh.claimed_at is None and fresh.heartbeat_at is None
    assert int(fresh.attempts or 0) == 0, "the pause gate's attempt refund still applies"


# ══ A2A7-02 — a partly priced task has no complete total ══════════════════════════════════════


def _run(task_id: int, *, status: str, cost: Decimal | None, tokens_in: int | None = None) -> AgentRun:
    return AgentRun(
        task_id=task_id,
        agent_type="finance",
        status=status,
        started_at=datetime.now(UTC),
        cost_usd=cost,
        tokens_input=tokens_in,
    )


@pytest.mark.asyncio
async def test_a_partly_priced_task_reports_no_complete_total(async_db_session):
    task = await create_task(async_db_session, title="retried run", agent_type="finance")
    async_db_session.add_all(
        [
            _run(task.id, status="completed", cost=Decimal("0.02"), tokens_in=100),
            _run(task.id, status="failed", cost=None, tokens_in=42),
        ]
    )
    await async_db_session.flush()

    totals = await get_task_run_totals(async_db_session, [task.id])
    entry = totals[task.id]
    assert entry.unmeasured_runs == 1, "the unpriced run is COUNTED, never dropped by SUM"
    assert entry.cost_usd is None, (
        f"served {entry.cost_usd} as the task's complete cost while one run never reported one"
    )
    assert entry.tokens_in == 142, "the measured token sums still add up"


@pytest.mark.asyncio
async def test_a_fully_priced_task_still_totals_and_a_missing_one_stays_absent(async_db_session):
    priced = await create_task(async_db_session, title="priced run", agent_type="finance")
    async_db_session.add(_run(priced.id, status="completed", cost=Decimal("0.02")))
    no_runs = await create_task(async_db_session, title="never ran", agent_type="finance")
    await async_db_session.flush()

    totals = await get_task_run_totals(async_db_session, [priced.id, no_runs.id])
    assert totals[priced.id].cost_usd == Decimal("0.0200")
    assert totals[priced.id].unmeasured_runs == 0
    assert no_runs.id not in totals, "a task with no runs is absent — null at the wire, not zero"


@pytest.mark.asyncio
async def test_a_running_row_is_not_counted_as_an_unmeasured_run(async_db_session):
    """A live run has not reported YET — that is not the same fact as a run that never will."""
    task = await create_task(async_db_session, title="live run", agent_type="finance")
    async_db_session.add(_run(task.id, status="running", cost=None))
    await async_db_session.flush()

    entry = (await get_task_run_totals(async_db_session, [task.id]))[task.id]
    assert entry.unmeasured_runs == 0
    assert entry.cost_usd is None, "a live run's absent cost is still unmeasured, never 0"
