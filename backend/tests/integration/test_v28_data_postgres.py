"""V28/L5 — the PostgreSQL legs: chain from zero, cascades, NULL-vs-0 (integration only).

UNRUN locally: this lane is forbidden to use docker, and the integration conftest starts
PostgreSQL through testcontainers.  CI's `integration.yml` is the execution path; the same
assertions run on SQLite in `tests/unit/test_v28_data_{migrations,cascades,spend}.py`.

The module creates its OWN database inside the container (`v28_l5`) so it always starts from an
EMPTY schema: the shared session container may already hold `integration_db`'s `create_all`
tables, and a chain-from-zero run must not be contaminated by them.
"""
from __future__ import annotations

import asyncio
import contextlib
import pathlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]
HEAD = "0016"
EXPECTED_TABLE_COUNT = 24

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _create_database(postgres_url: str) -> str:
    """A dedicated empty database for this module; returns its async URL."""
    import asyncpg

    name = f"v28_l5_{uuid.uuid4().hex[:8]}"
    connection = await asyncpg.connect(_asyncpg_dsn(postgres_url))
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    base = postgres_url.rsplit("/", 1)[0]
    return f"{base}/{name}"


@pytest.fixture(scope="session")
def pg_url(postgres_url):
    """The module's own database, migrated from ZERO to head (sync: alembic calls asyncio.run)."""
    from app.config import settings

    url = asyncio.run(_create_database(postgres_url))
    previous = settings.database_url
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    settings.database_url = url  # `alembic/env.py` reads this, not cfg's option
    try:
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = previous
    yield url

    async def _drop() -> None:
        import asyncpg

        connection = await asyncpg.connect(_asyncpg_dsn(postgres_url))
        try:
            await connection.execute(f'DROP DATABASE IF EXISTS "{url.rsplit("/", 1)[1]}"')
        finally:
            await connection.close()

    with contextlib.suppress(Exception):
        asyncio.run(_drop())


def _engine(url: str):
    return create_async_engine(url, echo=False)


async def _tables(url: str) -> tuple[str, set[str]]:
    engine = _engine(url)
    try:
        async with engine.connect() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            tables = {row[0] for row in rows} - {"alembic_version"}
        return str(version), tables
    finally:
        await engine.dispose()


async def test_the_pg_chain_from_zero_reaches_0016_with_24_tables(pg_url) -> None:
    version, tables = await _tables(pg_url)
    assert version == HEAD
    assert len(tables) == EXPECTED_TABLE_COUNT, f"expected 24 tables, found {sorted(tables)}"
    assert {"task_attachments", "vision_descriptions", "agent_runs", "email_logs"} <= tables


async def test_the_pg_cascades_match_the_sqlite_chain(pg_url) -> None:
    engine = _engine(pg_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, title, agent_type, status) VALUES "
                    "(9001, 'tagged', 'finance', 'pending'), (9002, 'other', 'finance', 'pending')"
                )
            )
            for attachment_id, task_id in ((9001, 9001), (9002, 9001), (9003, 9002)):
                await connection.execute(
                    text(
                        "INSERT INTO task_attachments (id, task_id, filename, stored_name, mime, "
                        "size_bytes, sha256) VALUES (:id, :task, 'a.png', :stored, 'image/png', 3, :sha)"
                    ),
                    {"id": attachment_id, "task": task_id, "stored": f"{attachment_id}.png",
                     "sha": "a" * 64},
                )
                await connection.execute(
                    text(
                        "INSERT INTO vision_descriptions (attachment_id, task_id, sha256, description) "
                        "VALUES (:id, :task, :sha, 'a red square')"
                    ),
                    {"id": attachment_id, "task": task_id, "sha": "a" * 64},
                )
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tasks WHERE id = 9001"))
            attachments = (
                await connection.execute(text("SELECT id FROM task_attachments ORDER BY id"))
            ).scalars().all()
            descriptions = (
                await connection.execute(text("SELECT attachment_id FROM vision_descriptions"))
            ).scalars().all()
        assert list(attachments) == [9003], "PostgreSQL must cascade task → attachments"
        assert list(descriptions) == [9003], "task → vision descriptions must cascade too"
    finally:
        await engine.dispose()


async def test_pg_spend_null_and_zero_stay_distinct(pg_url) -> None:
    engine = _engine(pg_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO ad_campaigns (id, platform, name) VALUES (9100, 'google_ads', 'c')")
            )
            await connection.execute(
                text(
                    "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions) "
                    "VALUES (1, 9100, '2026-10-01', 100, 10, 1)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
                    "spend_usd, revenue_usd) VALUES (2, 9100, '2026-10-02', 100, 10, 1, 0, 0)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
                    "spend_usd, revenue_usd) VALUES (3, 9100, '2026-10-03', 100, 10, 1, NULL, 10)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
                    "spend_usd, revenue_usd) VALUES (4, 9100, '2026-10-04', 100, 10, 1, 2, 10)"
                )
            )
        async with engine.connect() as connection:
            rows = {
                row.id: row
                for row in (
                    await connection.execute(
                        text("SELECT id, spend_usd, cpc, roas FROM ad_metrics ORDER BY id")
                    )
                )
            }
        assert rows[1].spend_usd is None, "an omitted spend is NULL on PostgreSQL too"
        assert rows[2].spend_usd == 0, "a measured zero stays zero"
        assert rows[3].spend_usd is None
        assert rows[1].cpc is None and rows[3].cpc is None, "no spend → no CPC"
        assert float(rows[2].cpc) == 0.0
        assert rows[2].roas is None and rows[3].roas is None
        assert float(rows[4].cpc) == pytest.approx(0.2)
        assert float(rows[4].roas) == pytest.approx(5.0)
    finally:
        await engine.dispose()


async def test_pg_agent_runs_survive_a_task_delete_with_a_null_task_id(pg_url) -> None:
    """PostgreSQL contract (migration 0002 §6): `agent_runs.task_id` is ON DELETE SET NULL.

    SQLite cannot alter an FK, so its create_all metadata still says NO ACTION — that is why this
    case is pinned on the engine that actually runs the product (V28/L5's original assertion
    assumed the SQLite metadata and was wrong on PG).
    """
    engine = _engine(pg_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, title, agent_type, status) VALUES "
                    "(9200, 'with a run', 'finance', 'pending')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_runs (id, task_id, agent_type, status) "
                    "VALUES (9200, 9200, 'finance', 'completed')"
                )
            )
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tasks WHERE id = 9200"))
        async with engine.connect() as connection:
            task_id = (
                await connection.execute(
                    text("SELECT task_id FROM agent_runs WHERE id = 9200")
                )
            ).scalar_one()
        assert task_id is None, "the run must survive the task delete with a NULL task_id"
    finally:
        await engine.dispose()
