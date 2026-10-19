"""T09 / D17 — `AdMetricRow.revenue_usd` + the persist round trip.

The contract this pins: the metric row's money field is declared in the SCHEMA the run payload
is validated against (`AdMetricRow`, `app/schemas/agent_outputs.py`), so a reported revenue
survives validation, reaches the writer's money loop (`ads_service.record_ads_run`) and lands in
the `Numeric(14,4)` column — and `roas` (a GENERATED column) is computed by the engine from it.

V24/L4 moved the field's default: absent revenue is `None` (never `0.0`), because the old zero
default made every run that did not report revenue assert a `$0` measurement.  V25/L4 completed
the storage half: migration 0014 makes `ad_metrics.revenue_usd` nullable with no default, so the
writer persists an explicit NULL for an unreported value (the pre-0014 column defaulted it to 0).

RED leg (measured, receipt §RED): delete the `revenue_usd` field from `AdMetricRow` and
`test_a_reported_revenue_round_trips` fails — Pydantic drops the unknown key, the writer never
sees it and the row keeps the column default (the R33 residual this ticket closes).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select


def _payload(revenue: float | None) -> dict:
    """One `AdsOutput`-shaped run payload, validated THROUGH the schema (the real path)."""
    from app.schemas.agent_outputs import AdsOutput

    metric: dict = {
        "campaign_external_id": "g-100",
        "date": "2026-10-13",
        "impressions": 100,
        "clicks": 5,
        "conversions": 1,
        "spend_usd": 10.0,
    }
    if revenue is not None:
        metric["revenue_usd"] = revenue
    model = AdsOutput.model_validate({
        "summary": "one metric day",
        "campaigns": [{"platform": "google_ads", "external_id": "g-100", "name": "Search Brand"}],
        "metrics": [metric],
    })
    return model.model_dump(mode="json")


# ── the declared field ─────────────────────────────────────────────────────────────────────────


def test_the_field_is_declared_optional_and_non_negative():
    from app.schemas.agent_outputs import AdMetricRow

    field = AdMetricRow.model_fields["revenue_usd"]
    assert field.default is None, "an unreported revenue is absent — never a fabricated zero"
    assert AdMetricRow(
        campaign_external_id="g-1", date="2026-10-13"
    ).revenue_usd is None
    with pytest.raises(ValidationError):
        AdMetricRow(campaign_external_id="g-1", date="2026-10-13", revenue_usd=-0.01)


def test_an_unknown_key_would_be_dropped_so_the_field_must_be_declared():
    """The mechanism the RED leg relies on: this model has no extra-key passthrough."""
    from app.schemas.agent_outputs import AdMetricRow

    assert AdMetricRow.model_config.get("extra", "ignore") != "allow"


# ── the round trip (schema -> writer -> column) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reported_revenue_round_trips(async_db_session):
    from app.models.ads import AdMetric
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(async_db_session, _payload(revenue=12.5))
    assert report["ok"] is True, report
    assert (report["campaigns_written"], report["metrics_written"]) == (1, 1)

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd == Decimal("12.5"), "the reported revenue reaches the exact column"
    assert row.spend_usd == Decimal("10")
    assert row.roas == Decimal("1.25"), "roas is the ENGINE's value: 12.5 / 10"
    assert row.ctr == Decimal("0.05")


@pytest.mark.asyncio
async def test_an_unreported_revenue_is_stored_as_null(async_db_session):
    """Not reported is ABSENCE — the writer persists an explicit NULL, never a zero.

    V25/L4 re-pin (old -> new): this test used to assert `revenue_usd == Decimal("0")` and
    `roas == Decimal("0")`, because the pre-0014 column was `NOT NULL DEFAULT 0` and the writer
    omitted the key.  Migration 0014 makes the column nullable with no default and the writer
    writes NULL, so the honest assertion is None/None — the exact distinction V24/L4 could only
    document as a deviation.
    """
    from app.models.ads import AdMetric
    from app.services.ads_service import record_ads_run

    report = await record_ads_run(async_db_session, _payload(revenue=None))
    assert report["metrics_written"] == 1

    row = (await async_db_session.execute(select(AdMetric))).scalars().one()
    assert row.revenue_usd is None
    assert row.roas is None
