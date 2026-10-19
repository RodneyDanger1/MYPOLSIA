"""V25/L4 — ads revenue is MEASURED or ABSENT: nullable at rest, distinct on the wire.

The V24/L4 lane could only tell half the truth: `ad_metrics.revenue_usd` shipped
`NUMERIC(14, 4) DEFAULT '0' NOT NULL`, so an unreported revenue and a measured `$0` were the
same stored byte — and the API had to serve EVERY stored zero as `null` to avoid printing a
`$0.00` it could not certify.  A genuine zero-revenue day therefore rendered as a dash.

This file pins the completed contract (migration 0014 + model/service/API alignment):

  1. `ad_metrics.revenue_usd` is NULLABLE with NO server default; existing `0` rows are
     backfilled to NULL (a stored zero was never certified as a measurement); downgrade restores
     `NOT NULL DEFAULT 0` after `COALESCE(revenue_usd, 0)`.  The generated `roas` column yields
     NULL unless revenue is measured AND spend is a usable denominator.
  2. The ORM maps `revenue_usd` as `Decimal | None` with no default; `AdMetric()` without it
     stores NULL.  `roas` is `None` for an unmeasured day and `0` for a measured zero over spend.
  3. `record_ads_run` persists an EXPLICIT NULL for an unreported revenue and a reported `0`
     as a measured zero — never one as the other.
  4. The API serves NULL as `null` and a measured `0` as `0.0`; ROAS only when revenue and spend
     are both measured (and spend is a usable denominator).
  5. Sweep: the adjacent reads named in the brief survive malformed AT-REST JSON — the daily
     report route and the runs (usage) ledger serve a null cell, never a 500.

Attribution: every "old" assertion this lane re-pins lives in a sibling file and is updated there
with a V25/L4 note; this file is the new contract and is RED on the base tree (no 0014, revenue
NOT NULL/DEFAULT 0, a stored 0 served as null).
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


def _load_migration():
    """Import `0014_ads_nullable_revenue.py` by path (module name is not an identifier)."""
    path = VERSIONS / "0014_ads_nullable_revenue.py"
    assert path.exists(), "migration 0014 is missing"
    spec = importlib.util.spec_from_file_location("v25_ads_nullable_0014", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revision_graph() -> dict[str, str | None]:
    """{revision: down_revision} from every version file, annotation-shape agnostic."""
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


def test_migration_0014_is_child_of_0013_and_nothing_revises_it() -> None:
    """Single head: 0014 sits on L1's 0013, and no revision names 0014 as its parent."""
    graph = _revision_graph()
    assert "0014" in graph, graph
    assert graph["0014"] == "0013", "0014's down_revision must be L1's 0013 (one chain)"
    assert "0014" not in set(graph.values()), "another revision revises 0014 — the chain forks"
    # 0013 is L1's file; when it has landed, it must point at 0012 and be the ONLY parent of 0014.
    if "0013" in graph:
        assert graph["0013"] == "0012", graph


def _legacy_sqlite_db(path) -> sqlite3.Connection:
    """A database in the PRE-0014 shape (0012 head), seeded through the migration's own DDL.

    `_DOWNGRADE_FINAL_DDL` IS the pre-0014 CREATE TABLE text (that is what downgrade restores),
    so seeding through it measures the actual upgrade path instead of a hand-copied schema.
    """
    module = _load_migration()
    con = sqlite3.connect(path)
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
    con.execute(module._DOWNGRADE_FINAL_DDL)
    for index_sql in module._INDEXES:
        con.execute(index_sql)
    con.execute("INSERT INTO ad_campaigns (id, platform, name) VALUES (1, 'google_ads', 'c1')")
    rows = [
        # id, revenue, spend  — 0 revenue is the OLD column default (never certified measured)
        (1, 0.0, 10.0),
        (2, 12.5, 10.0),
        (3, 0.0, 0.0),
        (4, 6.0, 0.0),   # revenue measured, spend zero -> ROAS is undefined, not 0
    ]
    for row_id, revenue, spend in rows:
        con.execute(
            "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions, "
            "spend_usd, revenue_usd) VALUES (?, 1, ?, 100, 10, 1, ?, ?)",
            (row_id, f"2026-10-0{row_id}", spend, revenue),
        )
    con.commit()
    return con


def _run_ops(engine, fn) -> None:
    """Run a migration leg through Alembic's Operations proxy on a real connection."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            fn()


def _table_info(con: sqlite3.Connection) -> dict[str, tuple]:
    return {
        row[1]: {"type": row[2], "notnull": bool(row[3]), "default": row[4]}
        for row in con.execute("PRAGMA table_info(ad_metrics)")
    }


def test_upgrade_backfills_zero_revenue_to_null_and_keeps_measurements(tmp_path) -> None:
    from sqlalchemy import create_engine

    db_path = tmp_path / "pre0014.db"
    con = _legacy_sqlite_db(db_path)
    con.close()
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    module = _load_migration()
    try:
        _run_ops(engine, module.upgrade)
    finally:
        engine.dispose()

    con = sqlite3.connect(db_path)
    try:
        info = _table_info(con)
        assert info["revenue_usd"]["notnull"] is False, "revenue_usd must be nullable"
        assert info["revenue_usd"]["default"] is None, "no server default: absence is explicit NULL"
        assert info["spend_usd"]["notnull"] is False, "spend was already nullable — untouched"
        assert info["spend_usd"]["default"] == "'0'", "spend's pre-existing default is left alone"

        values = dict(con.execute("SELECT id, revenue_usd FROM ad_metrics").fetchall())
        assert values[1] is None, "a stored 0 from the old default is NOT a measurement"
        assert values[2] == 12.5, "a reported revenue is preserved exactly"
        assert values[3] is None
        assert values[4] == 6.0, "a measured revenue survives even with zero spend"

        assert con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0] == 4, "no row lost"

        roas = dict(con.execute("SELECT id, roas FROM ad_metrics").fetchall())
        assert roas[1] is None, "no revenue -> the generated ROAS is NULL, not 0"
        assert float(roas[2]) == pytest.approx(1.25)
        assert roas[3] is None, "zero spend is no denominator — NULL, not 0×"
        assert roas[4] is None

        stored_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ad_metrics'"
        ).fetchone()[0]
        assert "revenue_usd NUMERIC(14, 4)," in stored_sql
        assert "revenue_usd NUMERIC(14, 4) DEFAULT" not in stored_sql
        assert "revenue_usd NUMERIC(14, 4) NOT NULL" not in stored_sql

        index_names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ad_metrics'"
            )
        }
        assert {"ix_ad_metrics_date_campaign", "uq_ad_metrics_campaign_date"} <= index_names
    finally:
        con.close()


def test_downgrade_restores_not_null_default_zero_with_coalesce(tmp_path) -> None:
    from sqlalchemy import create_engine

    db_path = tmp_path / "roundtrip.db"
    con = _legacy_sqlite_db(db_path)
    con.close()
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
        assert info["revenue_usd"]["notnull"] is True
        assert info["revenue_usd"]["default"] == "'0'"
        values = dict(con.execute("SELECT id, revenue_usd FROM ad_metrics").fetchall())
        assert values[1] == 0.0, "a NULL is restored as the pre-0014 default, not lost"
        assert values[2] == 12.5
        assert con.execute("SELECT COUNT(*) FROM ad_metrics").fetchone()[0] == 4
        # The old generated expression is back: zero spend -> 0 (the pre-0014 lie, restored
        # because a downgrade must return the previous SHAPE, not a different one).
        assert float(con.execute("SELECT roas FROM ad_metrics WHERE id=4").fetchone()[0]) == 0.0
        # And the upgraded shape can be re-applied (up/down/up round trip).
    finally:
        con.close()

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        _run_ops(engine, module.upgrade)
    finally:
        engine.dispose()
    con = sqlite3.connect(db_path)
    try:
        assert _table_info(con)["revenue_usd"]["notnull"] is False
        assert dict(con.execute("SELECT id, revenue_usd FROM ad_metrics").fetchall())[1] is None
    finally:
        con.close()


# ── 2. the ORM contract ───────────────────────────────────────────────────────────────────────


def test_orm_revenue_is_nullable_with_no_default() -> None:
    column = AdMetric.__table__.c.revenue_usd
    assert column.nullable is True
    assert column.server_default is None, "a server default would re-conflate absence with $0"
    assert column.default is None, "no Python-side default either"
    assert column.type.precision == 14 and column.type.scale == 4


@pytest.mark.asyncio
async def test_orm_insert_without_revenue_stores_null_and_roas_null(async_db_session) -> None:
    campaign = AdCampaign(platform="google_ads", name="orm-null")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add(AdMetric(campaign_id=campaign.id, date=date(2026, 10, 1),
                                  impressions=10, clicks=1, conversions=0, spend_usd=Decimal("2")))
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd is None, "an omitted revenue is absence at rest, not 0"
    assert row.roas is None, "the generated ROAS cannot invent a ratio from an absent revenue"


@pytest.mark.asyncio
async def test_orm_measured_zero_is_a_zero_not_null(async_db_session) -> None:
    campaign = AdCampaign(platform="google_ads", name="orm-zero")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add(AdMetric(campaign_id=campaign.id, date=date(2026, 10, 1),
                                  impressions=10, clicks=1, conversions=0,
                                  spend_usd=Decimal("4"), revenue_usd=Decimal("0")))
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd == Decimal("0.0000"), "a reported zero IS a measurement"
    assert row.roas == Decimal("0.000000"), "0 revenue over measured spend is a real 0×"


# ── 3. the writer ────────────────────────────────────────────────────────────────────────────


def _payload(revenue: float | None, *, spend: float | None = 10.0) -> dict:
    from app.schemas.agent_outputs import AdsOutput

    metric: dict = {
        "campaign_external_id": "g-100",
        "date": "2026-10-13",
        "impressions": 100,
        "clicks": 5,
        "conversions": 1,
        "spend_usd": spend,
    }
    if revenue is not None:
        metric["revenue_usd"] = revenue
    model = AdsOutput.model_validate({
        "summary": "one metric day",
        "campaigns": [{"platform": "google_ads", "external_id": "g-100", "name": "Search Brand"}],
        "metrics": [metric],
    })
    return model.model_dump(mode="json")


@pytest.mark.asyncio
async def test_writer_persists_explicit_null_when_revenue_unreported(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(async_db_session, _payload(revenue=None))
    assert report["ok"] is True, report
    await async_db_session.commit()

    raw = await async_db_session.execute(
        text("SELECT revenue_usd IS NULL FROM ad_metrics")
    )
    assert raw.scalar() == 1, "the writer must store NULL — never the 0 that used to mean unknown"


@pytest.mark.asyncio
async def test_writer_persists_a_measured_zero_exactly(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(async_db_session, _payload(revenue=0.0))
    assert report["ok"] is True, report
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd == Decimal("0.0000"), "reported 0 is stored as 0, not as NULL"
    assert row.roas == Decimal("0.000000")


@pytest.mark.asyncio
async def test_writer_keeps_a_positive_revenue_exact(async_db_session) -> None:
    from app.services.ads_service import record_ads_run

    await record_ads_run(async_db_session, _payload(revenue=12.5))
    await async_db_session.commit()

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd == Decimal("12.5")
    assert row.roas == Decimal("1.25")


# ── 4. the API: null vs measured zero ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def ads_client(async_db_session):
    """The REAL ads router on a minimal app (the V24/L4 fixture, unchanged)."""
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


async def _seed_three_days(async_db_session) -> int:
    campaign = AdCampaign(platform="google_ads", name="api-truth", status="active")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add_all([
        # unreported -> NULL at rest
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 1), impressions=100, clicks=10,
                 conversions=2, spend_usd=Decimal("8"), revenue_usd=None),
        # measured zero over measured spend -> 0.0 / 0.0
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 2), impressions=80, clicks=4,
                 conversions=0, spend_usd=Decimal("5"), revenue_usd=Decimal("0")),
        # measured positive over zero spend -> revenue served, ROAS unmeasurable
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 3), impressions=10, clicks=1,
                 conversions=1, spend_usd=Decimal("0"), revenue_usd=Decimal("6")),
    ])
    await async_db_session.commit()
    return campaign.id


async def test_api_serves_null_and_measured_zero_distinctly(ads_client, async_db_session) -> None:
    campaign_id = await _seed_three_days(async_db_session)

    response = await ads_client.get(
        f"/api/v1/ads/campaigns/{campaign_id}/metrics", headers=HEADERS
    )
    assert response.status_code == 200, response.text[:400]
    rows = {row["date"]: row for row in response.json()}

    assert rows["2026-10-01"]["revenue_usd"] is None, "unreported revenue is null on the wire"
    assert rows["2026-10-01"]["roas"] is None

    assert rows["2026-10-02"]["revenue_usd"] == 0.0, "a measured zero is served as 0.0"
    assert rows["2026-10-02"]["roas"] == 0.0, "0 revenue / 5 spend is a real 0.00×"

    assert rows["2026-10-03"]["revenue_usd"] == 6.0
    assert rows["2026-10-03"]["roas"] is None, "zero spend has no ratio"


async def test_api_serves_a_positive_pair_and_computes_roas(ads_client, async_db_session) -> None:
    campaign = AdCampaign(platform="meta_ads", name="api-pair")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add(AdMetric(campaign_id=campaign.id, date=date(2026, 10, 4),
                                  impressions=100, clicks=10, conversions=2,
                                  spend_usd=Decimal("8"), revenue_usd=Decimal("20")))
    await async_db_session.commit()

    response = await ads_client.get(
        f"/api/v1/ads/campaigns/{campaign.id}/metrics", headers=HEADERS
    )
    row = response.json()[0]
    assert row["revenue_usd"] == 20.0
    assert row["spend_usd"] == 8.0
    assert row["roas"] == 2.5


# ── 5. SWEEP: the adjacent malformed-at-rest-JSON reads ─────────────────────────────────────


async def test_daily_report_route_survives_malformed_json_cells(api_client, auth_headers,
                                                               async_db_session) -> None:
    """`GET /dashboard/reports/daily` must serve the honest columns, not 500, for junk JSON.

    `metrics_snapshot`/`insights` are JSON at rest; a raw/ops writer can leave bytes that are not
    JSON in the SQLite deployment (PostgreSQL's jsonb would refuse them).  The V24/L6 read path
    casts each cell to TEXT and parses defensively — this is the reachable-route pin for that fix
    (the brief names it as a known-adjacent defect; it is NOT a live defect on this tree).
    """
    from app.models.report import DailyReport

    report = DailyReport(report_date=date(2026, 10, 9), morning_plan="plan", evening_summary="done")
    async_db_session.add(report)
    await async_db_session.commit()

    await async_db_session.execute(
        text("UPDATE daily_reports SET metrics_snapshot = :bad, insights = :bad2 WHERE id = :id"),
        {"bad": "{not json", "bad2": "[not json", "id": report.id},
    )
    await async_db_session.commit()
    async_db_session.expire_all()

    response = await api_client.get(
        "/api/v1/dashboard/reports/daily?report_date=2026-10-09", headers=auth_headers
    )
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["morning_plan"] == "plan", "the readable columns still serve"
    assert body["metrics_snapshot"] is None, "junk is 'not measured', never a parsed guess"
    assert body["insights"] is None


async def test_ads_metrics_read_survives_malformed_numeric_cells(ads_client, async_db_session) -> None:
    """One junk AT-REST numeric cell must not 500 the campaign's whole metric history.

    Measured on the base tree (V25/L4 sweep): a text value in `ad_metrics.revenue_usd` made
    SQLAlchemy's Numeric result processor raise `TypeError: must be real number, not str` inside
    `db.execute`, and `GET /ads/campaigns/{id}/metrics` answered an uncoded 500.  SQLite's dynamic
    typing permits the state (PostgreSQL's typed columns refuse the write).  The reader now casts
    every cell to TEXT and parses per row: bad money/ratio/count cells read as null ("not
    measured"), a row whose DATE is unreadable is omitted by name, and the honest days still serve.
    """
    campaign = AdCampaign(platform="google_ads", name="junk-cells")
    async_db_session.add(campaign)
    await async_db_session.flush()
    campaign_id = campaign.id
    async_db_session.add(AdMetric(campaign_id=campaign_id, date=date(2026, 10, 1),
                                  impressions=100, clicks=10, conversions=2,
                                  spend_usd=Decimal("10"), revenue_usd=Decimal("12.5")))
    await async_db_session.commit()

    await async_db_session.execute(text(
        "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions,"
        " spend_usd, revenue_usd) VALUES (:cid, '2026-10-02', 'n/a', 5, 0, 'oops', 'abc')"
    ), {"cid": campaign_id})
    await async_db_session.execute(text(
        "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions,"
        " spend_usd, revenue_usd) VALUES (:cid, 'not-a-date', 1, 1, 0, 1, 1)"
    ), {"cid": campaign_id})
    await async_db_session.commit()
    async_db_session.expire_all()

    response = await ads_client.get(
        f"/api/v1/ads/campaigns/{campaign_id}/metrics", headers=HEADERS
    )
    assert response.status_code == 200, response.text[:400]
    rows = response.json()

    # The unreadable-DATE row is omitted (never served under a fabricated day); the other two serve.
    assert [row["date"] for row in rows] == ["2026-10-02", "2026-10-01"]
    junk = rows[0]
    assert junk["impressions"] is None and junk["spend_usd"] is None
    assert junk["revenue_usd"] is None and junk["roas"] is None

    honest = rows[1]
    assert honest["impressions"] == 100
    assert honest["revenue_usd"] == 12.5
    assert honest["roas"] == 1.25


async def test_runs_usage_ledger_survives_malformed_usage_json(api_client, auth_headers,
                                                              async_db_session) -> None:
    """`GET /api/v1/runs` (the /usage page's read) must not 500 on junk `usage_json`.

    There is no `/api/v1/usage` ROUTE in this tree — the usage page reads this ledger.  The U5
    contract: a malformed at-rest envelope reads as 'not recorded' (null), and the row still
    serves.  Pinned here because the brief names the usage surface as a known-adjacent defect.
    """
    from app.models.task import AgentRun

    run = AgentRun(agent_type="finance", run_type="task", status="completed")
    async_db_session.add(run)
    await async_db_session.commit()
    run_id = run.id  # capture BEFORE expire_all — a later attribute read would lazy-load

    await async_db_session.execute(
        text("UPDATE agent_runs SET usage_json = :bad, tools_used = :bad2 WHERE id = :id"),
        {"bad": "{not json", "bad2": "{not json", "id": run_id},
    )
    await async_db_session.commit()
    async_db_session.expire_all()

    response = await api_client.get("/api/v1/runs?limit=10", headers=auth_headers)
    assert response.status_code == 200, response.text[:400]
    row = next(item for item in response.json()["items"] if item["id"] == run_id)
    assert row["cost_source"] is None
    assert row["pricing_version"] is None

    detail = await api_client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text[:400]
    body = detail.json()
    assert body["tools_used"] is None, "junk tools_used is 'never recorded', not a crash"
    assert body["num_turns"] is None
    assert body["is_error"] is None
