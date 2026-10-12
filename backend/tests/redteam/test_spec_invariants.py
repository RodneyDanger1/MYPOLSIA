"""Red-team suite: proves the SPEC's load-bearing claims and the deliberate
behaviours of the rebuild.  NOT part of the shipped suite - run explicitly:

    cd conformance/backend
    CLAUDE_CLI_MOCK=true <venv>/python -m pytest tests/redteam -v
"""
from __future__ import annotations

import datetime as dt
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core import database
from app.core.timeutil import day_bounds, utc_day_bounds
from app.models.report import ActivityLog

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# 1. ordering tie-break is REQUIRED (created_at has 1s resolution on SQLite)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_created_at_only_ordering_is_wrong(async_db_session):
    """Two rows written in the same second share created_at -> created_at-only
    ordering is non-deterministic; id DESC must break the tie."""
    async_db_session.add(ActivityLog(agent_type="a", action="x", summary="First"))
    async_db_session.add(ActivityLog(agent_type="b", action="y", summary="Second"))
    await async_db_session.commit()

    naive_order = (
        await async_db_session.execute(
            select(ActivityLog).order_by(ActivityLog.created_at.desc())
        )
    ).scalars().all()
    id_order = (
        await async_db_session.execute(
            select(ActivityLog).order_by(
                ActivityLog.created_at.desc(), ActivityLog.id.desc()
            )
        )
    ).scalars().all()

    assert naive_order[0].summary == "First", (
        "expected the SQLite same-second tie to return the WRONG row "
        f"(proving the bug), got {naive_order[0].summary!r}"
    )
    assert id_order[0].summary == "Second", "id DESC tie-break must return the newest row"
    assert naive_order[0].created_at == naive_order[1].created_at, (
        "delay: created_at must collide on a second-granularity default"
    )


# --------------------------------------------------------------------------- #
# 2. the naive-local-midnight day window miscounts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_naive_local_midnight_window_miscounts(async_db_session):
    """Reproduces backend/celery_app/tasks/daily_cycle.py:94
    (`datetime.combine(date.today(), datetime.min.time())`) against UTC-stored
    rows, and shows the corrected company-local (UTC-aware) window."""
    db = async_db_session
    day = date(2026, 10, 11)

    # 2026-10-11 02:00 UTC == 2026-10-10 22:00 in New York  -> YESTERDAY locally
    # 2026-10-11 20:00 UTC == 2026-10-11 16:00 in New York  -> today locally
    db.add(ActivityLog(agent_type="a", action="x", summary="yesterday-local",
                       created_at=datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)))
    db.add(ActivityLog(agent_type="b", action="y", summary="today-local",
                       created_at=datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc)))
    await db.commit()

    original_start = datetime.combine(day, dt.time.min)          # naive LOCAL midnight
    original = (
        await db.execute(
            select(func.count(ActivityLog.id)).where(ActivityLog.created_at >= original_start)
        )
    ).scalar()

    start, end = day_bounds(day, "America/New_York")             # corrected
    corrected = (
        await db.execute(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.created_at >= start, ActivityLog.created_at < end
            )
        )
    ).scalar()

    utc_start, utc_end = utc_day_bounds(day)
    utc_window = (
        await db.execute(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.created_at >= utc_start, ActivityLog.created_at < utc_end
            )
        )
    ).scalar()

    print(f"\n  original naive-local-midnight count = {original}")
    print(f"  corrected company-local count       = {corrected}")
    print(f"  UTC-day count                       = {utc_window}")

    assert start == datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)
    assert corrected == 1, "company-local 2026-10-11 contains exactly the 20:00 UTC row"
    assert utc_window == 2, "a UTC day window contains both rows"
    assert original == 2, (
        "the ORIGINAL window counts the 02:00 UTC row (22:00 the previous day in "
        "New York) as part of 2026-10-11 -> an inflated daily report"
    )


@pytest.mark.asyncio
async def test_tz_aware_bind_drops_offset_on_sqlite(async_db_session):
    """A tz-aware datetime bound into DateTime(timezone=True) keeps its wall clock
    and LOSES its offset on SQLite -> a local-offset bound is compared as if UTC."""
    async_db_session.add(
        ActivityLog(agent_type="a", action="x", summary="offset-probe",
                    created_at=datetime(2026, 10, 11, 0, 0, tzinfo=timezone(timedelta(hours=-5))))
    )
    await async_db_session.commit()
    row = (
        await async_db_session.execute(
            select(ActivityLog).where(ActivityLog.summary == "offset-probe")
        )
    ).scalars().first()

    raw = (
        await async_db_session.execute(
            sa.text("select created_at from activity_log where summary='offset-probe'")
        )
    ).scalar()
    print("\n  python value :", repr(row.created_at))
    print("  raw sqlite   :", repr(raw))
    assert row.created_at.tzinfo is None, "SQLite returns naive datetimes"
    assert raw.startswith("2026-10-11 00:00"), raw


# --------------------------------------------------------------------------- #
# 3. tasks.metadata clashes with declarative Base.metadata
# --------------------------------------------------------------------------- #
def test_metadata_attribute_name_is_reserved():
    """`metadata` as a mapped attribute is a HARD ERROR on SQLAlchemy 2.x, not a warning.

    The first revision of this check asserted a warning — the behaviour the upload's SQLAlchemy-1.x
    habits assumed. On the pinned SQLAlchemy 2.0.36 the declarative API refuses the class outright,
    which is the STRONGER guarantee, so the check now asserts the refusal and then pins how the
    shipped model solves it (attribute `metadata_json`, DB column still literally `metadata`, so the
    migration's column name survives).
    """
    class ProbeBase(DeclarativeBase):
        pass

    with pytest.raises(sa.exc.InvalidRequestError) as excinfo:
        class Shadow(ProbeBase):
            __tablename__ = "shadow_probe"
            id = sa.Column(sa.Integer, primary_key=True)
            metadata = sa.Column("metadata", sa.JSON)

    assert "metadata" in str(excinfo.value), (
        "the refusal must name the offending attribute, not fail anonymously"
    )

    from app.models.task import Task

    assert Task.metadata is Task.__table__.metadata, (
        "the real Task must keep the MetaData object at .metadata"
    )
    assert "metadata" in [c.name for c in Task.__table__.columns]
    assert "metadata_json" in Task.__mapper__.attrs.keys()


# --------------------------------------------------------------------------- #
# 4. engine construction must be lazy (asyncpg absent in the unit venv)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_engine_is_lazy_and_the_driver_is_needed_only_when_used():
    """The engine must be built lazily — and building one must not need a live server.

    The first revision asserted `asyncpg` was ABSENT (`pytest.raises(ModuleNotFoundError)`), which
    tests the machine, not the code: asyncpg 0.30.0 is pinned in requirements.txt and installed in
    this venv, so that assertion could only ever pass on a host that cannot run the documented stack.
    The invariant is that neither importing nor constructing the engine may REQUIRE the driver or a
    server, so this asserts the behaviour that must hold either way and, when the driver really is
    missing, that its absence surfaces as a named refusal.
    """
    import importlib.util

    import app.core.database as db

    assert db._engine is None, "importing app.core.database must not build an engine"
    assert db.Base.metadata.tables, "Base.metadata must be populated by app.models"

    if importlib.util.find_spec("asyncpg") is None:
        with pytest.raises(ModuleNotFoundError) as excinfo:
            create_async_engine("postgresql+asyncpg://u:p@h/db")
        assert "asyncpg" in str(excinfo.value), "the refusal must name the missing driver"
    else:
        # Installed (the shipped pin): building the engine must still not connect to anything.
        engine = create_async_engine("postgresql+asyncpg://u:p@h/db")
        try:
            assert engine.dialect.name == "postgresql"
            assert engine.url.drivername == "postgresql+asyncpg"
        finally:
            await engine.dispose()


# --------------------------------------------------------------------------- #
# 5. auth ordering: 422 for unknown agent even with NO key; 401 for known+no key
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_trigger_ordering_without_auth(api_client):
    unknown_no_key = await api_client.post("/api/v1/agents/nonexistent/trigger")
    known_no_key = await api_client.post("/api/v1/agents/social_media/trigger")
    unknown_with_key = await api_client.post(
        "/api/v1/agents/nonexistent/trigger", headers={"X-API-Key": "test-key"}
    )
    unknown_wrong_key = await api_client.post(
        "/api/v1/agents/nonexistent/trigger", headers={"X-API-Key": "nope"}
    )
    valid = await api_client.post(
        "/api/v1/agents/social_media/trigger", headers={"X-API-Key": "test-key"}
    )
    print("\n  unknown+no-key   ->", unknown_no_key.status_code)
    print("  known+no-key     ->", known_no_key.status_code)
    print("  unknown+key      ->", unknown_with_key.status_code)
    print("  unknown+bad-key  ->", unknown_wrong_key.status_code)
    print("  known+key(no body) ->", valid.status_code, valid.json())

    assert unknown_no_key.status_code == 422
    assert unknown_with_key.status_code == 422
    assert unknown_wrong_key.status_code == 422
    assert known_no_key.status_code == 401
    assert valid.status_code == 202
    assert valid.json()["status"] == "queued"
    assert "queued" in valid.json()["message"]


# --------------------------------------------------------------------------- #
# 6. /health and the Stripe webhook are reachable with no key
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_open_endpoints_need_no_key(api_client):
    assert (await api_client.get("/api/v1/health")).json() == {"status": "ok"}
    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook",
        content=b'{"id":"evt_1"}',
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 500  # reached the handler (no key configured)
    assert resp.status_code != 401


# --------------------------------------------------------------------------- #
# 7. get_db auto-commits on success and rolls back on exception
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_db_commits_and_rolls_back(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'gd.db').as_posix()}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "_session_factory", factory)

    agen = database.get_db()
    session = await agen.__anext__()
    session.add(ActivityLog(agent_type="a", action="commit", summary="ok"))
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    async with factory() as verify:
        committed = (
            await verify.execute(
                select(func.count(ActivityLog.id)).where(ActivityLog.action == "commit")
            )
        ).scalar()
    assert committed == 1, "get_db must commit after a successful yield"

    agen2 = database.get_db()
    session2 = await agen2.__anext__()
    session2.add(ActivityLog(agent_type="a", action="rollback", summary="boom"))
    with pytest.raises(RuntimeError):
        await agen2.athrow(RuntimeError("boom"))
    async with factory() as verify2:
        rolled = (
            await verify2.execute(
                select(func.count(ActivityLog.id)).where(ActivityLog.action == "rollback")
            )
        ).scalar()
    assert rolled == 0, "get_db must roll back when the request raises"
    await engine.dispose()


# --------------------------------------------------------------------------- #
# 8. report idempotency is a real DB invariant, not just an object identity
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_or_create_daily_report_row_count(async_db_session):
    from app.services.report_service import get_or_create_daily_report

    day = date(2026, 5, 5)
    first = await get_or_create_daily_report(async_db_session, day)
    await async_db_session.commit()
    second = await get_or_create_daily_report(async_db_session, day)
    third = await get_or_create_daily_report(async_db_session, day)
    assert first.id == second.id == third.id
    count = (
        await async_db_session.execute(
            select(func.count()).select_from(sa.text("daily_reports"))
        )
    ).scalar()
    assert count == 1


# --------------------------------------------------------------------------- #
# 9. log_activity publishes exactly once, to a channel containing polsia:activity
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_log_activity_publishes_once_and_serialises_iso(async_db_session, mock_redis):
    from app.services.activity_service import log_activity

    await log_activity(async_db_session, agent_type="finance", action="snapshot",
                       summary="s", level="success")
    assert mock_redis.publish.call_count == 1
    channel, payload = mock_redis.publish.call_args[0]
    assert "polsia:activity" in channel
    import json

    body = json.loads(payload)
    assert set(body) >= {"id", "agent_type", "action", "summary", "level", "created_at"}
    dt.datetime.fromisoformat(body["created_at"])


# --------------------------------------------------------------------------- #
# 10. agent registry size is pinned at 9 (docs say 10 - the tests win)
# --------------------------------------------------------------------------- #
def test_agent_registry_is_exactly_nine():
    from app.agents.crew_factory import AGENT_MAP
    from app.services.task_service import VALID_AGENT_TYPES

    assert len(AGENT_MAP) == 9
    assert tuple(AGENT_MAP) == tuple(VALID_AGENT_TYPES)
    assert "social_media" in AGENT_MAP and "finance" in AGENT_MAP
    assert "deployment" not in AGENT_MAP


# --------------------------------------------------------------------------- #
# 11. the model layer compiles to the PostgreSQL DDL the migration declares
# --------------------------------------------------------------------------- #
def test_models_compile_to_pg_types_and_sqlite_types():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    from app.core.database import Base

    pg_ddl = ""
    for table in Base.metadata.sorted_tables:
        pg_ddl += str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect()))
    sqlite_ddl = ""
    for table in Base.metadata.sorted_tables:
        sqlite_ddl += str(sa.schema.CreateTable(table).compile(dialect=sqlite_dialect()))

    assert "JSONB" in pg_ddl, "payload columns must be jsonb on PostgreSQL"
    assert "VARCHAR[]" in pg_ddl, "list columns must be varchar[] on PostgreSQL"
    assert "JSON" in sqlite_ddl and "ARRAY" not in sqlite_ddl
    assert "metadata JSONB" in pg_ddl or "metadata JSON" in pg_ddl
    assert len(Base.metadata.tables) == 17, sorted(Base.metadata.tables)


# --------------------------------------------------------------------------- #
# 12. WS wire shape matches frontend/src/lib/api.ts ActivityEvent
# --------------------------------------------------------------------------- #
def test_activity_wire_shape():
    from app.core.events import activity_payload, serialize_event

    payload = activity_payload(id=1, agent_type="social_media", action="post_tweet",
                               summary="Posted", level="success")
    assert set(payload) >= {"id", "agent_type", "action", "summary", "level", "created_at"}
    import json

    parsed = json.loads(serialize_event(payload))
    assert parsed["level"] == "success"
    assert set(parsed["level"] for _ in [0]) <= {"info", "success", "warning", "error"}
