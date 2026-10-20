"""V26/L6 — ads spend is MEASURED or ABSENT: nullable at rest without a default (0015).

0014 gave `revenue_usd` the two states it needs but deliberately left `spend_usd` alone: the
column was already nullable, yet it still carried `DEFAULT '0'`, so a writer that OMITTED spend
at rest got a stored zero and the ads surface could not tell "we never saw a spend figure" from
"we measured a $0.00 day".  `ads_service.record_ads_run` even said so in a comment ("spend_usd
keeps the pre-existing shape: omitted -> the column's own default") — the bug in code form.

This file pins the completed contract (migration 0015 + model/service alignment):

  1. `ad_metrics.spend_usd` is NULLABLE with NO server default and no Python default; existing
     `0` rows are backfilled to NULL (the same conservative rule as revenue: an old stored zero
     was never certified as a measurement); downgrade restores `DEFAULT 0` after
     `COALESCE(spend_usd, 0)`.  Revenue keeps the 0014 shape on both legs.
  2. `record_ads_run` persists an EXPLICIT NULL when the run omits spend and a measured `0` when
     the run reports `0` — never one as the other.
  3. The API serves NULL as `null` and a measured `0` as `0.0`.

RED on the base tree (measured): there is no `0015_ads_spend_nullable.py`, the ORM still maps
`spend_usd` with `server_default="0"`/`default=0`, and a run that omits spend stores `0`.
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

import app.config as app_config
from app.models.ads import AdCampaign, AdMetric

BACKEND = Path(__file__).resolve().parents[2]
VERSIONS = BACKEND / "alembic" / "versions"
HEADERS = {"X-API-Key": "test-key"}


# ── 1. the migration file: graph contract + reversible SQLite round trip ──────────────────────


def _load_migration(name: str = "0015_ads_spend_nullable"):
    path = VERSIONS / f"{name}.py"
    assert path.exists(), f"migration {name} is missing"
    spec = importlib.util.spec_from_file_location(f"v26_ads_spend_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revision_graph() -> dict[str, str | None]:
    graph: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', source, re.MULTILINE)
        down = re.search(
            r'^down_revision[^=\n]*=\s*(?:"([^"]+)"|None)',
            source,
            re.MULTILINE,
        )
        if revision:
            graph[revision.group(1)] = down.group(1) if down and down.group(1) else None
    return graph


def test_migration_0015_is_child_of_0014_and_the_single_head() -> None:
    graph = _revision_graph()
    assert "0015" in graph, "migration 0015 is missing"
    assert graph["0015"] == "0014", "0015's down_revision must be 0014 (one chain)"
    assert "0015" not in set(graph.values()), "another revision revises 0015 — the chain forks"


def _pre_0015_sqlite_db(path) -> None:
    """A database in the PRE-0015 shape (0014 head), seeded through 0014's own upgrade DDL.

    `0014._UPGRADE_FINAL_DDL` IS the shape 0015 upgrades from (spend DEFAULT '0', revenue
    nullable, measured-only roas), so seeding through it measures the real upgrade path instead
    of a hand-copied schema.
    """
    migration_0014 = _load_migration("0014_ads_nullable_revenue")
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE ad_campaigns (\n"
            "\tid INTEGER NOT NULL, \n"
            "\tplatform VARCHAR(50) NOT NULL, \n"
            "\texternal_id VARCHAR(255), \n"
            "\tname VARCHAR(512) NOT NULL, \n"
            "\tgoal VARCHAR(255), \n"
            "\tstatus VARCHAR(50) DEFAULT 'active', \n"
            "\tdaily_budget_usd NUMERIC(14, 4) DEFAULT '0', \n"
            "\ttotal_spent_usd NUMERIC(14, 4) DEFAULT '0', \n"
            "\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n"
            "\tPRIMARY KEY (id)\n"
            ")"
        )
        con.execute(migration_0014._UPGRADE_FINAL_DDL)
        for index_sql in migration_0014._INDEXES:
            con.execute(index_sql)
        con.execute("INSERT INTO ad_campaigns (id, platform, name) VALUES (1, 'google_ads', 'c1')")
        rows = [
            # id, spend, revenue — a stored spend 0 is the OLD column default, never certified
            (1, 0.0, 10.0),
            (2, 2.5, 5.0),
            (3, 0.0, 0.0),
            (4, None, None),
        ]
        for row_id, spend, revenue in rows:
            con.execute(
                "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
                "spend_usd, revenue_usd) VALUES (?, 1, ?, 100, 10, 1, ?, ?)",
                (row_id, f"2026-10-0{row_id}", spend, revenue),
            )
        con.commit()
    finally:
        con.close()


def _run_ops(engine, fn) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            fn()


def _table_info(con: sqlite3.Connection) -> dict[str, dict]:
    return {
        row[1]: {"type": row[2], "notnull": bool(row[3]), "default": row[4]}
        for row in con.execute("PRAGMA table_info(ad_metrics)")
    }


def test_upgrade_drops_spend_default_and_backfills_zero_to_null(tmp_path) -> None:
    from sqlalchemy import create_engine

    db_path = tmp_path / "pre0015.db"
    _pre_0015_sqlite_db(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    module = _load_migration()
    try:
        _run_ops(engine, module.upgrade)
    finally:
        engine.dispose()

    con = sqlite3.connect(db_path)
    try:
        info = _table_info(con)
        assert info["spend_usd"]["notnull"] is False, "spend_usd stays nullable"
        assert info["spend_usd"]["default"] is None, "no server default: absence is explicit NULL"
        # 0015 must NOT undo 0014's revenue shape.
        assert info["revenue_usd"]["notnull"] is False
        assert info["revenue_usd"]["default"] is None

        values = dict(con.execute("SELECT id, spend_usd FROM ad_metrics").fetchall())
        assert values[1] is None, "a stored 0 from the old default is NOT a measurement"
        assert values[2] == 2.5, "a measured 2.5000 is preserved exactly"
        assert values[3] is None
        assert values[4] is None

        revenue = dict(con.execute("SELECT id, revenue_usd FROM ad_metrics").fetchall())
        assert revenue[1] == 10.0, "0015 does not touch revenue"
        assert revenue[3] == 0.0, "a measured revenue zero survives (0014's rule)"
        assert revenue[4] is None

        assert con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0] == 4, "no row lost"

        # cpc is GENERATED from spend: an absent spend must not invent $0.00/click.
        cpc = dict(con.execute("SELECT id, cpc FROM ad_metrics").fetchall())
        assert cpc[1] is None, "no spend -> no CPC, not 0"
        assert float(cpc[2]) == pytest.approx(0.25)

        stored_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ad_metrics'"
        ).fetchone()[0]
        assert "spend_usd NUMERIC(14, 4)," in stored_sql
        assert "spend_usd NUMERIC(14, 4) DEFAULT" not in stored_sql
        assert "revenue_usd NUMERIC(14, 4)," in stored_sql

        index_names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ad_metrics'"
            )
        }
        assert {"ix_ad_metrics_date_campaign", "uq_ad_metrics_campaign_date"} <= index_names
    finally:
        con.close()


def test_downgrade_restores_default_zero_and_keeps_revenue_nullable(tmp_path) -> None:
    from sqlalchemy import create_engine

    db_path = tmp_path / "roundtrip.db"
    _pre_0015_sqlite_db(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    module = _load_migration()
    try:
        _run_ops(engine, module.upgrade)
        _run_ops(engine, module.downgrade)
    finally:
        engine.dispose()

    con = sqlite3.connect(db_path)
    try:
        info = _table_info(con)
        assert info["spend_usd"]["notnull"] is False
        assert info["spend_usd"]["default"] == "'0'", "the pre-0015 default is restored"
        # 0015's downgrade restores SPEND only; revenue stays at 0014's shape.
        assert info["revenue_usd"]["notnull"] is False
        assert info["revenue_usd"]["default"] is None

        values = dict(con.execute("SELECT id, spend_usd FROM ad_metrics").fetchall())
        assert values[1] == 0.0, "a NULL is restored as the pre-0015 default, not lost"
        assert values[2] == 2.5
        assert con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0] == 4
    finally:
        con.close()

    # up / down / up must end in the upgraded shape again.
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        _run_ops(engine, module.upgrade)
    finally:
        engine.dispose()
    con = sqlite3.connect(db_path)
    try:
        assert _table_info(con)["spend_usd"]["default"] is None
        assert dict(con.execute("SELECT id, spend_usd FROM ad_metrics").fetchall())[1] is None
    finally:
        con.close()


# ── 2. the ORM contract ───────────────────────────────────────────────────────────────────────


def test_orm_spend_is_nullable_with_no_default() -> None:
    column = AdMetric.__table__.c.spend_usd
    assert column.nullable is True
    assert column.server_default is None, "a server default would re-conflate absence with $0"
    assert column.default is None, "no Python-side default either"
    assert column.type.precision == 14 and column.type.scale == 4


# ── 3. the writer ────────────────────────────────────────────────────────────────────────────


def _raw_payload(*, spend: object = None, include_spend: bool) -> dict:
    """The run's `result` mapping exactly as the worker hands it to the writer."""
    metric: dict = {
        "campaign_external_id": "g-100",
        "date": "2026-10-13",
        "impressions": 100,
        "clicks": 5,
        "conversions": 1,
        "revenue_usd": 0.0,
    }
    if include_spend:
        metric["spend_usd"] = spend
    return {
        "summary": "one metric day",
        "campaigns": [
            {"platform": "google_ads", "external_id": "g-100", "name": "Search Brand"}
        ],
        "metrics": [metric],
    }


@pytest.mark.asyncio
async def test_writer_persists_explicit_null_when_spend_omitted(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(async_db_session, _raw_payload(include_spend=False))
    assert report["ok"] is True, report
    await async_db_session.commit()

    raw = await async_db_session.execute(text("SELECT spend_usd IS NULL FROM ad_metrics"))
    assert raw.scalar() == 1, "an omitted spend must be stored NULL, never the old 0 default"


@pytest.mark.asyncio
async def test_writer_persists_a_measured_zero_spend_exactly(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(
        async_db_session, _raw_payload(spend=0.0, include_spend=True)
    )
    assert report["ok"] is True, report
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.spend_usd == Decimal("0.0000"), "a reported 0 is a measurement, not absence"


@pytest.mark.asyncio
async def test_writer_persists_a_measured_positive_spend_exactly(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    await record_ads_run(async_db_session, _raw_payload(spend=2.5, include_spend=True))
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.spend_usd == Decimal("2.5")


# ── 4. the API: null vs measured zero ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def ads_client(async_db_session):
    from fastapi import FastAPI

    from app.api.deps import get_db
    from app.api.v1.ads import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: async_db_session
    app_config.settings.api_key = "test-key"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_api_serves_null_spend_and_measured_zero_distinctly(
    ads_client, async_db_session
) -> None:
    campaign = AdCampaign(platform="google_ads", name="spend-truth")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add_all(
        [
            AdMetric(
                campaign_id=campaign.id,
                date=date(2026, 10, 1),
                impressions=100,
                clicks=10,
                conversions=2,
                spend_usd=None,
                revenue_usd=Decimal("10"),
            ),
            AdMetric(
                campaign_id=campaign.id,
                date=date(2026, 10, 2),
                impressions=80,
                clicks=4,
                conversions=0,
                spend_usd=Decimal("0"),
                revenue_usd=Decimal("0"),
            ),
            AdMetric(
                campaign_id=campaign.id,
                date=date(2026, 10, 3),
                impressions=20,
                clicks=2,
                conversions=1,
                spend_usd=Decimal("2.5"),
                revenue_usd=Decimal("5"),
            ),
        ]
    )
    await async_db_session.commit()

    response = await ads_client.get(
        f"/api/v1/ads/campaigns/{campaign.id}/metrics", headers=HEADERS
    )
    assert response.status_code == 200, response.text[:400]
    rows = {row["date"]: row for row in response.json()}

    assert rows["2026-10-01"]["spend_usd"] is None, "unmeasured spend is null on the wire"
    assert rows["2026-10-01"]["roas"] is None, "no spend denominator -> no ratio"
    assert rows["2026-10-02"]["spend_usd"] == 0.0, "a measured zero is served as 0.0"
    assert rows["2026-10-02"]["roas"] is None, "zero spend is no denominator"
    assert rows["2026-10-03"]["spend_usd"] == 2.5
    assert rows["2026-10-03"]["roas"] == 2.0
