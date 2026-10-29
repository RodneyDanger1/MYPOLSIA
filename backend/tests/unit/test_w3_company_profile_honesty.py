"""Wave-3 company-profile honesty — blanks stay blank, zeros stay measured, labels stay true.

THE RISK.  `PUT /api/v1/config` is the ONLY writer of the singleton company row, and it copies
every provided field onto the row (`api/v1/config.py::update_config`).  `kpis`, `pricing_model` and
`goals` are free-form JSON, so a coercion anywhere in that path (a `value or 0`, a pydantic float
field, a "helpful" default) would turn an owner's BLANK into a measurement — the exact class the
V22 seed fix and the TS-4 dashboard label exist to prevent.  On this wave the profile is also
created for a real micro-business, so a fabricated `0` there becomes a fabricated price/KPI on the
storefront.

WHAT IS PINNED HERE (backend round-trip, no browser):

  * a JSON `null` inside `kpis` is stored and served as `null` — never `0`;
  * an empty object is stored and served as `{}` — never materialised into zero-valued keys;
  * a MEASURED zero (`0`) survives as `0` (zero is a measurement; null is the absence);
  * a blank string survives as `""` (the owner cleared a box; it is not a zero);
  * a partial PUT (only `name`) never wipes the other JSON columns;
  * `/dashboard/summary` serves the configured map VERBATIM and labels it `configured`;
  * the seed (`scripts/seed_company.py::default_company`) still writes `kpis={}` — the "create"
    path cannot fabricate KPIs.

Each assertion is a named check, so the RED leg (a coercion planted in the writer) fails HERE
rather than shipping a fabricated zero to the dashboard.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.company import CompanyConfig

SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_company.py"


def _seed_module():
    """Load the seed script as a module (it is a script, not part of a package)."""
    spec = importlib.util.spec_from_file_location("seed_company_w3", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _company(db) -> CompanyConfig:
    company = CompanyConfig(name="W3 Profile Co", timezone="UTC", daily_cycle_hour=6)
    db.add(company)
    await db.commit()
    return company


async def _stored(db) -> CompanyConfig:
    """The row as stored (fresh read — the API writes through its own session)."""
    db.expire_all()
    row = (
        await db.execute(select(CompanyConfig).order_by(CompanyConfig.id).limit(1))
    ).scalar_one()
    return row


@pytest.mark.asyncio
async def test_kpi_blanks_round_trip_as_nulls_never_zero(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session)
    body = {"kpis": {"mrr_usd": None, "active_customers": None, "churn_rate": None}}

    put = await api_client.put("/api/v1/config", json=body, headers=auth_headers)
    assert put.status_code == 200, put.text
    assert put.json()["kpis"] == body["kpis"]

    get = await api_client.get("/api/v1/config", headers=auth_headers)
    assert get.json()["kpis"] == body["kpis"], "a blank must never come back as 0"

    stored = await _stored(async_db_session)
    assert stored.kpis == body["kpis"], "the ROW carries the nulls, not a coerced 0"


@pytest.mark.asyncio
async def test_empty_objects_round_trip_as_empty_never_materialised_zeros(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session)
    body = {"kpis": {}, "pricing_model": {}, "goals": {}}

    put = await api_client.put("/api/v1/config", json=body, headers=auth_headers)
    assert put.status_code == 200, put.text
    served = put.json()
    for field in ("kpis", "pricing_model", "goals"):
        assert served[field] == {}, f"{field} must stay empty, not become a zero-valued map"

    stored = await _stored(async_db_session)
    assert (stored.kpis, stored.pricing_model, stored.goals) == ({}, {}, {})


@pytest.mark.asyncio
async def test_a_measured_zero_is_preserved_as_zero(
    async_db_session, api_client, auth_headers
) -> None:
    """Zero is a MEASUREMENT — the honesty rule forbids replacing it with null either."""
    await _company(async_db_session)
    body = {"kpis": {"mrr_usd": 0, "active_customers": 0}}

    put = await api_client.put("/api/v1/config", json=body, headers=auth_headers)
    assert put.json()["kpis"] == {"mrr_usd": 0, "active_customers": 0}
    assert put.json()["kpis"]["mrr_usd"] is not None

    stored = await _stored(async_db_session)
    assert stored.kpis == {"mrr_usd": 0, "active_customers": 0}


@pytest.mark.asyncio
async def test_a_blank_string_stays_blank(async_db_session, api_client, auth_headers) -> None:
    await _company(async_db_session)

    put = await api_client.put(
        "/api/v1/config", json={"kpis": {"mrr_usd": ""}}, headers=auth_headers
    )
    assert put.status_code == 200, put.text
    assert put.json()["kpis"] == {"mrr_usd": ""}

    stored = await _stored(async_db_session)
    assert stored.kpis == {"mrr_usd": ""}


@pytest.mark.asyncio
async def test_a_partial_put_never_wipes_the_other_json_columns(
    async_db_session, api_client, auth_headers
) -> None:
    """The settings page PUTs a subset; `exclude_unset` must keep the rest of the profile."""
    await _company(async_db_session)
    original = {
        "kpis": {"mrr_usd": None},
        "pricing_model": {"skus": [{"name": "9 oz candle", "price": 26}]},
        "goals": {"q4_2026": ["opening order placed"]},
    }
    seeded = await api_client.put("/api/v1/config", json=original, headers=auth_headers)
    assert seeded.status_code == 200, seeded.text

    # … then a PUT that only renames the company (the settings form's save shape).
    renamed = await api_client.put(
        "/api/v1/config", json={"name": "Renamed Co"}, headers=auth_headers
    )
    assert renamed.status_code == 200, renamed.text
    served = renamed.json()
    assert served["name"] == "Renamed Co"
    for field, value in original.items():
        assert served[field] == value, f"a partial PUT cleared {field}"

    stored = await _stored(async_db_session)
    assert stored.kpis == original["kpis"]
    assert stored.pricing_model == original["pricing_model"]
    assert stored.goals == original["goals"]


@pytest.mark.asyncio
async def test_the_dashboard_serves_the_configured_kpis_verbatim_with_the_label(
    async_db_session, api_client, auth_headers
) -> None:
    """`kpis` is the owner's data (verbatim) and the wire says WHERE it came from."""
    await _company(async_db_session)
    kpis = {"mrr_usd": None, "active_customers": 0}
    await api_client.put("/api/v1/config", json={"kpis": kpis}, headers=auth_headers)

    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()

    assert data["kpis"] == kpis, "the dashboard must not fill, round or drop a configured value"
    assert data["kpis_provenance"] == "configured"


def test_the_seed_create_path_writes_no_kpi_that_nobody_measured() -> None:
    """The ONLY create path is `make seed`; it must not start the profile with zero KPIs."""
    company = _seed_module().default_company()

    assert company.kpis == {}, (
        "a seed-time zero KPI is a measurement nobody made; the profile must start blank"
    )
