"""V15 / E1 — 5-05 (S4-F4, decision #9 LOCKED): `PUT /config` refuses UNKNOWN keys BY NAME.

RED-first: at b17d7b0 `CompanyConfigUpdate` (`schemas/company.py:75`) carries no
`model_config`/`extra=`, so pydantic's default IGNORES a typo'd key and `api/v1/config.py`
answers 200 "saved" — the caller believes `{"namee": "x"}` renamed the company.

Pinned legs:
  * one unknown top-level key -> 422 NAMING it (pydantic `extra_forbidden` loc), row untouched;
  * a typo'd sibling among valid keys -> 422 naming the typo only;
  * the legit whole-object PUT (incl. the owner_* keys 4-06 declares first-class) -> 200 and the
    row actually updates — strictness must not break the real settings-page write.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select


async def _seed_company(db):
    from app.models.company import CompanyConfig

    db.add(CompanyConfig(name="Strict Co"))
    await db.flush()


async def _company_name(db) -> str:
    from app.services.company_service import get_company

    company = await get_company(db)
    assert company is not None
    return company.name


@pytest.mark.asyncio
async def test_unknown_key_is_refused_by_name_and_changes_nothing(
    api_client, auth_headers, async_db_session
):
    await _seed_company(async_db_session)

    resp = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"namee": "typo renamed"}
    )

    assert resp.status_code == 422, (
        f"5-05 not fixed: PUT /config answered {resp.status_code} for an unknown key "
        f"({resp.text[:200]!r}) — a typo'd key was silently accepted"
    )
    detail = resp.json()["detail"]
    assert any(
        "namee" in [str(part) for part in error.get("loc", [])] for error in detail
    ), f"the 422 must NAME the offending key: {detail!r}"
    assert "namee" in resp.text
    assert await _company_name(async_db_session) == "Strict Co", "a refused PUT must not touch the row"


@pytest.mark.asyncio
async def test_typo_among_valid_keys_is_refused_by_name(
    api_client, auth_headers, async_db_session
):
    await _seed_company(async_db_session)

    resp = await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={"name": "Renamed", "owner_hours_per_week": 5, "owner_hour_per_week": 9},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any(
        "owner_hour_per_week" in [str(part) for part in error.get("loc", [])]
        for error in detail
    ), detail
    assert await _company_name(async_db_session) == "Strict Co", "all-or-nothing: nothing saved"


@pytest.mark.asyncio
async def test_legit_whole_object_put_with_owner_keys_still_saves(
    api_client, auth_headers, async_db_session
):
    await _seed_company(async_db_session)

    resp = await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={
            "name": "Renamed Co",
            "owner_skills": "ops, pricing",
            "owner_hours_per_week": 12,
            "owner_budget_usd": 250.5,
            "owner_goal": "first 10 customers",
            "risk_tolerance": "low",
            "channels_ready": ["email"],
            "market_locale": "en-US",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed Co"
    assert body["owner_hours_per_week"] == 12
    assert body["owner_skills"] == "ops, pricing"

    from app.models.company import CompanyConfig

    row = (await async_db_session.execute(select(CompanyConfig))).scalar_one()
    assert row.name == "Renamed Co" and row.owner_hours_per_week == 12
