"""R31 — ads schema truth: the ORM now writes the schema both migrations ship.

R26 (`polsia-work/build/R26-ads/RECEIPT.md` §F1/§F2) proved two cross-file defects with named
`xfail` proofs; this lane fixes them and converts the proofs into pins:

F2  `app/models/ads.py` mapped `ctr/cpc/roas` as plain WRITABLE Floats while BOTH engines hold them
    `GENERATED ALWAYS AS (...)` (0002:233-245; 0003:71/79), and `revenue_usd` (`NOT NULL` in the
    shipped DDL, 0002:203-207) was unmapped — the ORM's own INSERT died on the migrated schema
    (`cannot INSERT into generated column "ctr"`, R26 `raw/probe3-output.txt`) while the
    `create_all` unit fixture accepted it: the fixture hid a production failure.  Fixed: the three
    ratios are `Numeric(14, 6)` + `Computed(<the 0002 expression>)` (read-only), `revenue_usd` is
    mapped `Numeric(14, 4) NOT NULL`.
F1  `AdCampaignResponse` (`app/schemas/social.py`) demanded `status`/`daily_budget_usd`/
    `total_spent_usd` that the frozen schema allows to be NULL (raw-SQL/ops writers are an accepted
    path — `e2e/ads.spec.ts`), so ONE legal NULL row turned `GET /api/v1/ads/campaigns` into a 500
    with no `E_*` code.  Fixed: those fields are optional with the documented semantics *null =
    not recorded, never a fabricated 0*; the frontend guard (`frontend/src/app/ads/page.tsx`)
    renders the dash for them.

Every DB-touching assertion below runs either against an `alembic upgrade head` SQLite database
(0001→0004 — the DDL production ships) or against the shipped `create_all` unit fixture, whose
`ad_metrics` DDL is pinned HERE to be the migrated text modulo SQLite's implicit `VIRTUAL` keyword
(measured: `raw/probe4-output.txt`).  No mocks anywhere.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import SQLAlchemyError

from app.models.ads import AdMetric
from app.services.ads_service import create_campaign, list_metrics

# The R26 fixtures — one definition, three files, re-registered by import.  The reference below
# marks the imports as used: the test signatures deliberately shadow the imported names with the
# fixture parameters pytest resolves, which is only an F811 "redefinition" while the import reads
# as unused.
from tests.unit.api.test_r26_ads_edges import head_api_client
from tests.unit.test_r26_ads_sweep import head_db, head_session

_R26_FIXTURES = (head_db, head_session, head_api_client)

HEADERS = {"X-API-Key": "test-key"}
URL = "/api/v1/ads/campaigns"
#: `app/schemas/social.py:43-52` — the frozen response contract the /ads page reads, key-for-key.
RESPONSE_KEYS = {
    "id",
    "platform",
    "external_id",
    "name",
    "goal",
    "status",
    "daily_budget_usd",
    "total_spent_usd",
    "created_at",
}


def _norm_ddl(sql: str) -> str:
    """Whitespace-insensitive DDL text.

    SQLite re-packs the text it stores (a column added by `ALTER TABLE` lands on the previous
    definition's line), so the stored string is a rendering, not the contract; the tokens are.
    """
    return "".join(sql.split())


def _file_ddl(db_path: Path, table: str = "ad_metrics") -> str:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
    finally:
        con.close()


async def _fixture_ddl(session, table: str = "ad_metrics") -> str:
    mine = text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t")
    return (await session.execute(mine, {"t": table})).scalar_one()


async def _add(session, platform: str = "google_ads", name: str = "camp", **kw) -> int:
    from app.models.ads import AdCampaign

    row = AdCampaign(platform=platform, name=name, **kw)
    session.add(row)
    await session.commit()
    return row.id


# ── F2: the mapped insert writes the migrated schema (the regression pin) ─────────────────────


async def test_the_orm_insert_writes_the_migrated_schema(head_session) -> None:
    """F2 acceptance pin: an ORM insert against the `alembic upgrade head` schema.

    Pre-fix this exact flush died with
    `(sqlite3.OperationalError) cannot INSERT into generated column "ctr"` (R26 `raw/probe3-output.txt`)
    because the model declared the three generated ratios writable.  The generated values asserted
    below are computed BY THE ENGINE from (impressions, clicks, spend_usd, revenue_usd).
    """
    await create_campaign(head_session, "google_ads", "r31-orm-writer")
    metric = AdMetric(
        campaign_id=1,
        date=date(2026, 1, 2),
        impressions=10,
        clicks=2,
        conversions=1,
        spend_usd=Decimal("3.0000"),
        revenue_usd=Decimal("6.0000"),
    )
    head_session.add(metric)
    await head_session.flush()

    stored = (
        await head_session.execute(select(AdMetric).where(AdMetric.id == metric.id))
    ).scalar_one()
    assert stored.ctr == Decimal("0.200000"), "2 clicks / 10 impressions, computed by the engine"
    assert stored.cpc == Decimal("1.500000"), "3.0000 spend / 2 clicks"
    assert stored.roas == Decimal("2.000000"), "6.0000 revenue / 3.0000 spend"
    assert stored.revenue_usd == Decimal("6.0000")


def test_the_insert_statement_never_names_the_generated_columns() -> None:
    """Portable statement-level pin: the INSERT is built without ctr/cpc/roas on BOTH dialects.

    (An explicitly set value still reaches the INSERT — see the next test: the ENGINE is the
    enforcement point, exactly as 0002's comment measures.)
    """
    stmt = AdMetric.__table__.insert().values(campaign_id=1, date=date(2026, 1, 2))
    for dialect in (sqlite_dialect.dialect(), postgresql.dialect()):
        sql = str(stmt.compile(dialect=dialect))
        assert "ctr" not in sql and "cpc" not in sql and "roas" not in sql, sql
        assert "revenue_usd" in sql, "the NOT NULL column must be supplied"


async def test_the_engine_refuses_an_explicit_write_to_a_generated_column(head_session) -> None:
    """The columns are unwritable BY CONSTRUCTION — an explicitly set value is refused by the DB.

    Pinned as the named engine error rather than trusted to the ORM, because that is where the
    guarantee lives (0002:236-238 states it as the measured property of the drop+re-add).
    """
    await create_campaign(head_session, "google_ads", "r31-gen-guard")
    head_session.add(
        AdMetric(
            campaign_id=1,
            date=date(2026, 1, 3),
            impressions=4,
            clicks=1,
            ctr=Decimal("0.900000"),
        )
    )
    with pytest.raises(SQLAlchemyError) as excinfo:
        await head_session.flush()
    assert 'cannot INSERT into generated column "ctr"' in str(excinfo.value)
    await head_session.rollback()


async def test_generated_ratios_are_read_back_through_the_orm(head_session) -> None:
    """Both writers agree on the migrated schema: an ORM-seeded row and a raw-SQL-seeded row are
    read back through `list_metrics` with the engine-computed ratios (the read path production
    uses), and the values are Decimals — the type the DDL holds (`NUMERIC(14, 6)`)."""
    await create_campaign(head_session, "google_ads", "r31-read")
    head_session.add(
        AdMetric(
            campaign_id=1,
            date=date(2026, 1, 4),
            impressions=8,
            clicks=2,
            spend_usd=Decimal("2.0000"),
            revenue_usd=Decimal("8.0000"),
        )
    )
    await head_session.flush()
    await head_session.execute(
        text(
            "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions,"
            " spend_usd, revenue_usd) VALUES (1,'2026-01-05',8,4,0,2.0,8.0)"
        )
    )
    await head_session.commit()

    rows = await list_metrics(head_session, 1)
    assert [r.date for r in rows] == [date(2026, 1, 5), date(2026, 1, 4)]
    by_date = {r.date: r for r in rows}
    orm_row, raw_row = by_date[date(2026, 1, 4)], by_date[date(2026, 1, 5)]
    assert (orm_row.ctr, orm_row.cpc, orm_row.roas) == (
        Decimal("0.25"),
        Decimal("1"),
        Decimal("4"),
    )
    assert (raw_row.ctr, raw_row.cpc, raw_row.roas) == (
        Decimal("0.5"),
        Decimal("0.5"),
        Decimal("4"),
    )
    assert all(isinstance(v, Decimal) for v in (orm_row.ctr, orm_row.cpc, orm_row.roas))


async def test_revenue_usd_is_not_null_with_the_ddl_default(head_session) -> None:
    """`revenue_usd` is the real column: `NOT NULL` in the ORM as in the DDL, defaulting to 0
    (never NULL), and an explicit Decimal round-trips exactly."""
    assert AdMetric.__table__.c.revenue_usd.nullable is False

    await create_campaign(head_session, "google_ads", "r31-revenue")
    head_session.add(AdMetric(campaign_id=1, date=date(2026, 1, 6), impressions=4, clicks=1))
    await head_session.flush()
    defaulted = (await head_session.execute(select(AdMetric).where(AdMetric.id == 1))).scalar_one()
    assert defaulted.revenue_usd == Decimal("0.0000"), "the column default, not NULL"

    head_session.add(
        AdMetric(
            campaign_id=1,
            date=date(2026, 1, 7),
            impressions=4,
            clicks=1,
            revenue_usd=Decimal("12.3456"),
        )
    )
    await head_session.flush()
    exact = (await head_session.execute(select(AdMetric).where(AdMetric.id == 2))).scalar_one()
    assert exact.revenue_usd == Decimal("12.3456")


# ── F2: the unit fixture no longer hides the production shape ─────────────────────────────────


async def test_the_unit_fixture_schema_now_carries_the_shipped_generated_columns(
    async_db_session,
) -> None:
    """The `create_all` fixture (tests/conftest.py:24-47) must declare what the migrations ship —
    pre-R31 it held three plain writable FLOATs and no `revenue_usd`, so the fixture accepted an
    insert the migrated schema rejects."""
    ddl = await _fixture_ddl(async_db_session)
    assert ddl.count("GENERATED ALWAYS AS") == 3
    assert "revenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL" in ddl


async def test_the_fixture_ddl_matches_the_migrated_ddl(async_db_session, head_db) -> None:
    """DDL parity, measured: the fixture text equals the migrated text once whitespace and SQLite's
    implicit `VIRTUAL` keyword are set aside (the migrated text spells it; SQLite's own default is
    the same VIRTUAL column)."""
    migrated = _file_ddl(head_db[0])
    fixture = await _fixture_ddl(async_db_session)

    assert migrated.count(" VIRTUAL") == 3
    assert "VIRTUAL" not in fixture, "SQLite renders its implicit default without the keyword"
    assert _norm_ddl(fixture).replace("VIRTUAL", "") == _norm_ddl(migrated).replace("VIRTUAL", "")


# ── F1: a schema-legal NULL row is served, not 500'd (the endpoint pin) ───────────────────────


async def test_a_legal_null_row_is_served_with_nulls_not_fabricated_values(head_api_client) -> None:
    """One schema-legal NULL row must not break the list (R26 F1 measured an unnamed 500 here), and
    the served row must carry NULL — never a fabricated 0/`"active"` — while its neighbours keep
    their recorded values."""
    client, session = head_api_client
    await _add(
        session,
        "google_ads",
        "r31-fine",
        daily_budget_usd=Decimal("12.5000"),
        total_spent_usd=Decimal("3.2500"),
    )
    await session.execute(
        text(
            "INSERT INTO ad_campaigns (platform, name, status, daily_budget_usd, total_spent_usd,"
            " created_at) VALUES ('meta','r31-legacy-null',NULL,NULL,NULL,"
            " strftime('%Y-%m-%d %H:%M:%S','now'))"
        )
    )
    await session.commit()

    resp = await client.get(URL, headers=HEADERS)
    assert resp.status_code == 200, f"a schema-legal row must be served or refused by name: {resp.text[:200]}"
    rows = {r["name"]: r for r in resp.json()}
    assert set(rows) == {"r31-fine", "r31-legacy-null"}

    null_row = rows["r31-legacy-null"]
    assert set(null_row) == RESPONSE_KEYS, "the served shape stays the frozen contract"
    assert null_row["status"] is None
    assert null_row["daily_budget_usd"] is None
    assert null_row["total_spent_usd"] is None

    neighbour = rows["r31-fine"]
    assert neighbour["status"] == "active"
    assert neighbour["daily_budget_usd"] == 12.5
    assert neighbour["total_spent_usd"] == 3.25
