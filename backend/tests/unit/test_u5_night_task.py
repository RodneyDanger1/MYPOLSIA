"""U5 / P3 — the Night Task acceptance: bounded drain, honest receipt, off = zero writes.

What this file pins (the frozen U5 interface; seat coder-p3-schema-night implements exactly this):

* `night_task.run_night_task()` (the sync Celery task, called directly in-process) self-gates on
  the COMPANY-LOCAL hour: `company_config.night_task_hour` when stored, else the documented 23:00
  default (reported as window `source == "default"`); out of 0-23 -> named `PolsiaError(E_CONFIG)`;
  stored `night_task_limit` outside 1-5 -> named `E_CONFIG` too.  The window is `[H:00, H+1:00)`
  in `core/timeutil.company_tz()` as aware UTC instants, carried as `"window"`.
* ENABLED + in-window: up to `night_task_limit` of the TOP-N `pending` rows **in the dispatcher's
  own order** (`priority, coalesce(scheduled_date, created_at), id` — the `claim_task` ordering,
  `task_service.claimable_task_ids`) are handed to the broker via `enqueue_created`, in ONE read
  transaction, and exactly ONE `log_activity` receipt (`action="night_task_ran"`, summary
  `Night task ran X of Y`) is written with titles + skip reasons.
* **No parallel claim semantics**: the rows stay `pending` with no `claimed_by` — the queue
  worker's existing claim path performs the transition (a pre-claim would make every delivery
  skip with `E_TASK_NOT_RUNNABLE`).
* DISABLED: the tick returns `{"status": "off"}` and writes NOTHING — no activity row, no task
  touch, no broker contact.  Both ways: the identical setup flipped ENABLED through
  `PUT /api/v1/config` writes exactly one receipt (so the disabled assertion is not vacuous).
* An empty queue is truthfully "ran 0 of N"; a queue that is pending-but-not-runnable says so.

House rules kept: assertions read the FILE with `sqlite3` (the reader is not the writer); the
database is file-backed and driven on the Celery process loop (`run_async`); Redis is mocked.

RED legs (measured this session on this file — details in the lane receipt): neutralising the
disabled gate (falling through when off) fails ``test_disabled_writes_nothing_and_the_enabling_put_flips_it``;
swapping the helper's ordering to bare `id` fails ``test_enabled_drains_the_top_n_in_the_dispatchers_own_order``;
short-circuiting an empty selection before the receipt fails
``test_empty_queue_is_truthfully_ran_0_of_n``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.models  # noqa: F401  (register every table on Base.metadata before create_all)
from app.config import settings
from app.core.errors import E_CONFIG, PolsiaError
from celery_app.tasks import night_task
from celery_app.tasks._common import run_async

NY = "America/New_York"
UTC_TZ = "UTC"


# ── harness ───────────────────────────────────────────────────────────────────


class NightDB:
    """A file-backed database behind the app-wide session factory.

    `scalar()`/`count()`/`rows()` read the FILE with the stdlib driver — no ORM, no pool, no
    identity map — so an assertion here is an assertion about the row actually on disk.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # ── raw file side (no ORM: what is actually stored) ──────────────────────
    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return self._read(sql, params)

    def scalar(self, sql: str, params: tuple = ()):
        con = sqlite3.connect(str(self.path))
        try:
            row = con.execute(sql, params).fetchone()
            return row[0] if row is not None else None
        finally:
            con.close()

    def count(self, table: str, where: str = "1=1", params: tuple = ()) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table} WHERE {where}", params))

    def activity_rows(self) -> list[dict]:
        return self._read("SELECT action, summary, level, detail FROM activity_log ORDER BY id")

    # ── ORM side (the app's own factory, on the process loop) ────────────────
    def seed_company(self, *, enabled: bool | None = None, hour: int | None = None,
                     limit: int | None = None, tz: str = UTC_TZ) -> None:
        """Insert the single company row through the app's session factory.

        `night_task_hour` has NO default in the frozen schema, so an explicit `None` stores a real
        NULL (unlike `daily_cycle_hour`, whose column default is 6 — measured in
        `test_daily_cycle_window.py`).  `enabled=None` omits the flag so the column default
        (FALSE) applies — the pre-migration row shape.
        """
        from app.core.database import get_session_factory
        from app.models.company import CompanyConfig

        kwargs = {"name": "Test Co", "timezone": tz, "night_task_hour": hour}
        if enabled is not None:
            kwargs["night_task_enabled"] = enabled
        if limit is not None:
            kwargs["night_task_limit"] = limit

        async def _make() -> None:
            async with get_session_factory()() as session:
                session.add(CompanyConfig(**kwargs))
                await session.commit()

        run_async(_make())

    def add_task(self, *, title: str, agent_type: str = "finance", priority: int = 3,
                 scheduled_date: datetime | None = None,
                 created_at: datetime | None = None, status: str = "pending") -> int:
        """Insert one queue row with a CONTROLLED created_at (for the ordering pin)."""
        from app.core.database import get_session_factory
        from app.models.task import Task

        async def _make() -> int:
            async with get_session_factory()() as session:
                task = Task(
                    title=title,
                    agent_type=agent_type,
                    priority=priority,
                    status=status,
                    scheduled_date=scheduled_date,
                    created_at=created_at or datetime(2026, 10, 12, 0, 0),
                )
                session.add(task)
                await session.commit()
                return int(task.id)

        return run_async(_make())

    def task_row(self, task_id: int) -> dict:
        rows = self._read("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else {}


class BrokerRecorder:
    """Stands in for `celery.group` AND the single-delivery `run_agent_task.delay` path.

    `_common.enqueue_created` imports `group` from the celery package and `run_agent_task` from
    `celery_app.tasks.agent_tasks` at CALL time, so both patch targets are module attributes.
    """

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.delays: list[int] = []

    def __call__(self, sigs):
        self.batches.append(list(sigs))
        return self

    def apply_async(self) -> None:
        pass


def _install_broker_recorder(monkeypatch) -> BrokerRecorder:
    recorder = BrokerRecorder()
    monkeypatch.setattr("celery.group", recorder)
    fake_task = MagicMock()
    fake_task.s = lambda task_id: {"task_id": task_id}
    fake_task.delay = lambda task_id: recorder.delays.append(int(task_id))
    # `enqueue_created` does `from celery_app.tasks.agent_tasks import run_agent_task` inside the
    # function, so the patched module attribute is what the call site resolves.
    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", fake_task)
    return recorder


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the activity publish goes to a mock (same shape as the queue conftest)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture
def night_db(monkeypatch, tmp_path):
    """Point the app-wide session factory at a fresh file-backed SQLite database."""
    from app.core import database

    path = tmp_path / "night_task.sqlite"
    monkeypatch.setattr(database.settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")

    async def _build() -> None:
        await database.dispose_engine()
        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    run_async(_build())
    yield NightDB(path)
    run_async(database.dispose_engine())


def _pin_clock(monkeypatch, now: datetime) -> None:
    """Pin the task's clock via the module attr it imports at module top (daily_cycle pattern)."""
    monkeypatch.setattr(night_task, "now_utc", lambda: now)


def _put_config(payload: dict) -> object:
    """`PUT /api/v1/config` on the process loop, through the REAL `get_db` (no override)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async def _call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.put(
                "/api/v1/config", headers={"X-API-Key": "test-key"}, json=payload
            )

    return run_async(_call())


def _seed_four_rows(db: NightDB) -> list[int]:
    """A queue whose dispatcher order is NOT its id order (ids [3, 2, 4, 1]).

    Order = (priority asc, coalesce(scheduled_date, created_at) asc, id asc):
      * C (id 3): priority 1, scheduled 2026-10-11T06:00 -> coalesce 10-11T06:00  (first)
      * B (id 2): priority 1, no schedule, created 2026-10-12T12:00             (second)
      * D (id 4): priority 3, no schedule, created 2026-10-09T00:00             (third)
      * A (id 1): priority 5, no schedule, created 2026-10-10T00:00             (last)
    A naive id-order or (priority, id) reader produces a DIFFERENT list.
    """
    a = db.add_task(title="A-low", priority=5, created_at=datetime(2026, 10, 10, 0, 0))
    b = db.add_task(title="B-high", priority=1, created_at=datetime(2026, 10, 12, 12, 0))
    c = db.add_task(
        title="C-high-scheduled", priority=1, scheduled_date=datetime(2026, 10, 11, 6, 0),
        created_at=datetime(2026, 10, 12, 18, 0),
    )
    d = db.add_task(title="D-mid", priority=3, created_at=datetime(2026, 10, 9, 0, 0))
    assert (a, b, c, d) == (1, 2, 3, 4)
    return [a, b, c, d]


# ── tests ─────────────────────────────────────────────────────────────────────


def test_enabled_drains_the_top_n_in_the_dispatchers_own_order(night_db, monkeypatch):
    """ENABLED + in-window: the TOP-N in `claim_task`'s order, ONE receipt, rows untouched.

    Pins: 4 pending rows; limit 3 -> exactly `[3, 2, 4]` (the dispatcher's own order, NOT id
    order); ONE group fan-out with those ids; the rows stay `pending` with no `claimed_by` (the
    worker's claim path owns the transition — no parallel semantics); exactly ONE activity row,
    `night_task_ran`, summary "Night task ran 3 of 3", titles in order; a skip reason names the
    one row left beyond the limit.  RED leg: swapping the helper's ordering to bare `id` fails
    this test (the ids would read [1, 2, 3]).
    """
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    night_db.seed_company(enabled=True, hour=23, limit=3)
    _seed_four_rows(night_db)
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 23, 30, tzinfo=UTC))
    recorder = _install_broker_recorder(monkeypatch)

    result = night_task.run_night_task()

    assert result["status"] == "completed"
    assert result["ran"] == 3
    assert result["task_ids"] == [3, 2, 4], "the dispatcher's own order, not id order"
    assert result["titles"] == ["C-high-scheduled", "B-high", "D-mid"]
    assert result["window"] == {
        "hour": 23,
        "source": "company",
        "tz": "UTC",
        "start": datetime(2026, 10, 12, 23, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 10, 13, 0, tzinfo=UTC).isoformat(),
    }
    assert recorder.batches == [[{"task_id": 3}, {"task_id": 2}, {"task_id": 4}]]
    assert recorder.delays == []

    # File truth: nothing claimed, nothing moved — the existing claim path stays the one writer.
    for task_id in (1, 2, 3, 4):
        row = night_db.task_row(task_id)
        assert row["status"] == "pending", task_id
        assert row["claimed_by"] is None, task_id
        assert row["attempts"] == 0, task_id

    # Exactly ONE receipt, carrying summary + titles + skip reasons.
    activities = night_db.activity_rows()
    assert len(activities) == 1
    entry = activities[0]
    assert entry["action"] == "night_task_ran"
    assert entry["summary"] == "Night task ran 3 of 3"
    detail = json.loads(entry["detail"])
    assert detail["task_ids"] == [3, 2, 4]
    assert detail["titles"] == ["C-high-scheduled", "B-high", "D-mid"]
    assert detail["limit"] == 3 and detail["ran"] == 3
    assert detail["pending_total"] == 4 and detail["runnable_total"] == 4
    assert detail["skipped"] == ["1 more runnable pending task(s) beyond the limit of 3"]
    assert detail["window"]["hour"] == 23


def test_disabled_writes_nothing_and_the_enabling_put_flips_it(night_db, monkeypatch):
    """Both ways: OFF is zero writes (not vacuous), then `PUT /config` flips it ON and it writes.

    Case 1 — the row's flag is the column default FALSE (the post-0007 state of every existing
    deployment): the tick returns exactly `{"status": "off"}`; tasks untouched, activity_log
    EMPTY, no broker contact.
    Case 2 (the control, same database): `PUT /api/v1/config {"night_task_enabled": true,
    "night_task_limit": 2}` — which also proves the new fields pass through the un-whitelisted
    PUT route — the FILE row stores enabled=1/limit=2, and the next tick writes exactly ONE
    receipt "ran 2 of 2" and fans out 2 ids.
    RED leg: removing the disabled gate (falling through when off) fails case 1.
    """
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    monkeypatch.setattr(settings, "api_key", "test-key")
    night_db.seed_company(hour=23, limit=3)  # enabled omitted -> column default FALSE
    _seed_four_rows(night_db)
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 23, 30, tzinfo=UTC))
    recorder = _install_broker_recorder(monkeypatch)

    # case 1 — disabled: exactly {"status": "off"}, zero writes.
    assert night_db.scalar("SELECT night_task_enabled FROM company_config") == 0
    assert night_task.run_night_task() == {"status": "off"}
    assert night_db.count("activity_log") == 0
    assert night_db.count("tasks") == 4
    assert night_db.scalar("SELECT count(*) FROM tasks WHERE status != 'pending'") == 0
    assert recorder.batches == [] and recorder.delays == []

    # case 2 — the SAME setup, enabled through the API (the passthrough proof).
    response = _put_config({"night_task_enabled": True, "night_task_limit": 2})
    assert response.status_code == 200, response.text[:300]
    assert response.json()["night_task_enabled"] is True
    assert response.json()["night_task_limit"] == 2
    # House rule: the reader is not the writer — assert the ROW on disk.
    assert night_db.scalar("SELECT night_task_enabled FROM company_config") == 1
    assert night_db.scalar("SELECT night_task_limit FROM company_config") == 2

    result = night_task.run_night_task()
    assert result["status"] == "completed"
    assert result["ran"] == 2 and result["limit"] == 2
    assert result["titles"] == ["C-high-scheduled", "B-high"]
    assert recorder.batches == [[{"task_id": 3}, {"task_id": 2}]]
    activities = night_db.activity_rows()
    assert len(activities) == 1
    assert activities[0]["summary"] == "Night task ran 2 of 2"


def test_empty_queue_is_truthfully_ran_0_of_n(night_db, monkeypatch):
    """An empty (or not-yet-runnable) queue is reported honestly — never padded, never silent.

    Case 1 — no rows at all: the receipt STILL exists (a night tick happened), summary
    "Night task ran 0 of 3", `skipped == ["the queue was empty"]`, no broker contact.
    Case 2 — one row scheduled in the FUTURE (outside the claim filter): "ran 0 of 3" with the
    measured reason "not runnable yet", the row untouched.
    RED leg: short-circuiting an empty selection before the receipt fails this test.
    """
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    night_db.seed_company(enabled=True, hour=23, limit=3)
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 23, 30, tzinfo=UTC))
    recorder = _install_broker_recorder(monkeypatch)

    # case 1 — truly empty.
    result = night_task.run_night_task()
    assert result["status"] == "completed"
    assert result["ran"] == 0 and result["task_ids"] == []
    assert result["skipped"] == ["the queue was empty"]
    activities = night_db.activity_rows()
    assert len(activities) == 1
    assert activities[0]["summary"] == "Night task ran 0 of 3"
    assert json.loads(activities[0]["detail"])["skipped"] == ["the queue was empty"]
    assert recorder.batches == [] and recorder.delays == []

    # case 2 — pending but not runnable yet.
    future_id = night_db.add_task(title="Later", scheduled_date=datetime(2099, 1, 1, 0, 0))
    result = night_task.run_night_task()
    assert result["ran"] == 0
    assert result["skipped"] == [
        "1 pending task(s) are not runnable yet (scheduled in the future)"
    ]
    assert night_db.task_row(future_id)["status"] == "pending"
    assert night_db.count("activity_log") == 2  # one receipt per tick, both honest zeros
    assert recorder.batches == [] and recorder.delays == []


def test_unset_hour_defaults_to_23_and_the_edges_0_and_23_gate(night_db, monkeypatch):
    """The unset-hour default, the window gate, and the accepted edge hours 0 and 23.

    Pins: (a) `night_task_hour` NULL + 23:30 local -> runs, window `hour=23 source="default"`;
    (b) stored hour 0 + 00:15 local -> runs (window hour 0 — the other accepted edge);
    (c) stored hour 23 + 22:30 local -> `{"status": "skipped", "reason":
    "outside_night_window", "window": {...}}` and NOTHING written (no receipt, no broker, row
    untouched); the window is still computed and reported.
    """
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    night_db.seed_company(enabled=True, hour=None, limit=1)  # NULL = unset -> 23:00 default
    single_id = night_db.add_task(title="Only", priority=1, created_at=datetime(2026, 10, 12))
    recorder = _install_broker_recorder(monkeypatch)

    # (a) NULL hour resolves to 23:00, reported as the default.
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 23, 30, tzinfo=UTC))
    result = night_task.run_night_task()
    assert result["status"] == "completed" and result["ran"] == 1
    assert result["window"]["hour"] == 23 and result["window"]["source"] == "default"
    assert recorder.delays == [single_id]  # single-row hand-off takes the delay path
    assert night_db.count("activity_log") == 1

    # (b) the hour-0 edge, in its window.
    _write_company_hour(night_db, 0)
    _pin_clock(monkeypatch, datetime(2026, 10, 13, 0, 15, tzinfo=UTC))
    result = night_task.run_night_task()
    assert result["status"] == "completed"
    assert result["window"] == {
        "hour": 0,
        "source": "company",
        "tz": "UTC",
        "start": datetime(2026, 10, 13, 0, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 10, 13, 1, tzinfo=UTC).isoformat(),
    }
    assert night_db.count("activity_log") == 2

    # (c) outside the window: named skip, zero writes.
    _write_company_hour(night_db, 23)
    _pin_clock(monkeypatch, datetime(2026, 10, 13, 22, 30, tzinfo=UTC))
    before = night_db.count("activity_log")
    result = night_task.run_night_task()
    assert result["status"] == "skipped"
    assert result["reason"] == "outside_night_window"
    assert result["window"]["hour"] == 23 and result["window"]["source"] == "company"
    assert night_db.count("activity_log") == before  # nothing written
    assert night_db.scalar("SELECT count(*) FROM tasks WHERE status != 'pending'") == 0


def _write_company_hour(db: NightDB, hour: int) -> None:
    """Change the stored hour through the app's factory (a real writer, not raw SQL)."""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.company import CompanyConfig

    async def _update() -> None:
        async with get_session_factory()() as session:
            row = (await session.execute(select(CompanyConfig))).scalar_one()
            row.night_task_hour = hour
            await session.commit()

    run_async(_update())


def test_invalid_stored_hour_or_limit_is_a_named_refusal_with_no_writes(night_db, monkeypatch):
    """Malformed stored values refuse BY NAME (E_CONFIG) — no silent fallback, no partial writes.

    Pins: stored hour 99 -> `PolsiaError.code == E_CONFIG`, message names 99 and the 0-23 range;
    stored limit 6 -> E_CONFIG naming 6 and "1-5"; in BOTH cases nothing was written (activity
    empty) and the queue is untouched — the refusal happens before any selection or fan-out.
    """
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 23, 30, tzinfo=UTC))
    recorder = _install_broker_recorder(monkeypatch)

    night_db.seed_company(enabled=True, hour=99, limit=3)
    night_db.add_task(title="Queued")
    with pytest.raises(PolsiaError) as excinfo:
        night_task.run_night_task()
    assert excinfo.value.code == E_CONFIG
    assert "99" in excinfo.value.message
    assert "0-23" in excinfo.value.message
    assert night_db.count("activity_log") == 0
    assert night_db.scalar("SELECT count(*) FROM tasks WHERE status != 'pending'") == 0
    assert recorder.batches == [] and recorder.delays == []

    _write_company_hour(night_db, 23)
    _set_company_limit(night_db, 6)  # the wire refuses 6; a stored 6 is the runtime's problem
    with pytest.raises(PolsiaError) as excinfo:
        night_task.run_night_task()
    assert excinfo.value.code == E_CONFIG
    assert "6" in excinfo.value.message and "1-5" in excinfo.value.message
    assert night_db.count("activity_log") == 0
    assert recorder.batches == [] and recorder.delays == []


def _set_company_limit(db: NightDB, limit: int) -> None:
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models.company import CompanyConfig

    async def _update() -> None:
        async with get_session_factory()() as session:
            row = (await session.execute(select(CompanyConfig))).scalar_one()
            row.night_task_limit = limit
            await session.commit()

    run_async(_update())


def test_helper_bounds_and_read_only_selection(night_db):
    """`claimable_task_ids` bounds and its read-only contract.

    Pins: `limit` 0 / -1 / True (bool is an int subclass) / 2.0 all raise ValueError; limit 1
    returns exactly the dispatcher's first id; the selection never mutates a row (all four stay
    `pending`, `attempts` 0, `claimed_by` NULL); a large limit returns everything runnable.
    """
    from app.core.database import get_session_factory
    from app.services.task_service import claimable_task_ids

    _seed_four_rows(night_db)

    async def _call(limit):
        async with get_session_factory()() as db:
            return await claimable_task_ids(db, limit=limit)

    for bad in (0, -1, True, 2.0):
        with pytest.raises(ValueError):
            run_async(_call(bad))

    assert run_async(_call(1)) == [3]
    assert run_async(_call(3)) == [3, 2, 4]
    assert run_async(_call(99)) == [3, 2, 4, 1]

    for task_id in (1, 2, 3, 4):
        row = night_db.task_row(task_id)
        assert row["status"] == "pending" and row["attempts"] == 0
        assert row["claimed_by"] is None


def test_put_wire_validators_and_boundaries_for_the_new_fields(night_db, monkeypatch):
    """The wire half (schemas/company.py): 1..5 / 0..23 bounds, booleans, blank-email = unset.

    Pins: `night_task_limit` 0 and 6 -> 422 naming the field, row untouched; 1 and 5 accepted;
    `night_task_hour` 0 and 23 accepted, 24 -> 422; `notify_on_completed: true` stored as 1;
    `notification_email: "   "` normalises to NULL (the documented "unconfigured" state); the
    GET/response carries every new field.
    """
    monkeypatch.setattr(settings, "api_key", "test-key")
    night_db.seed_company(hour=23, limit=3)

    for bad in (0, 6):
        response = _put_config({"night_task_limit": bad})
        assert response.status_code == 422, f"{bad} must be refused: {response.text[:200]}"
        locs = [err["loc"] for err in response.json()["detail"]]
        assert ["body", "night_task_limit"] in locs, locs

    for bad in (24, -1):
        response = _put_config({"night_task_hour": bad})
        assert response.status_code == 422, f"{bad} must be refused: {response.text[:200]}"
        locs = [err["loc"] for err in response.json()["detail"]]
        assert ["body", "night_task_hour"] in locs, locs

    # the legal boundary values land on the row (file-read, not the response).
    response = _put_config({
        "night_task_limit": 5,
        "night_task_hour": 0,
        "notify_on_completed": True,
        "notification_email": "   ",
    })
    assert response.status_code == 200, response.text[:300]
    assert night_db.scalar("SELECT night_task_limit FROM company_config") == 5
    assert night_db.scalar("SELECT night_task_hour FROM company_config") == 0
    assert night_db.scalar("SELECT notify_on_completed FROM company_config") == 1
    assert night_db.scalar("SELECT notification_email FROM company_config") is None

    response = _put_config({"night_task_hour": 23})
    assert response.status_code == 200
    assert night_db.scalar("SELECT night_task_hour FROM company_config") == 23

    # every new field is on the response shape.
    body = response.json()
    for field in (
        "night_task_enabled", "night_task_limit", "night_task_hour", "notification_email",
        "notify_on_completed", "notify_on_failed", "notify_on_awaiting_approval",
        "notify_on_run_requeued",
    ):
        assert field in body, field
