"""Wave v14 S3-05: the planning endpoint's REASON CONTRACT, pinned against the real route.

The Finance page's Planning panel (`frontend/src/app/finance/page.tsx`) renders each unmeasured
input as the em dash PLUS the cause the payload records for that exact field. The cause lives in
the response's `reasons` map, keyed 1:1 to the field it explains. Before this wave the panel read
`burn_reason` / `capacity_reason` / `margin_reason` — fields NO backend ever produced — while the
13-key `reasons` map had no consumer, so the cause never reached the screen (wave v13 s3-wiring F5).

This pin nails the key vocabulary BOTH sides agreed on: if the backend renames a reason key, this
test goes red instead of the panel silently printing "no reason recorded".
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_planning_reasons_map_carries_every_unmeasured_field(api_client, auth_headers):
    """On an empty database every null field the panel renders has its cause in `reasons`."""
    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The three segments the panel renders are ALWAYS objects (nulls inside when unmeasured) — the
    # panel's field-level rendering depends on this shape, and its `segment: null` branch is
    # defensive only.
    assert isinstance(body["capacity"], dict)
    assert isinstance(body["margin_template"], dict)

    # Every reason key below is read VERBATIM by the panel (`reasonFor(reasons, "…")`).
    for key in (
        "burn_line_items",
        "capacity.hours_per_client_lo",
        "capacity.hours_per_client_hi",
        "capacity.max_clients",
        "capacity.mrr_at_capacity",
        "capacity.break_even_clients",
        "margin_template.price",
        "margin_template.ai_cost",
        "margin_template.human_hours",
        "margin_template.human_rate",
        "margin_template.margin",
    ):
        assert key in body["reasons"], f"{key} must carry the cause of its null field"
        assert isinstance(body["reasons"][key], str) and body["reasons"][key].strip()

    # And each such key really accompanies a null (never a measured value).
    assert body["capacity"]["max_clients"] is None
    assert body["margin_template"]["margin"] is None
    assert body["burn_line_items"] == []


@pytest.mark.asyncio
async def test_planning_measured_fields_carry_no_reason(api_client, auth_headers, async_db_session):
    """A reason exists only for an UNMEASURED field — the panel prints every reason it is given."""
    from app.models.company import CompanyConfig

    async_db_session.add(
        CompanyConfig(
            name="Reason Co",
            owner_hours_per_week=40,
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

    capacity = body["capacity"]
    assert capacity["max_clients"] == 6
    assert capacity["hours_per_client_lo"] == 15
    # Measured now: the capacity causes must be GONE from the map (a stale key would be printed
    # next to a live number by the panel).
    for key in (
        "capacity.hours_per_client_lo",
        "capacity.hours_per_client_hi",
        "capacity.max_clients",
    ):
        assert key not in body["reasons"], f"{key} must not explain a measured value"
    # The unmeasured ones stay (human_rate is never configured; the margin needs it).
    assert "margin_template.human_rate" in body["reasons"]
    assert "margin_template.margin" in body["reasons"]
