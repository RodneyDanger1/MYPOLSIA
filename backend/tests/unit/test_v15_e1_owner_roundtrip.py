"""V15 / E1 — 4-06 backend half (S3-02, #5 LOCKED): VERIFY-ONLY round-trip pin.

The task list (4-06) states the producer FULLY EXISTS at b17d7b0 — `schemas/company.py` accepts
the owner_* keys, `api/v1/config.py:68-69` copies `model_dump(exclude_unset=True)` onto the row,
`models/company.py` has the columns.  This pin PROVES that against the real API surface so W1
can build the Settings intake on it (the H7 verify note); it is not a rebuild and passes on the
pre-fix tree.

Honest-semantics legs (the "not set" rule W1's UI must mirror):
  * every owner_* value set via PUT comes back EXACTLY (owner_budget_usd exact at rest as
    Decimal, hours as int, channels_ready as a real list);
  * present-but-null CLEARS: owner_hours_per_week/owner_budget_usd -> null; owner_goal -> '' (the
    NOT NULL column's canonical "not configured", never a fabricated 0); channels_ready -> [].
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select


async def _seed(db):
    from app.models.company import CompanyConfig

    db.add(CompanyConfig(name="Owner Profile Co"))
    await db.flush()


@pytest.mark.asyncio
async def test_owner_fields_round_trip_through_the_put(api_client, auth_headers, async_db_session):
    await _seed(async_db_session)

    put = await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={
            "owner_skills": "operations, bookkeeping",
            "owner_hours_per_week": 15,
            "owner_budget_usd": 1234.56,
            "owner_goal": "10 paying customers",
            "risk_tolerance": "moderate",
            "channels_ready": ["email", "x"],
            "market_locale": "en-GB",
        },
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["owner_skills"] == "operations, bookkeeping"
    assert body["owner_hours_per_week"] == 15
    assert body["owner_budget_usd"] == 1234.56
    assert body["owner_goal"] == "10 paying customers"
    assert body["risk_tolerance"] == "moderate"
    assert body["channels_ready"] == ["email", "x"]
    assert body["market_locale"] == "en-GB"

    # The GET serves the same row back (one wire shape, both verbs).
    get = await api_client.get("/api/v1/config", headers=auth_headers)
    assert get.status_code == 200, get.text
    assert get.json()["owner_hours_per_week"] == 15
    assert get.json()["owner_budget_usd"] == 1234.56

    # At rest: money is an exact Decimal (A-T17), never a float artifact.
    from app.models.company import CompanyConfig

    row = (await async_db_session.execute(select(CompanyConfig))).scalar_one()
    assert row.owner_budget_usd == Decimal("1234.56")
    assert row.owner_hours_per_week == 15
    assert row.channels_ready == ["email", "x"]


@pytest.mark.asyncio
async def test_owner_null_semantics_are_canonical_not_fabricated(
    api_client, auth_headers, async_db_session
):
    await _seed(async_db_session)
    await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={"owner_hours_per_week": 9, "owner_budget_usd": 50.0, "owner_goal": "goal",
              "channels_ready": ["email"]},
    )

    cleared = await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={
            "owner_hours_per_week": None,
            "owner_budget_usd": None,
            "owner_goal": None,
            "channels_ready": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["owner_hours_per_week"] is None
    assert body["owner_budget_usd"] is None
    # NOT NULL columns carry their canonical "not configured" value — never a fabricated 0.
    assert body["owner_goal"] == ""
    assert body["channels_ready"] == []

    from app.models.company import CompanyConfig

    row = (await async_db_session.execute(select(CompanyConfig))).scalar_one()
    assert row.owner_hours_per_week is None
    assert row.owner_budget_usd is None
