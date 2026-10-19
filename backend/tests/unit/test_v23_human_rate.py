"""Wave V23 / L3 — `company_config.human_hourly_rate`: storage, round-trip, and the Finance read.

THE PRODUCT ASK (COMPLETENESS §7 item 11): margin was permanently `—` because
`build_planning_view` HARD-CODED `human_rate = None` — the owner profile carried hours, never a
rate.  This wave gives the rate a real column (migration 0011, additive + reversible), accepts it
on `PUT /config` (`ge=0`, `null` clears), and reads it in the planning view so margin becomes
measurable ONLY when the rate AND capacity (owner hours + catalog hours, which is what allocates
`ai_cost`) both exist.

THE HONESTY LAWS PINNED HERE:
  * absent rate  -> `margin_template.human_rate: null` + a reason naming where to set it;
  * absent capacity -> `margin` stays null with its own reason even when the rate exists;
  * both present -> the rate is served AND `margin` is computed (never fabricated);
  * the write side refuses negative and non-finite rates (`ge=0`; NaN/Infinity are not
    measurements — the S4-F1 rule);
  * the migration is additive (one nullable FLOAT, no default) and reversible (downgrade drops
    exactly that column; rows survive; the dropped value is documented data loss).
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]


# ── migration 0011 on a real SQLite chain ────────────────────────────────────────────────────


@pytest.fixture
def alembic_db(monkeypatch, tmp_path):
    """A tmp SQLite database plus the alembic Config env.py reads (`settings.database_url`)."""
    from alembic import config as alembic_config
    from app.config import settings

    db_path = tmp_path / "v23_human_rate.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg, db_path


def _columns(db_path: pathlib.Path, table: str) -> list[tuple[str, str, bool, str | None]]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return [
            (row[1], row[2], bool(row[3]), row[4])
            for row in con.execute(f"PRAGMA table_info({table})")
        ]
    finally:
        con.close()


def _scalar(db_path: pathlib.Path, sql: str):
    con = sqlite3.connect(db_path.as_posix())
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def test_migration_0011_is_additive_and_reversible_on_sqlite(alembic_db):
    """upgrade -> ONE nullable FLOAT with no default; downgrade -> column gone, row intact."""
    from alembic import command

    cfg, db_path = alembic_db
    command.upgrade(cfg, "0010")

    assert "human_hourly_rate" not in [col[0] for col in _columns(db_path, "company_config")]
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute("INSERT INTO company_config (name) VALUES ('Pre-rate Co')")
        con.commit()
    finally:
        con.close()
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "0010"

    command.upgrade(cfg, "0011")
    columns = _columns(db_path, "company_config")
    rate = [col for col in columns if col[0] == "human_hourly_rate"]
    assert len(rate) == 1, "0011 must add exactly one column"
    _name, type_name, not_null, default = rate[0]
    assert not_null is False, "the column is NULLABLE: NULL = no rate was set"
    assert default is None, "no default: an existing row must stay NULL, never a fabricated rate"
    assert type_name.upper().startswith("FLOAT"), f"portable Float column, got {type_name!r}"
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "0011"
    assert _scalar(db_path, "SELECT COUNT(*) FROM company_config") == 1
    assert _scalar(db_path, "SELECT human_hourly_rate FROM company_config") is None

    # A stored rate survives the upgrade; the downgrade drops the COLUMN, never the row.
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute("UPDATE company_config SET human_hourly_rate = 85.5")
        con.commit()
    finally:
        con.close()

    command.downgrade(cfg, "0010")
    assert "human_hourly_rate" not in [col[0] for col in _columns(db_path, "company_config")]
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "0010"
    assert _scalar(db_path, "SELECT COUNT(*) FROM company_config") == 1, "downgrade must not lose rows"
    assert _scalar(db_path, "SELECT name FROM company_config") == "Pre-rate Co"

    # Re-upgrade: the column returns empty — dropping it discarded the value (documented, not silent).
    command.upgrade(cfg, "0011")
    assert "human_hourly_rate" in [col[0] for col in _columns(db_path, "company_config")]
    assert _scalar(db_path, "SELECT human_hourly_rate FROM company_config") is None
    assert _scalar(db_path, "SELECT version_num FROM alembic_version") == "0011"


# ── PUT /config round-trip (None clears) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_config_round_trips_the_rate_and_null_clears(api_client, auth_headers, async_db_session):
    from app.models.company import CompanyConfig

    company = CompanyConfig(name="Rate Co")
    async_db_session.add(company)
    await async_db_session.commit()

    read = await api_client.get("/api/v1/config", headers=auth_headers)
    assert read.status_code == 200, read.text
    assert read.json()["human_hourly_rate"] is None, "absent = null, never 0"

    written = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"human_hourly_rate": 85}
    )
    assert written.status_code == 200, written.text
    assert written.json()["human_hourly_rate"] == 85.0
    await async_db_session.refresh(company)
    assert float(company.human_hourly_rate) == 85.0, "the row itself carries the rate"

    read = await api_client.get("/api/v1/config", headers=auth_headers)
    assert read.json()["human_hourly_rate"] == 85.0

    # A MEASURED ZERO is a reading, not an absence.
    zero = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"human_hourly_rate": 0}
    )
    assert zero.status_code == 200, zero.text
    assert zero.json()["human_hourly_rate"] == 0.0

    # null clears the value (present-but-null).  It must never become 0.
    cleared = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"human_hourly_rate": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["human_hourly_rate"] is None
    await async_db_session.refresh(company)
    assert company.human_hourly_rate is None

    # A negative rate is refused BY THE FIELD, before the row is touched.
    refused = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"human_hourly_rate": -0.01}
    )
    assert refused.status_code == 422, refused.text
    assert "human_hourly_rate" in refused.text
    await async_db_session.refresh(company)
    assert company.human_hourly_rate is None, "a refused PUT must not have written anything"

    # The JSON non-finite tokens (`NaN`, overflowing `1e999` -> Infinity) are refused too: they
    # are corruption, not measurements (S4-F1), and must never reach the column.
    for token in ("NaN", "1e999"):
        non_finite = await api_client.put(
            "/api/v1/config",
            headers={**auth_headers, "Content-Type": "application/json"},
            content=f'{{"human_hourly_rate": {token}}}'.encode(),
        )
        assert non_finite.status_code == 422, (token, non_finite.text)
        await async_db_session.refresh(company)
        assert company.human_hourly_rate is None


def test_update_schema_refuses_negative_and_non_finite_rates():
    """`ge=0` plus the finite-number law: NaN/Infinity are not measurements."""
    from pydantic import ValidationError

    from app.schemas.company import CompanyConfigUpdate

    assert CompanyConfigUpdate(human_hourly_rate=0).human_hourly_rate == 0.0
    assert CompanyConfigUpdate(human_hourly_rate=42.5).human_hourly_rate == 42.5
    assert CompanyConfigUpdate(human_hourly_rate=None).human_hourly_rate is None
    for bad in (-1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            CompanyConfigUpdate(human_hourly_rate=bad)


def test_service_accessor_treats_no_row_and_corrupt_values_as_unmeasured():
    """The read side re-checks old/raw rows: a corrupt stored rate is NO VALUE, not money math."""
    from app.models.company import CompanyConfig
    from app.services.company_service import measured_human_hourly_rate

    assert measured_human_hourly_rate(None) is None
    assert measured_human_hourly_rate(CompanyConfig(name="x")) is None
    assert measured_human_hourly_rate(CompanyConfig(name="x", human_hourly_rate=0)) == 0.0
    assert measured_human_hourly_rate(CompanyConfig(name="x", human_hourly_rate=85.5)) == 85.5
    assert measured_human_hourly_rate(CompanyConfig(name="x", human_hourly_rate=-1)) is None
    assert (
        measured_human_hourly_rate(CompanyConfig(name="x", human_hourly_rate=float("inf")))
        is None
    )


# ── finance reads the stored rate; margin only with rate + capacity ─────────────────────────


async def _seed_measured_company(async_db_session, *, rate: float | None) -> None:
    """The fixed inputs test_v9_planning already proves: SEO row -> 25 h, 40 h/wk -> 6 clients."""
    from decimal import Decimal

    from app.core.database import utcnow
    from app.models.company import CompanyConfig
    from app.models.task import AgentRun

    async_db_session.add(
        CompanyConfig(
            name="Rate Co",
            owner_hours_per_week=40,
            human_hourly_rate=rate,
            confirmed_idea={
                "name": "SEO — small business",
                "price_band": [1500, 3000],
                "one_liner": "Full SEO program",
            },
        )
    )
    async_db_session.add(
        AgentRun(
            agent_type="social_media",
            status="completed",
            cost_usd=Decimal("60"),
            started_at=utcnow(),
        )
    )
    await async_db_session.commit()


@pytest.mark.asyncio
async def test_finance_uses_the_stored_rate_and_margin_becomes_measurable(
    api_client, auth_headers, async_db_session
):
    await _seed_measured_company(async_db_session, rate=30)

    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["margin_template"]["price"] == 1500.0
    assert body["margin_template"]["ai_cost"] == 10.0  # 60 USD / 6 clients
    assert body["margin_template"]["human_hours"] == 25.0
    assert body["margin_template"]["human_rate"] == 30.0, "the stored rate is READ, not hard-coded"
    # (1500 - (10 + 25*30)) / 1500 = 740 / 1500
    assert body["margin_template"]["margin"] == pytest.approx(740 / 1500)
    # Measured fields carry no reason — the panel prints every reason it is given.
    assert "margin_template.human_rate" not in body["reasons"]
    assert "margin_template.margin" not in body["reasons"]


@pytest.mark.asyncio
async def test_rate_without_capacity_keeps_margin_null_with_its_reason(
    api_client, auth_headers, async_db_session
):
    """The rate exists, but no owner hours -> no max_clients -> no ai_cost allocation -> no margin."""
    from app.models.company import CompanyConfig

    async_db_session.add(
        CompanyConfig(
            name="Rate Only Co",
            human_hourly_rate=30,
            confirmed_idea={
                "name": "SEO — small business",
                "price_band": [1500, 3000],
                "one_liner": "Full SEO program",
            },
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["capacity"]["max_clients"] is None
    assert body["margin_template"]["human_rate"] == 30.0
    assert body["margin_template"]["ai_cost"] is None
    assert body["margin_template"]["margin"] is None, "capacity is absent — margin must stay null"
    assert "margin_template.human_rate" not in body["reasons"]
    assert "capacity.max_clients" in body["reasons"]
    assert "margin_template.ai_cost" in body["reasons"]
    assert "margin_template.margin" in body["reasons"]


@pytest.mark.asyncio
async def test_absent_rate_keeps_its_reason_and_points_at_finance(
    api_client, auth_headers, async_db_session
):
    await _seed_measured_company(async_db_session, rate=None)

    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["margin_template"]["human_rate"] is None
    assert body["margin_template"]["margin"] is None
    reason = body["reasons"]["margin_template.human_rate"]
    assert isinstance(reason, str) and reason.strip()
    assert "Finance" in reason, "the reason must say WHERE to set the rate"
    assert "margin_template.margin" in body["reasons"]
