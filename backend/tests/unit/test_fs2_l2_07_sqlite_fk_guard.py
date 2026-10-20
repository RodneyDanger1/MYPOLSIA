"""PHASE-3 fixer-2 / L2-07 — the alembic chain survives SQLite FK enforcement.

RED before the fix (lane-2 `probe_chain_fk.py`, leg B; re-measured in
`fix-2/raw/premise-probe_chain_fk.txt`): `alembic upgrade head` over a 0002 database SEEDED
with one campaign + one metric fails with `IntegrityError: FOREIGN KEY constraint failed`
whenever `PRAGMA foreign_keys=ON` is in force for the run, because 0003's `_rebuild` drops the
FK parent (`ad_campaigns`) while the child rows still point at it.  The fix disables
enforcement at raw-connect time in `alembic/env.py` (12-step step 1) and runs
`PRAGMA foreign_key_check` after the run (step 10).

The hardened environment is modelled the way research-B's probe modelled it: every engine
created during the run gets a `foreign_keys=ON` connect listener, so the alembic engine's own
connections would enforce FKs were it not for the env guard (which registers later and wins).
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def fk_enforced_engines(monkeypatch):
    """The 'operator enabled FK enforcement' environment: FK-ON for every engine, incl. alembic's."""
    import sqlalchemy.ext.asyncio as sa_async
    from sqlalchemy import event

    real = sa_async.create_async_engine

    def patched(url, **kwargs):  # noqa: ANN001, ANN003
        engine = real(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):  # noqa: ANN001, ANN202
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    monkeypatch.setattr(sa_async, "create_async_engine", patched)
    return real


@pytest.fixture
def alembic_db(monkeypatch, tmp_path):
    """A tmp SQLite database plus the alembic Config env.py will read (settings.database_url)."""
    from app.config import settings

    db_path = tmp_path / "fk_chain.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def _fk_on_connection(db_path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.as_posix())
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _scalar(con: sqlite3.Connection, sql: str):
    return con.execute(sql).fetchone()[0]


def test_chain_upgrade_over_a_seeded_0002_db_works_with_fk_enforcement_on(
    alembic_db, fk_enforced_engines
):
    cfg, db_path = alembic_db
    command.upgrade(cfg, "0002")

    # Seed one campaign + one metric through a connection that ENFORCES FKs, and prove the
    # enforcement is live with the control: an orphan child insert must be refused.
    con = _fk_on_connection(db_path)
    try:
        assert _scalar(con, "PRAGMA foreign_keys") == 1
        con.execute(
            "INSERT INTO ad_campaigns (id, platform, name, status, daily_budget_usd, "
            "total_spent_usd) VALUES (1, 'twitter', 'seeded', 'active', 10.0, 5.0)"
        )
        con.execute(
            "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions, "
            "spend_usd, revenue_usd) VALUES (1, '2026-10-13', 10, 2, 1, 1.005, 3.0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions, "
                "spend_usd, revenue_usd) VALUES (999, '2026-10-13', 0, 0, 0, 0, 0)"
            )
        con.commit()
    finally:
        con.close()

    # The migration run itself: with the env-level guard this completes; without it the
    # 0003 rebuild aborts with `IntegrityError: FOREIGN KEY constraint failed` (RED leg).
    command.upgrade(cfg, "head")

    con = _fk_on_connection(db_path)
    try:
        # W1/T03+T19 -> U5 -> V8 -> V14 -> V22 -> V23 -> V24: the chain head moved 0006 -> ... ->
        # 0011 -> 0012 (`0012_perf_indexes.py`, V24 lane L6, index-only); this pin is a DELIBERATE
        # update (old -> new: "0011" -> "0012"), the guard legs below are unchanged.
        # V25 (L1+L4): the chain head moved 0012 -> 0014 (`0013_task_attachments.py`,
        # `0014_ads_nullable_revenue.py`) — deliberate re-pin; the guard legs below are unchanged.
        # V26/L6: head 0014 -> 0015 (`0015_ads_spend_nullable.py`) — deliberate re-pin.
        # V27/L4: head 0015 -> 0016 (`0016_vision_descriptions.py`) — deliberate re-pin.
        assert _scalar(con, "SELECT version_num FROM alembic_version") == "0016"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _scalar(con, "SELECT COUNT(*) FROM ad_campaigns") == 1
        assert _scalar(con, "SELECT COUNT(*) FROM ad_metrics") == 1
        assert _scalar(con, "SELECT spend_usd FROM ad_metrics WHERE campaign_id = 1") == 1.005
    finally:
        con.close()


def test_downgrade_and_reupgrade_also_survive_fk_enforcement(alembic_db, fk_enforced_engines):
    """`downgrade()` shares `_rebuild`; the guard must cover the whole run, including -1 legs."""
    cfg, db_path = alembic_db
    command.upgrade(cfg, "0002")

    con = _fk_on_connection(db_path)
    try:
        con.execute(
            "INSERT INTO ad_campaigns (id, platform, name, status) VALUES (1, 'twitter', 'x', 'active')"
        )
        con.execute(
            "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions, "
            "spend_usd, revenue_usd) VALUES (1, '2026-10-13', 1, 1, 1, 0.5, 1.0)"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")
    with _fk_on_connection(db_path) as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (con.execute("SELECT COUNT(*) FROM ad_campaigns").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0]) == (1, 1)
    command.upgrade(cfg, "head")
