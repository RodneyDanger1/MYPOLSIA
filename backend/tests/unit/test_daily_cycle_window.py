"""T2 acceptance — the hour written THROUGH the API drives the morning cycle's window (A–D).

What this file pins (the frozen T2 interface; seat S1-core implements exactly this):

* `celery_app.tasks.daily_cycle.run_morning_cycle()` (the sync Celery task, called directly
  in-process) computes its window from the effective hour: `company_config.daily_cycle_hour`
  when stored, else `settings.morning_cycle_hour`; validated 0–23, else `PolsiaError(E_CONFIG)`
  naming the value, the range and the source.
* The window is `[H:00, H+1:00)` in the COMPANY-LOCAL timezone (`core/timeutil.company_tz()`,
  driven by `settings.company_timezone`) as aware UTC instants, carried on the pass result as
  `"window"` = {`hour`, `source` (company|settings), `tz` (zone key), `start`/`end` (ISO-8601
  aware UTC strings)}.
* An out-of-window call returns exactly
  `{"status": "skipped", "reason": "outside_morning_window", "window": {...}}` and does no work.
* A and B write the hour through `PUT /api/v1/config` / direct seeding and prove the SCHEDULER
  picked it up — the file-truth row read (`sqlite3.connect`) is the house rule: the reader must
  not be the writer.

RED legs (planted by the parent on the settled tree; this file must fail under both):

* LEG 1 (the plan's): `app.services.company_service.get_daily_cycle_hour` patched to
  `AsyncMock(return_value=None)` — the old unconditional-settings behaviour. Tests A and C must
  FAIL (A loses the stored hour and falls back to settings; C never refuses).
* LEG 2 (the tz trap): `daily_cycle.company_tz` patched to return UTC — test A must FAIL
  (13:30 America/New_York is 17:30 UTC, outside a UTC 13:00–14:00 window).

Harness facts (measured shapes on this tree; the fixtures are self-contained because fixtures do
not cross directories — the pattern is `tests/unit/celery/conftest.py::queue_db`):

* ONE file-backed SQLite database is seen by BOTH the API and the task: `database.settings.
  database_url` points at a tmp_path file and the real lazy `get_engine()` is rebuilt on the
  celery process loop (`celery_app.tasks._common.run_async`).
* The API leg does NOT override `get_db`: the real dependency resolves through the patched
  factory, so the PUT writes the SAME file the task reads.
* Measured (see seat `raw/probe_orm_none.py`): storing `daily_cycle_hour=None` through the ORM
  lands the column default 6, not NULL — so test B's NULL row is constructed at the file level
  with raw SQL, and the equivalence is asserted against the file, not the ORM's intent.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import app.models  # noqa: F401  (register every table on Base.metadata before create_all)
from app.config import settings
from app.core.errors import E_CONFIG, PolsiaError
from celery_app.tasks import daily_cycle
from celery_app.tasks._common import run_async

NY = ZoneInfo("America/New_York")


# ── harness ───────────────────────────────────────────────────────────────────


class CycleDB:
    """A file-backed database behind the app-wide session factory.

    `scalar()`/`count()` read the FILE with the stdlib driver — no ORM, no pool, no identity
    map — so an assertion here is an assertion about the row actually on disk.
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

    def execute(self, sql: str, params: tuple = ()) -> None:
        """A direct file write (used only for the NULL construction in test B)."""
        con = sqlite3.connect(str(self.path))
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def scalar(self, sql: str, params: tuple = ()):
        con = sqlite3.connect(str(self.path))
        try:
            row = con.execute(sql, params).fetchone()
            return row[0] if row is not None else None
        finally:
            con.close()

    def count(self, table: str) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table}"))

    # ── ORM side (the app's own factory, on the process loop) ────────────────
    def seed_company(self, *, hour: int, tz: str = "America/New_York") -> None:
        """Insert the single company row through the app's session factory."""
        from app.core.database import get_session_factory
        from app.models.company import CompanyConfig

        async def _make() -> None:
            async with get_session_factory()() as session:
                session.add(CompanyConfig(name="Test Co", timezone=tz, daily_cycle_hour=hour))
                await session.commit()

        run_async(_make())

    def stored_hour(self) -> int | None:
        return self.scalar("SELECT daily_cycle_hour FROM company_config WHERE id = 1")


class GroupRecorder:
    """Stands in for the `celery.group` the fan-out helper imports at call time: records the
    fan-out, contacts no broker. (D2: the dispatch moved into `_common.enqueue_created`, which
    does `from celery import group` inside the function — so the patch target is the celery
    package attribute, not a `daily_cycle` module attr.)"""

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.apply_async_count = 0

    def __call__(self, sigs):
        self.batches.append(list(sigs))
        return self

    def apply_async(self) -> None:
        self.apply_async_count += 1


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the activity publish goes to a mock (same shape as the queue conftest)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture
def cycle_db(monkeypatch, tmp_path):
    """Point the app-wide session factory at a fresh file-backed SQLite database."""
    from app.core import database

    path = tmp_path / "cycle_window.sqlite"
    monkeypatch.setattr(database.settings, "database_url", f"sqlite+aiosqlite:///{path.as_posix()}")

    async def _build() -> None:
        await database.dispose_engine()
        engine = database.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    run_async(_build())
    yield CycleDB(path)
    run_async(database.dispose_engine())


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


def _install_agent_spy(monkeypatch) -> MagicMock:
    """A plan with no tasks, so the cycle creates exactly its 3 fixed tasks."""
    import app.agents.crew_factory as crew_factory

    spy = MagicMock(return_value={"summary": "test plan", "tasks": []})
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return spy


def _pin_clock(monkeypatch, now: datetime) -> None:
    """Pin the cycle's clock via the module attr S1-core imports at module top."""
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: now)


def _install_group_recorder(monkeypatch) -> GroupRecorder:
    recorder = GroupRecorder()
    # D2: the fan-out helper (`_common.enqueue_created`) reads `group` from the celery package at
    # call time; patch there. (Was `setattr(daily_cycle, "group", recorder)` before the dispatch
    # moved; assertions unchanged.)
    monkeypatch.setattr("celery.group", recorder)
    return recorder


# ── tests ─────────────────────────────────────────────────────────────────────


def test_a_configured_hour_drives_the_window_through_the_api_round_trip(cycle_db, monkeypatch):
    """The T2 invariant end to end: an hour written through the API drives the computed window.

    Pins: seed row 6 -> `PUT /api/v1/config {"daily_cycle_hour": 13}` -> the FILE row is 13
    (sqlite3, not the response) -> with the clock at 13:30 America/New_York the cycle completes
    and its window is exactly [13:00, 14:00) America/New_York as aware UTC instants; exactly 3
    task rows exist and the group recorder saw one `apply_async`. The settings hour is pinned to
    a DIFFERENT value (3), so a settings-only reader cannot pass this test.

    RED legs (see module docstring): LEG 1 -> falls back to settings, window hour becomes 3 —
    this test must FAIL. LEG 2 (`daily_cycle.company_tz` -> UTC) -> 13:30 New York is 17:30 UTC,
    outside a UTC 13:00–14:00 window, the cycle skips — this test must FAIL.
    """
    monkeypatch.setattr(settings, "company_timezone", "America/New_York")
    monkeypatch.setattr(settings, "morning_cycle_hour", 3)
    monkeypatch.setattr(settings, "api_key", "test-key")

    cycle_db.seed_company(hour=6)

    response = _put_config({"daily_cycle_hour": 13})
    assert response.status_code == 200
    assert response.json()["daily_cycle_hour"] == 13
    # House rule: the reader is not the writer — assert the ROW on disk, not only the response.
    assert cycle_db.stored_hour() == 13

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 13, 30, tzinfo=NY))
    recorder = _install_group_recorder(monkeypatch)
    spy = _install_agent_spy(monkeypatch)

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "completed"
    assert result["window"] == {
        "hour": 13,
        "source": "company",
        "tz": "America/New_York",
        "start": datetime(2026, 10, 12, 13, tzinfo=NY).astimezone(UTC).isoformat(),
        "end": datetime(2026, 10, 12, 14, tzinfo=NY).astimezone(UTC).isoformat(),
    }
    # the brief's literals: start "2026-10-12T17:00:00+00:00", end "2026-10-12T18:00:00+00:00"
    # File truth: exactly the 3 fixed tasks were created, and the fan-out happened once.
    assert cycle_db.count("tasks") == 3
    assert recorder.apply_async_count == 1
    assert len(recorder.batches[0]) == 3
    spy.assert_called()  # the completed cycle really ran its body


def test_an_absent_stored_value_falls_back_to_the_settings_hour(cycle_db, monkeypatch):
    """Precedence, both ways of being absent: no row, and a row whose column stores NULL.

    Case 1 — no company row at all.  Case 2 — a row exists but stores NULL (measured: the ORM
    writes the column default 6 on an explicit `None`, probe `raw/probe_orm_none.py`; the NULL
    branch is therefore constructed with a raw file UPDATE and verified with a sqlite3 read).
    Both cases: clock pinned inside [07:00, 08:00) UTC, settings hour 7, tz UTC — the cycle
    completes and the window says `hour == 7`, `source == "settings"`, `tz == "UTC"`.

    RED leg (LEG 1): with `get_daily_cycle_hour` patched to None this test still passes — it is
    the fallback's own pin, and it must KEEP passing under both planted legs.
    """
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "morning_cycle_hour", 7)

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 7, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    _install_agent_spy(monkeypatch)

    expected_window = {
        "hour": 7,
        "source": "settings",
        "tz": "UTC",
        "start": datetime(2026, 10, 12, 7, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 10, 12, 8, tzinfo=UTC).isoformat(),
    }

    # case 1 — no company row
    result = daily_cycle.run_morning_cycle()
    assert result["status"] == "completed"
    assert result["window"] == expected_window

    # case 2 — a row that stores NULL.
    # DELIBERATE RE-PIN (wave v15 / E3 item 3-02): the morning cycle now holds a DURABLE
    # once-per-(cycle, company-local day) claim (`cycle_claimed` activity row, committed
    # before any spend), so a SECOND call for the same local day is a NAMED skip by design.
    # This test pins the fallback PRECEDENCE, not the claim — pinning the clock to the NEXT
    # day keeps its intent while respecting once-per-day.  The claim's own pins live in
    # tests/unit/celery/test_v15_e3_cycle_claim.py.
    _pin_clock(monkeypatch, datetime(2026, 10, 13, 7, 30, tzinfo=UTC))
    cycle_db.seed_company(hour=6, tz="UTC")
    cycle_db.execute("UPDATE company_config SET daily_cycle_hour = NULL WHERE id = 1")
    assert cycle_db.stored_hour() is None, "the NULL must really be on the file"

    result_null = daily_cycle.run_morning_cycle()
    assert result_null["status"] == "completed"
    assert result_null["window"] == expected_window | {
        "start": datetime(2026, 10, 13, 7, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 10, 13, 8, tzinfo=UTC).isoformat(),
    }


def test_an_invalid_stored_hour_is_a_named_refusal_and_produces_no_window(cycle_db, monkeypatch):
    """An invalid stored hour refuses BY NAME — no window, no work, no silent UTC.

    Pins: row stores 99 (ORM seed, sqlite3-verified) -> `run_morning_cycle()` raises `PolsiaError`
    with `code == E_CONFIG`; the message names the stored value ("99") and the accepted range
    (both "0" and "23" appear); the agent spy was never called; tasks and activity_log gained
    ZERO rows.

    RED leg (LEG 1): with `get_daily_cycle_hour` patched to None the stored 99 is never seen, no
    refusal is raised and the cycle completes — this test must FAIL.
    """
    monkeypatch.setattr(settings, "company_timezone", "America/New_York")
    monkeypatch.setattr(settings, "morning_cycle_hour", 7)

    cycle_db.seed_company(hour=99)
    assert cycle_db.stored_hour() == 99

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 13, 30, tzinfo=NY))
    recorder = _install_group_recorder(monkeypatch)
    spy = _install_agent_spy(monkeypatch)

    with pytest.raises(PolsiaError) as excinfo:
        daily_cycle.run_morning_cycle()

    assert excinfo.value.code == E_CONFIG
    message = excinfo.value.message
    assert "99" in message, "the refusal names the stored value"
    assert "0" in message and "23" in message, "the refusal names the accepted range"
    assert spy.call_count == 0, "no work may happen under a refused configuration"
    assert recorder.apply_async_count == 0, "no fan-out without a window"
    assert cycle_db.count("tasks") == 0
    assert cycle_db.count("activity_log") == 0


def test_outside_the_window_the_cycle_skips_with_no_side_effects(cycle_db, monkeypatch):
    """Outside its window the cycle skips explicitly and touches nothing.

    Pins: row hour 13 America/New_York; clock at 09:30 local (outside [13:00, 14:00)); the result
    is EXACTLY `{"status": "skipped", "reason": "outside_morning_window", "window": {...}}` with
    the window still computed and reported; the agent spy was never called; tasks and
    activity_log gained ZERO rows.
    """
    monkeypatch.setattr(settings, "company_timezone", "America/New_York")
    monkeypatch.setattr(settings, "morning_cycle_hour", 3)

    cycle_db.seed_company(hour=13)
    assert cycle_db.stored_hour() == 13

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 9, 30, tzinfo=NY))
    recorder = _install_group_recorder(monkeypatch)
    spy = _install_agent_spy(monkeypatch)

    result = daily_cycle.run_morning_cycle()

    assert result == {
        "status": "skipped",
        "reason": "outside_morning_window",
        "window": {
            "hour": 13,
            "source": "company",
            "tz": "America/New_York",
            "start": datetime(2026, 10, 12, 13, tzinfo=NY).astimezone(UTC).isoformat(),
            "end": datetime(2026, 10, 12, 14, tzinfo=NY).astimezone(UTC).isoformat(),
        },
    }
    assert spy.call_count == 0
    assert recorder.apply_async_count == 0
    assert cycle_db.count("tasks") == 0
    assert cycle_db.count("activity_log") == 0
