"""R26 — the ads **service** swept for real: filters, limits, ordering, refusals, dialects.

Measured before this lane (`polsia-work/build/R26-ads/raw/baseline-coverage.txt`):
`app/services/ads_service.py` 30 stmts / 18 miss / **40.0%** — no shipped test called any of the four
functions (coverage was import-time only).  This file exercises every statement and every caller-reachable
branch of `list_campaigns` / `create_campaign` / `get_campaign` / `list_metrics` against the repo's own
fixtures: the shipped `async_db_session` (`tests/conftest.py:24-47`, `Base.metadata.create_all`) and a
**migration-head** SQLite database built exactly like `alembic upgrade head` (the pattern of
`tests/unit/test_support_inbox_migration.py:50-61`; the upgrade runs in a worker thread because
`alembic/env.py:48` calls `asyncio.run` and cannot run inside the test's loop).

Four defects found by this sweep are fixed in `ads_service.py` (B1-B4 below; RED legs in the receipt).
Two more are cross-file and are carried here as `xfail(strict=False)` proofs with the fix location named
(B5/B6) — never silently skipped, never pinned as if correct:

B1 `get_campaign` did a raw `db.execute` while the module docstring (`ads_service.py:8`) promises "every
   read failure is `E_DB`" — the refusal bridge (`app/api/deps.py:95-125`) only translates `PolsiaError`,
   so a DB fault during `list_metrics`' existence check escaped as a bare 500.
B2 `create_campaign`'s `flush()` was unwrapped — a write failure escaped raw (the same class
   `outreach_service.create_prospect:44-58` documents as fixed for prospects: `E_VALIDATION` for a
   constraint, `db_refusal`/`E_DB` for everything else).
B3 a `status` outside the migration's value set (`ck_ad_campaigns_status`, `0002:103-104`) was stored
   silently on SQLite while PostgreSQL raises the CHECK — and `0002:298-299` states the rule for exactly
   this: the value sets "are enforced on PostgreSQL and asserted by the code paths that write them".
B4 padded input passed the `.strip()` guard but was stored *unstripped*, so a campaign written as
   `" google_ads "` could never be found by the platform filter (write/read disagreement; the sibling
   `create_prospect` normalises before storing).
"""
from __future__ import annotations

import inspect
import sqlite3
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import E_DB, E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.ads import AdCampaign, AdMetric
from app.schemas.social import AdCampaignResponse
from app.services import ads_service
from app.services.ads_service import (
    create_campaign,
    get_campaign,
    list_campaigns,
    list_metrics,
)

BACKEND = Path(__file__).resolve().parents[2]

# ── helpers ──────────────────────────────────────────────────────────────────


def _assert_refusal(exc: PolsiaError, code: str) -> None:
    """A refusal names itself: code, a message, and the fix hint from the one table."""
    assert exc.code == code
    assert exc.message
    assert exc.how_to_fix == ERROR_HINTS[code], "the hint must come from ERROR_HINTS"
    assert set(exc.as_dict()["error"]) == {"code", "message", "how_to_fix", "context"}


async def _count(db: AsyncSession, model: type) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _add_campaign(db: AsyncSession, platform: str = "google_ads", name: str = "camp") -> AdCampaign:
    row = AdCampaign(platform=platform, name=name)
    db.add(row)
    await db.commit()
    return row


async def _add_metric(db: AsyncSession, campaign_id: int, day: date, impressions: int) -> AdMetric:
    row = AdMetric(campaign_id=campaign_id, date=day, impressions=impressions)
    db.add(row)
    await db.commit()
    return row


# ── migration-head fixtures ──────────────────────────────────────────────────


@pytest.fixture
def head_db(monkeypatch, tmp_path) -> tuple[Path, str]:
    """A SQLite database at the migration head — the DDL production actually ships.

    Re-exported (imported) by `tests/unit/api/test_r26_ads_edges.py`: one definition, two files.
    """
    from alembic import command
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "r26_head.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    # `alembic/env.py:48` calls `asyncio.run(...)`: it cannot run inside the test's event loop,
    # so the upgrade gets its own thread (always loop-free).
    errors: list[BaseException] = []

    def _upgrade() -> None:
        try:
            command.upgrade(cfg, "head")
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread below
            errors.append(exc)

    worker = threading.Thread(target=_upgrade)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]
    return db_path, url


@pytest_asyncio.fixture
async def head_session(head_db):
    _db_path, url = head_db
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _head_ddl(db_path: Path, table: str) -> str:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
    finally:
        con.close()


# ── public contract ──────────────────────────────────────────────────────────


def test_the_public_surface_is_exactly_the_four_functions() -> None:
    """`__all__` is the callers' contract (side-effect dispatch imports by name)."""
    assert ads_service.__all__ == [
        "create_campaign",
        "get_campaign",
        "list_campaigns",
        "list_metrics",
    ]
    for name in ads_service.__all__:
        assert inspect.iscoroutinefunction(getattr(ads_service, name)), f"{name} must stay async"


# ── create_campaign: refusals ────────────────────────────────────────────────


@pytest.mark.parametrize("platform", [None, "", "   "])
async def test_create_campaign_refuses_a_blank_platform(async_db_session, platform) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        await create_campaign(async_db_session, platform, "a name")
    _assert_refusal(excinfo.value, E_VALIDATION)
    assert excinfo.value.context == {"platform": platform, "name": "a name"}
    assert await _count(async_db_session, AdCampaign) == 0, "a refusal must not write"


@pytest.mark.parametrize("name", [None, "", "   "])
async def test_create_campaign_refuses_a_blank_name(async_db_session, name) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        await create_campaign(async_db_session, "google_ads", name)
    _assert_refusal(excinfo.value, E_VALIDATION)
    assert excinfo.value.context == {"platform": "google_ads", "name": name}
    assert await _count(async_db_session, AdCampaign) == 0


@pytest.mark.parametrize("status", ["womble", "ACTIVE", "active ", "pending"])
async def test_create_campaign_refuses_a_status_outside_the_migrations_value_set(
    async_db_session, status
) -> None:
    """B3: `ck_ad_campaigns_status` (0002:103-104) is enforced by PostgreSQL only; SQLite cannot carry
    it (0002:297-299) and the unit fixture has no CHECK at all, so the writer must assert the set —
    exactly what that migration note requires.  Without this guard the two engines disagree."""
    with pytest.raises(PolsiaError) as excinfo:
        await create_campaign(async_db_session, "google_ads", "camp", status=status)
    _assert_refusal(excinfo.value, E_VALIDATION)
    assert excinfo.value.context["status"] == status
    assert await _count(async_db_session, AdCampaign) == 0


@pytest.mark.parametrize("status", ["active", "paused", "ended", "draft"])
async def test_create_campaign_accepts_every_documented_status(async_db_session, status) -> None:
    row = await create_campaign(async_db_session, "google_ads", f"camp-{status}", status=status)
    assert row.status == status


async def test_create_campaign_treats_an_explicit_null_status_as_the_column_default(
    async_db_session,
) -> None:
    """`status=None` is "unset" (the column default applies) — measured, not assumed
    (`raw/probe1-output.txt` P2; both default and server_default are 'active')."""
    row = await create_campaign(async_db_session, "google_ads", "camp", status=None)
    assert row.status == "active"
    stored = (await async_db_session.execute(
        select(AdCampaign.status).where(AdCampaign.id == row.id)
    )).scalar_one()
    assert stored == "active"


# ── create_campaign: write shape ─────────────────────────────────────────────


async def test_create_campaign_stores_trimmed_values(async_db_session) -> None:
    """B4: the guard already treats padded input as equivalent; storing it padded made the row
    unfindable through the platform filter the API exposes."""
    row = await create_campaign(async_db_session, " google_ads ", "  My Camp  ")
    assert (row.platform, row.name) == ("google_ads", "My Camp")
    found = await list_campaigns(async_db_session, platform="google_ads")
    assert [r.id for r in found] == [row.id], "the write must be findable by the canonical filter"


async def test_create_campaign_passes_unknown_columns_through_and_returns_a_loadable_row(
    async_db_session,
) -> None:
    row = await create_campaign(
        async_db_session,
        "meta",
        "passthrough",
        external_id="ext-1",
        goal="signups",
        daily_budget_usd=Decimal("12.5000"),
    )
    assert isinstance(row, AdCampaign)
    # Post-flush the server-side columns must be readable (SQLAlchemy 2.0 fetches them via RETURNING;
    # a lazy load here would raise MissingGreenlet in async context — measured in probe 1).
    assert (row.id, row.status, row.created_at) != (None, None, None)
    assert row.daily_budget_usd == Decimal("12.5000")
    stored = (await async_db_session.execute(
        select(AdCampaign.external_id, AdCampaign.goal, AdCampaign.total_spent_usd)
        .where(AdCampaign.id == row.id)
    )).one()
    assert tuple(stored) == ("ext-1", "signups", Decimal("0.0000"))


async def test_create_campaign_maps_a_constraint_violation_to_a_named_refusal(
    async_db_session,
) -> None:
    """B2: the `IntegrityError` arm.  The test installs a REAL unique index (the shape every
    PostgreSQL-side CHECK/UNIQUE arrives as) and provokes a REAL violation — no mock."""
    await async_db_session.execute(
        text("CREATE UNIQUE INDEX uq_r26_ad_campaigns_name ON ad_campaigns (name)")
    )
    await create_campaign(async_db_session, "google_ads", "unique-name")
    with pytest.raises(PolsiaError) as excinfo:
        await create_campaign(async_db_session, "meta", "unique-name")
    _assert_refusal(excinfo.value, E_VALIDATION)
    assert excinfo.value.context == {"platform": "meta", "name": "unique-name", "status": None}
    await async_db_session.rollback()


async def test_create_campaign_maps_a_write_failure_to_e_db(async_db_session) -> None:
    """B2: pre-fix the raw `OperationalError` escaped; the module's refusal discipline covers writes too."""
    await async_db_session.execute(text("DROP TABLE ad_campaigns"))
    with pytest.raises(PolsiaError) as excinfo:
        await create_campaign(async_db_session, "google_ads", "no-table")
    _assert_refusal(excinfo.value, E_DB)
    assert excinfo.value.context == {"query": "ad campaign insert"}


# ── get_campaign ─────────────────────────────────────────────────────────────


async def test_get_campaign_returns_the_row_or_none(async_db_session) -> None:
    assert await get_campaign(async_db_session, 1) is None
    row = await _add_campaign(async_db_session, name="one")
    got = await get_campaign(async_db_session, row.id)
    assert isinstance(got, AdCampaign) and got.id == row.id and got.name == "one"


async def test_get_campaign_translates_a_read_failure_to_e_db(async_db_session) -> None:
    """B1: `raw db.execute` escaped as an unnamed `OperationalError`; every read here is `E_DB`."""
    await async_db_session.execute(text("DROP TABLE ad_campaigns"))
    with pytest.raises(PolsiaError) as excinfo:
        await get_campaign(async_db_session, 1)
    _assert_refusal(excinfo.value, E_DB)
    assert excinfo.value.context == {"query": "ad campaign"}


async def test_get_campaign_returns_the_entity_not_a_row(async_db_session) -> None:
    """Return-shape contract: callers get an `AdCampaign` with its attributes, not a `Row`.

    This is the pin that caught this lane's own first fix attempt: `query_helpers.fetch_one`
    translates `E_DB` but hands back `Row((AdCampaign,))` for an entity select, so `get_campaign`
    keeps `one_or_none()` and wraps the translation itself (a PK filter cannot return several
    rows, so the `MultipleResultsFound` arm is unreachable by construction)."""
    row = await _add_campaign(async_db_session, name="entity")
    got = await get_campaign(async_db_session, row.id)
    assert isinstance(got, AdCampaign)
    assert (got.id, got.name, got.platform) == (row.id, "entity", "google_ads")


# ── list_campaigns ───────────────────────────────────────────────────────────


async def test_list_campaigns_is_empty_on_an_empty_table(async_db_session) -> None:
    rows = await list_campaigns(async_db_session)
    assert rows == [] and isinstance(rows, list)


async def test_list_campaigns_is_newest_first(async_db_session) -> None:
    for name in ("a", "b", "c"):
        await _add_campaign(async_db_session, name=name)
    rows = await list_campaigns(async_db_session)
    assert [r.name for r in rows] == ["c", "b", "a"]
    assert all(isinstance(r, AdCampaign) for r in rows)


async def test_list_campaigns_filters_platform_and_status_independently(async_db_session) -> None:
    await _add_campaign(async_db_session, "google_ads", "ga-active")
    await _add_campaign(async_db_session, "meta", "meta-active")
    paused = await _add_campaign(async_db_session, "google_ads", "ga-paused")
    paused.status = "paused"
    await async_db_session.commit()

    assert [r.name for r in await list_campaigns(async_db_session, platform="google_ads")] == [
        "ga-paused",
        "ga-active",
    ]
    assert [r.name for r in await list_campaigns(async_db_session, status="paused")] == ["ga-paused"]
    both = await list_campaigns(async_db_session, platform="google_ads", status="active")
    assert [r.name for r in both] == ["ga-active"]
    assert await list_campaigns(async_db_session, platform="nope") == []


async def test_list_campaigns_treats_a_blank_filter_as_a_literal_value(async_db_session) -> None:
    """Pinned, deliberately not "fixed": `platform=""` is a filter for the empty string, not
    "no filter" — the same shape `?platform=` has at the HTTP layer (pinned again in the api file),
    and the identical contract in `social_service`/`outreach_service`.  House-wide decision, see receipt."""
    await _add_campaign(async_db_session, "google_ads", "ga")
    assert await list_campaigns(async_db_session, platform="") == []
    assert await list_campaigns(async_db_session, status="") == []


async def test_list_campaigns_limit_boundaries(async_db_session) -> None:
    for name in ("a", "b", "c"):
        await _add_campaign(async_db_session, name=name)
    assert [r.name for r in await list_campaigns(async_db_session, limit=1)] == ["c"]
    assert [r.name for r in await list_campaigns(async_db_session, limit=2)] == ["c", "b"]
    assert await list_campaigns(async_db_session, limit=0) == []
    assert len(await list_campaigns(async_db_session, limit=10**9)) == 3


async def test_list_campaigns_default_limit_is_fifty_and_newest_survive(async_db_session) -> None:
    async_db_session.add_all(
        [AdCampaign(platform="google_ads", name=f"c{i:02d}") for i in range(51)]
    )
    await async_db_session.commit()
    rows = await list_campaigns(async_db_session)
    assert len(rows) == 50
    assert rows[0].name == "c50" and rows[-1].name == "c01"


async def test_a_negative_limit_is_unbounded_on_sqlite_and_divergent_on_postgres(
    async_db_session,
) -> None:
    """PINNED (not fixed): SQLite reads `LIMIT -1` as "no limit" (measured here), PostgreSQL rejects a
    negative LIMIT, so the two engines answer the same call differently.  Unreachable through the
    shipped route (`Query(ge=1)`, pinned in the api file) and spelled the same way in six sibling
    services — a house-wide guard decision, recorded as REQUIRED-FOLLOW-UP rather than edited here."""
    for name in ("a", "b", "c"):
        await _add_campaign(async_db_session, name=name)
    rows = await list_campaigns(async_db_session, limit=-1)
    assert [r.name for r in rows] == ["c", "b", "a"], "SQLite silently ignores a negative limit"

    sqlite_sql = str(select(AdCampaign).limit(-1).compile(compile_kwargs={"literal_binds": True}))
    pg_sql = str(
        select(AdCampaign).limit(-1).compile(dialect=postgresql.dialect())
    )
    assert "LIMIT -1" in sqlite_sql
    assert "LIMIT %(param_1)s" in pg_sql, "PG binds the parameter and errors on -1 at execution"


async def test_list_campaigns_translates_a_read_failure_to_e_db(async_db_session) -> None:
    await async_db_session.execute(text("DROP TABLE ad_campaigns"))
    with pytest.raises(PolsiaError) as excinfo:
        await list_campaigns(async_db_session)
    _assert_refusal(excinfo.value, E_DB)
    assert excinfo.value.context == {"query": "ad campaigns"}


# ── list_metrics ─────────────────────────────────────────────────────────────


async def test_list_metrics_refuses_an_unknown_campaign_by_name(async_db_session) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        await list_metrics(async_db_session, 4242)
    _assert_refusal(excinfo.value, E_NOT_FOUND)
    assert excinfo.value.context == {"campaign_id": 4242}
    assert "4242" in excinfo.value.message


async def test_list_metrics_is_empty_for_a_campaign_without_metrics(async_db_session) -> None:
    campaign = await _add_campaign(async_db_session)
    assert await list_metrics(async_db_session, campaign.id) == []


async def test_list_metrics_is_date_desc_then_id_desc(async_db_session) -> None:
    campaign = await _add_campaign(async_db_session)
    first = await _add_metric(async_db_session, campaign.id, date(2026, 1, 1), 5)
    older_dup = await _add_metric(async_db_session, campaign.id, date(2026, 1, 2), 7)
    newer_dup = await _add_metric(async_db_session, campaign.id, date(2026, 1, 2), 9)

    rows = await list_metrics(async_db_session, campaign.id)
    assert all(isinstance(r, AdMetric) for r in rows)
    assert [r.id for r in rows] == [newer_dup.id, older_dup.id, first.id]
    assert [r.date for r in rows] == [date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 1)]


async def test_list_metrics_limit_boundaries(async_db_session) -> None:
    campaign = await _add_campaign(async_db_session)
    for day in (1, 2, 3):
        await _add_metric(async_db_session, campaign.id, date(2026, 1, day), day)
    assert len(await list_metrics(async_db_session, campaign.id, limit=10**9)) == 3
    assert [r.date for r in await list_metrics(async_db_session, campaign.id, limit=1)] == [
        date(2026, 1, 3)
    ]
    assert await list_metrics(async_db_session, campaign.id, limit=0) == []


async def test_list_metrics_translates_a_read_failure_to_e_db(async_db_session) -> None:
    campaign = await _add_campaign(async_db_session)
    await async_db_session.execute(text("DROP TABLE ad_metrics"))
    with pytest.raises(PolsiaError) as excinfo:
        await list_metrics(async_db_session, campaign.id)
    _assert_refusal(excinfo.value, E_DB)
    assert excinfo.value.context == {"query": "ad metrics"}


async def test_list_metrics_existence_check_is_e_db_when_campaigns_are_unreadable(
    async_db_session,
) -> None:
    """B1's second symptom: the existence check runs on `ad_campaigns`; pre-fix its failure escaped raw."""
    await async_db_session.execute(text("DROP TABLE ad_campaigns"))
    with pytest.raises(PolsiaError) as excinfo:
        await list_metrics(async_db_session, 1)
    _assert_refusal(excinfo.value, E_DB)
    assert excinfo.value.context == {"query": "ad campaign"}


# ── the migration head (the DDL production actually ships) ───────────────────


async def test_head_schema_accepts_the_four_documented_statuses_and_serves_them(
    head_session, head_db
) -> None:
    for status in ("active", "paused", "ended", "draft"):
        row = await create_campaign(head_session, "google_ads", f"head-{status}", status=status)
        assert row.status == status
    rows = await list_campaigns(head_session, platform="google_ads")
    assert [r.status for r in rows] == ["draft", "ended", "paused", "active"]
    # the shape the frontend consumes (schemas/social.py:43-52) must validate on the real DDL too
    for row in rows:
        AdCampaignResponse.model_validate(row)


async def test_head_schema_cannot_enforce_the_status_check_on_sqlite(head_db) -> None:
    """Why B3's guard lives in code: `alembic 0002` SKIPS the CHECK on SQLite
    (`0002:297-299`: "SQLite cannot ADD CONSTRAINT to an existing table ... the value sets are
    enforced on PostgreSQL and asserted by the code paths that write them")."""
    db_path, _url = head_db
    assert "CHECK" not in _head_ddl(db_path, "ad_campaigns")
    assert "GENERATED ALWAYS AS" in _head_ddl(db_path, "ad_metrics")


async def test_head_schema_metrics_are_read_through_the_generated_columns(
    head_session,
) -> None:
    """`list_metrics` against the migrated ad_metrics (ctr/cpc/roas are GENERATED there, and
    revenue_usd exists only in the DDL) — raw seed rows, because the mapped insert cannot write
    those columns (see the B6 xfail below)."""
    await create_campaign(head_session, "google_ads", "metrics-head")
    await head_session.execute(text(
        "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions,"
        " spend_usd, revenue_usd) VALUES (1,'2026-01-01',4,1,0,1.5,0.0)"))
    await head_session.execute(text(
        "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions,"
        " spend_usd, revenue_usd) VALUES (1,'2026-01-02',8,2,1,2.5,0.0)"))
    await head_session.commit()

    rows = await list_metrics(head_session, 1)
    assert [r.date for r in rows] == [date(2026, 1, 2), date(2026, 1, 1)]
    assert rows[0].ctr == pytest.approx(0.25), "the generated column is what the reader sees"
    assert rows[0].spend_usd == Decimal("2.5000")


async def test_head_schema_unknown_campaign_still_refuses_by_name(head_session) -> None:
    with pytest.raises(PolsiaError) as excinfo:
        await list_metrics(head_session, 7)
    _assert_refusal(excinfo.value, E_NOT_FOUND)


# ── cross-file defects (proven here, fixed elsewhere) ────────────────────────


@pytest.mark.xfail(
    strict=False,
    reason="KNOWN DEFECT / REQUIRED-FOLLOW-UP (app/schemas/social.py:43-52): status and the two money "
    "columns are nullable in the frozen schema (models/ads.py:21-30) and on PostgreSQL CHECK(NULL) "
    "passes, while AdCampaignResponse declares them required — one schema-legal NULL row makes the "
    "whole endpoint answer an unnamed 500 (probe2 Q6). Not fixable in this lane's files.",
)
async def test_a_schema_legal_null_row_does_not_break_the_response_model(head_session) -> None:
    await head_session.execute(text(
        "INSERT INTO ad_campaigns (platform, name, status, daily_budget_usd, total_spent_usd,"
        " created_at) VALUES ('meta','legacy-null',NULL,NULL,NULL,"
        " strftime('%Y-%m-%d %H:%M:%S','now'))"))
    await head_session.commit()
    row = (await head_session.execute(
        select(AdCampaign).where(AdCampaign.name == "legacy-null")
    )).scalar_one()
    AdCampaignResponse.model_validate(row)  # currently raises pydantic ValidationError


@pytest.mark.xfail(
    strict=False,
    reason="KNOWN DEFECT / REQUIRED-FOLLOW-UP (app/models/ads.py:47-54): ctr/cpc/roas are mapped as "
    "plain writable Floats while BOTH engines hold them GENERATED (0002:233-242 drop+add; 0003:71 "
    "rebuild), and revenue_usd (NOT NULL in the DDL) is not mapped at all — the ORM's own INSERT "
    "cannot write a row into the shipped schema (measured: \"cannot INSERT into generated column "
    "%r\" / probe3-output.txt) while the create_all unit schema accepts it. No writer exists yet, so "
    "this is latent; the fix location is the model, not this lane's files.",
)
async def test_the_mapped_metric_insert_can_write_the_migrated_schema(head_session) -> None:
    await create_campaign(head_session, "google_ads", "orm-metric")
    head_session.add(AdMetric(campaign_id=1, date=date(2026, 1, 2), impressions=10, clicks=2))
    await head_session.flush()  # currently OperationalError: cannot INSERT into generated column "ctr"
