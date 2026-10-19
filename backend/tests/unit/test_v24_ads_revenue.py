"""V24/L4 — ads revenue truth: measured revenue, a real ROAS, persisted and served.

The audited gap: `AdMetricRow.revenue_usd` defaulted to `0.0`, so every ads run that did not
report revenue still asserted `$0` — and `ad_metrics.roas` (GENERATED from revenue/spend) then
read `0`, which the product printed as a real ratio.  This file pins the corrected contract:

  * the schema's revenue is OPTIONAL — absent is `None`, never a fabricated zero;
  * the provider mappers fill it from the providers' OWN revenue fields only — Google
    `metrics.conversions_value`, Meta `purchase_roas` × measured spend (purchase action value is
    the documented fallback when Meta does not return purchase_roas) — and leave it `None` when
    neither is exposed;
  * `roas` is computed only when BOTH revenue and spend are measured (`None` otherwise);
  * `record_ads_run` persists a reported revenue exactly (the existing money boundary), and
    never invents one;
  * the API serves revenue and derives ROAS from the measured pair — a stored `0` (the frozen
    column's default for "unreported") is served as `null`, never as a `$0` claim or a `0×` ROAS.

RED-first: every test below fails on the base tree (revenue defaults to 0.0; the API serves the
generated `roas` which is `0` when revenue is absent).
"""
from __future__ import annotations

import sys
import types
from datetime import date
from decimal import Decimal
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.config as app_config
from app.models.ads import AdCampaign, AdMetric
from app.schemas.agent_outputs import AdMetricRow, AdsOutput
from app.services import ads_service

GOOGLE_ADS_FIELDS = (
    "google_ads_developer_token",
    "google_ads_client_id",
    "google_ads_client_secret",
    "google_ads_refresh_token",
    "google_ads_customer_id",
)
HEADERS = {"X-API-Key": "test-key"}


# ── provider boundary doubles: the client modules only (the real readers run) ──────────────────


def _arm_google_ads(monkeypatch: Any) -> None:
    values = ("dev", "cid", "secret", "refresh", "1234567890")
    for field, value in zip(GOOGLE_ADS_FIELDS, values, strict=True):
        monkeypatch.setattr(app_config.settings, field, value)


def _google_row(campaign_id: int, name: str, micros: int, conversions: float,
                value: float | None, *, omit_value: bool = False) -> Any:
    metrics = types.SimpleNamespace(cost_micros=micros, conversions=conversions)
    if not omit_value:
        metrics.conversions_value = value
    return types.SimpleNamespace(
        campaign=types.SimpleNamespace(id=campaign_id, name=name, status="ENABLED"),
        metrics=metrics,
    )


def _install_fake_google_ads(monkeypatch: Any, rows: list[Any]) -> None:
    class _FakeService:
        def search(self, *, customer_id: str, query: str) -> list[Any]:
            return rows

    class _FakeClient:
        @classmethod
        def load_from_dict(cls, config: dict[str, Any]) -> _FakeClient:
            return cls()

        def get_service(self, name: str) -> _FakeService:
            return _FakeService()

    client_mod = types.ModuleType("google.ads.googleads.client")
    client_mod.GoogleAdsClient = _FakeClient               # type: ignore[attr-defined]
    for name in ("google", "google.ads", "google.ads.googleads"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "google.ads.googleads.client", client_mod)


def _install_fake_meta(monkeypatch: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _FakeAccount:
        def __init__(self, account_id: str) -> None:
            captured["account_id"] = account_id

        def get_insights(self, *, fields: list[str], params: dict[str, Any]) -> list[Any]:
            captured["fields"] = fields
            return rows

    class _FakeApi:
        @classmethod
        def init(cls, **kwargs: Any) -> None:
            captured["token"] = kwargs.get("access_token")

    fb = types.ModuleType("facebook_business")
    fb_api = types.ModuleType("facebook_business.api")
    fb_objs = types.ModuleType("facebook_business.adobjects")
    fb_acc = types.ModuleType("facebook_business.adobjects.adaccount")
    fb_api.FacebookAdsApi = _FakeApi                      # type: ignore[attr-defined]
    fb_acc.AdAccount = _FakeAccount                       # type: ignore[attr-defined]
    fb.api = fb_api                                       # type: ignore[attr-defined]
    fb.adobjects = fb_objs                                # type: ignore[attr-defined]
    for name, module in {
        "facebook_business": fb,
        "facebook_business.api": fb_api,
        "facebook_business.adobjects": fb_objs,
        "facebook_business.adobjects.adaccount": fb_acc,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(app_config.settings, "meta_ads_access_token", "meta-token")
    monkeypatch.setattr(app_config.settings, "meta_ads_account_id", "987")
    return captured


# ── 1. the schema contract ────────────────────────────────────────────────────────────────────


def test_ad_metric_row_revenue_is_absent_by_default_not_zero() -> None:
    """Absent revenue is `None`; the old `0.0` default was a fabricated measurement."""
    row = AdMetricRow(campaign_external_id="g-1", date="2026-10-18")
    assert row.revenue_usd is None
    assert row.roas is None


def test_ad_metric_row_accepts_a_measured_zero_and_positive_revenue() -> None:
    """A reported `0` is a reading (the provider measured it); it must not be coerced to None."""
    zero = AdMetricRow(campaign_external_id="g-1", date="2026-10-18", revenue_usd=0.0)
    paid = AdMetricRow(campaign_external_id="g-1", date="2026-10-18", revenue_usd=12.34)
    assert zero.revenue_usd == 0.0
    assert paid.revenue_usd == 12.34


def test_ads_output_dump_keeps_unreported_revenue_as_none() -> None:
    """The validated payload the worker persists must carry `None`, not a zero it invented."""
    payload = AdsOutput.model_validate({
        "summary": "read one campaign",
        "metrics": [{
            "campaign_external_id": "g-1", "date": "2026-10-18",
            "impressions": 10, "clicks": 1, "conversions": 1, "spend_usd": 4.0,
        }],
    })
    dumped = payload.model_dump()["metrics"][0]
    assert dumped["revenue_usd"] is None


# ── 2. Google: `metrics.conversions_value` is the revenue measurement ─────────────────────────


def test_google_reader_fills_revenue_from_conversions_value(monkeypatch) -> None:
    from app.agents import integrations

    _arm_google_ads(monkeypatch)
    _install_fake_google_ads(monkeypatch, [
        _google_row(1, "Brand", 12_340_000, 3.0, 55.5),
        _google_row(1, "Brand", 1_660_000, 1.0, 10.0),
    ])

    out = integrations.read_ad_metrics(platform="google_ads", days=7)

    assert out["ok"] is True
    campaign = out["result"]["campaigns"][0]
    assert campaign["spend_usd"] == 14.0
    assert campaign["revenue_usd"] == 65.5
    assert campaign["conversions_value_usd"] == 65.5
    assert campaign["roas"] == round(65.5 / 14.0, 4)
    assert out["result"]["totals"]["revenue_usd"] == 65.5


def test_google_reader_leaves_revenue_none_when_the_provider_omits_it(monkeypatch) -> None:
    """No `conversions_value` in the payload = nothing measured: revenue and ROAS are `None`."""
    from app.agents import integrations

    _arm_google_ads(monkeypatch)
    _install_fake_google_ads(monkeypatch, [
        _google_row(9, "No value", 5_000_000, 0.0, None, omit_value=True),
    ])

    out = integrations.read_ad_metrics(platform="google_ads", days=7)

    campaign = out["result"]["campaigns"][0]
    assert campaign["spend_usd"] == 5.0
    assert campaign["revenue_usd"] is None
    assert campaign["conversions_value_usd"] is None
    assert campaign["roas"] is None
    assert out["result"]["totals"]["revenue_usd"] is None
    assert out["result"]["totals"]["roas"] is None


# ── 3. Meta: `purchase_roas` × measured spend ────────────────────────────────────────────────


def test_meta_reader_fills_revenue_from_purchase_roas_times_measured_spend(monkeypatch) -> None:
    from app.agents import integrations

    captured = _install_fake_meta(monkeypatch, [{
        "campaign_id": "c-1",
        "campaign_name": "Retargeting",
        "spend": "10.50",
        "purchase_roas": [{"action_type": "omni_purchase", "value": "2.5"}],
    }])

    out = integrations.read_ad_metrics(platform="meta_ads", days=14)

    assert "purchase_roas" in captured["fields"], "the reader must request the revenue field"
    campaign = out["result"]["campaigns"][0]
    assert campaign["spend_usd"] == 10.5
    assert campaign["revenue_usd"] == 26.25          # 2.5 × 10.50, the provider's own measurement
    assert campaign["roas"] == 2.5
    assert campaign["conversions_value_usd"] is None  # no purchase action value was returned


def test_meta_reader_leaves_revenue_none_without_purchase_roas_or_action_value(monkeypatch) -> None:
    from app.agents import integrations

    _install_fake_meta(monkeypatch, [{
        "campaign_id": "c-2", "campaign_name": "Awareness", "spend": "4.00",
    }])

    out = integrations.read_ad_metrics(platform="meta_ads", days=7)

    campaign = out["result"]["campaigns"][0]
    assert campaign["revenue_usd"] is None
    assert campaign["conversions_value_usd"] is None
    assert campaign["roas"] is None


def test_meta_reader_cannot_multiply_purchase_roas_without_a_measured_spend(monkeypatch) -> None:
    """`purchase_roas` is a ratio; without spend there is no revenue dollar figure to report."""
    from app.agents import integrations

    _install_fake_meta(monkeypatch, [{
        "campaign_id": "c-3", "campaign_name": "No spend",
        "purchase_roas": [{"action_type": "omni_purchase", "value": "3.0"}],
    }])

    out = integrations.read_ad_metrics(platform="meta_ads", days=7)

    campaign = out["result"]["campaigns"][0]
    assert campaign["revenue_usd"] is None
    assert campaign["roas"] is None


# ── 4. persistence: a reported revenue survives exactly, no backfill is invented ────────────


def _campaign_payload() -> dict[str, Any]:
    return {
        "platform": "google_ads",
        "external_id": "g-100",
        "name": "Search Brand",
        "status": "active",
    }


def _metric_payload(**overrides: Any) -> dict[str, Any]:
    row = {
        "campaign_external_id": "g-100",
        "date": "2026-10-12",
        "impressions": 100,
        "clicks": 5,
        "conversions": 1,
        "spend_usd": 12.5,
    }
    row.update(overrides)
    return row


async def test_record_ads_run_persists_the_reported_revenue_exactly(async_db_session) -> None:
    report = await ads_service.record_ads_run(async_db_session, {
        "campaigns": [_campaign_payload()],
        "metrics": [_metric_payload(revenue_usd=12.3456)],
    })
    await async_db_session.commit()

    assert report["ok"] is True, report
    row = (await ads_service.list_metrics(async_db_session, 1))[0]
    assert row.revenue_usd == Decimal("12.3456")


async def test_record_ads_run_leaves_the_revenue_column_default_untouched_when_unreported(
    async_db_session,
) -> None:
    """No revenue in the payload = no revenue written; the column's frozen DEFAULT is not a write.

    (The DDL is `revenue_usd NUMERIC(14,4) DEFAULT '0' NOT NULL` — alembic 0002:203-207/0003:71 —
    so the database itself cannot hold NULL for this column.  What the lane forbids is the WRITER
    or the SCHEMA asserting a measured `0`; the API serves the unmeasurable stored default as null,
    see the route tests below.)
    """
    report = await ads_service.record_ads_run(async_db_session, {
        "campaigns": [_campaign_payload()],
        "metrics": [_metric_payload()],
    })
    await async_db_session.commit()

    assert report["ok"] is True, report
    row = (await ads_service.list_metrics(async_db_session, 1))[0]
    assert row.revenue_usd == Decimal("0.0000"), "the column's default, written by nobody"


# ── 5. the API serves revenue and derives ROAS from the measured pair only ──────────────────


@pytest_asyncio.fixture
async def ads_client(async_db_session):
    """The REAL ads router on a minimal app.

    Deliberately not `app.main`: this route's contract is the router + its response models, and
    importing the whole product app makes a lane-local truth test hostage to unrelated routers.
    """
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


async def _seed_campaign_and_metrics(async_db_session) -> int:
    campaign = AdCampaign(platform="google_ads", name="c1", status="active")
    async_db_session.add(campaign)
    await async_db_session.flush()
    async_db_session.add_all([
        # both measured: 20 / 8 = 2.5
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 1), impressions=100, clicks=10,
                 conversions=2, spend_usd=Decimal("8.0000"), revenue_usd=Decimal("20.0000")),
        # revenue unreported -> the column default 0; spend measured.  Must NOT serve 0.0 / 0.0.
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 2), impressions=80, clicks=4,
                 conversions=0, spend_usd=Decimal("5.0000"), revenue_usd=Decimal("0.0000")),
        # revenue measured, spend zero -> revenue is a reading, ROAS is unmeasurable.
        AdMetric(campaign_id=campaign.id, date=date(2026, 10, 3), impressions=10, clicks=1,
                 conversions=1, spend_usd=Decimal("0.0000"), revenue_usd=Decimal("6.0000")),
    ])
    await async_db_session.commit()
    return campaign.id


async def test_api_serves_measured_revenue_and_computes_roas(ads_client, async_db_session) -> None:
    campaign_id = await _seed_campaign_and_metrics(async_db_session)

    response = await ads_client.get(
        f"/api/v1/ads/campaigns/{campaign_id}/metrics", headers=HEADERS
    )
    assert response.status_code == 200, response.text[:400]
    rows = {row["date"]: row for row in response.json()}

    measured = rows["2026-10-01"]
    assert measured["revenue_usd"] == 20.0
    assert measured["roas"] == 2.5

    assert rows["2026-10-02"]["revenue_usd"] is None, "an unreported revenue is not a $0 reading"
    assert rows["2026-10-02"]["roas"] is None, "0-as-missing must never be served as a ratio"

    zero_spend = rows["2026-10-03"]
    assert zero_spend["revenue_usd"] == 6.0
    assert zero_spend["roas"] is None, "revenue/spend is unmeasurable when spend is zero"
