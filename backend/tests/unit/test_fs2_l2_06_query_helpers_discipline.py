"""PHASE-3 fixer-2 / L2-06 — every converted read refuses `E_DB`, never a codeless 500.

RED before the fix (lane-2 `probe_lane2.py` part D): pointed at an unopenable database, the
seven raw `db.execute` read sites escaped as `OperationalError` while `fetch_scalar` raised
`PolsiaError(E_DB)` — `deps.refusal_guard` translates only `PolsiaError`, so the raw sites
reached the client as FastAPI's bare, codeless 500.  Each site now goes through
`query_helpers.fetch_scalar`/`fetch_scalars`, which is this test's pin.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import E_DB, PolsiaError
from app.services import (
    company_service,
    memory_service,
    social_service,
    support_inbox_service,
)


def _broken_factory(tmp_path):
    """An aiosqlite engine whose DB path's parent does not exist -> cannot open."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'no_such_dir_marker' / 'probe.db').as_posix()}"
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("support inbox items", lambda db: support_inbox_service.list_items(db)),
        ("company config", lambda db: company_service.get_company(db)),
        ("company", lambda db: company_service.get_company_by_name(db, "Acme")),
        ("social post", lambda db: social_service.get_post(db, 1)),
        ("memory", lambda db: memory_service.get_memory(db, 1)),
        # T13/D20 removed `task_service.get_agent_run` and
        # `finance_service.get_latest_revenue_snapshot` as labelled zero-callers (fix-5 §4), so
        # their two rows leave this pin with them.  The discipline they covered — a converted read
        # refusing `E_DB` instead of a codeless 500 — stays pinned by the five rows above (and by
        # every other `fetch_scalar`/`fetch_scalars` caller in the tree).
    ],
)
async def test_converted_reads_refuse_e_db(tmp_path, label, call):
    engine, factory = _broken_factory(tmp_path)
    try:
        async with factory() as db:  # one session per call: a failed execute poisons a session
            with pytest.raises(PolsiaError) as excinfo:
                await call(db)
    finally:
        await engine.dispose()

    refusal = excinfo.value
    assert refusal.code == E_DB, f"{label}: expected E_DB, got {refusal.code!r}"
    assert label in refusal.message, (
        f"the refusal must name what could not be read; got: {refusal.message!r}"
    )
    assert label in str(refusal.context.get("query", "")), (
        f"the what= label must reach the refusal context; got: {refusal.context!r}"
    )
