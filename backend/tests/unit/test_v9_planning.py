"""V9 B2: the planning endpoint, the pure planning math helpers, and the daily report's cash/tax block.

Frozen shape (plan §9 lines 451-456): GET /api/v1/finance/planning answers burn_line_items /
capacity / margin_template; the daily report carries cash_block.  The ruling the tests enforce:
anything unmeasured is null + a reason — NEVER 0 (the empty-database run below must contain no
fabricated zero anywhere in the planning body).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.database import utcnow
from app.core.timeutil import today_utc
from app.models.company import CompanyConfig
from app.models.finance import ExpenseRecord, RevenueSnapshot, StripeEvent
from app.models.report import DailyReport

TAX_LINE = "Not tax advice — verify with MN Revenue."


@pytest.mark.asyncio
async def test_planning_is_null_honest_on_an_empty_database(api_client, auth_headers):
    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["burn_line_items"] == []
    capacity = body["capacity"]
    for key in ("hours_per_client_lo", "hours_per_client_hi", "max_clients", "mrr_at_capacity", "break_even_clients"):
        assert capacity[key] is None, f"{key} must be null when unmeasured, never 0"
    for key in ("price", "ai_cost", "human_hours", "human_rate", "margin"):
        assert body["margin_template"][key] is None, f"margin_template.{key} must be null when unmeasured"
    assert body["pricing_guardrails"] == []
    assert body["tax_disclaimer"] == TAX_LINE
    assert body["sales_tax_note"]
    # null + REASON: every nulled field is explained
    assert "capacity.max_clients" in body["reasons"]
    assert "margin_template.human_rate" in body["reasons"]
    # the dated-snapshot provenance rides every catalog-derived response (owner amendment)
    assert body["catalog_as_of"] == "2026-10-14"
    expected_stale = (today_utc() - date(2026, 10, 14)).days >= 180
    assert body["stale"] is expected_stale
    # UTC-anchored: the app computes provenance in UTC; a local `date.today()` here flips the
    # assertion for every run between 00:00 and 07:00 UTC (19:00+ CDT). Measured red 2026-10-18.
    assert body["staleness_days"] == (today_utc() - date(2026, 10, 14)).days


@pytest.mark.asyncio
async def test_planning_capacity_math_on_fixed_inputs(api_client, auth_headers, async_db_session):
    company = CompanyConfig(
        name="Test Co",
        owner_hours_per_week=40,
        confirmed_idea={"name": "SEO — small business", "price_band": [1500, 3000], "one_liner": "Full SEO program"},
    )
    async_db_session.add(company)
    from app.models.task import AgentRun

    async_db_session.add(AgentRun(agent_type="social_media", status="completed",
                                  cost_usd=Decimal("60"), started_at=utcnow()))
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    capacity = body["capacity"]
    assert capacity["hours_per_client_lo"] == 15
    assert capacity["hours_per_client_hi"] == 25
    assert capacity["hours_per_client_source"] == "SEO — small business"
    # 40 h/week -> 40 * 52/12 = 173.33 h/month; floor(173.33 / 25 h per client) = 6 clients at quality
    assert capacity["max_clients"] == 6
    assert capacity["mrr_at_capacity"] == 9000.0  # 6 clients x the low band ($1,500)
    # AI cost is an ALLOCATION of measured 30-day spend over clients at capacity: 60 / 6 = 10
    assert body["margin_template"]["price"] == 1500.0
    assert body["margin_template"]["ai_cost"] == 10.0
    assert body["margin_template"]["human_hours"] == 25.0
    assert body["margin_template"]["human_rate"] is None  # no rate configured anywhere
    assert body["margin_template"]["margin"] is None
    assert "margin_template.human_rate" in body["reasons"]


def test_margin_and_break_even_math_is_exact():
    from app.api.v1.finance import break_even_clients, margin_fraction

    assert margin_fraction(1000, 100, 10, 30) == 0.6  # (1000 - (100 + 10*30)) / 1000
    assert margin_fraction(1000, None, 10, 30) is None
    assert margin_fraction(1000, 100, 10, 30) is not None
    assert margin_fraction(100, 100, 1, 5) is None  # non-positive contribution is not a margin
    # contribution = 1000 - (100 + 10*30) = 600: burn 600 -> 1 client, burn 1200 -> 2 clients
    assert break_even_clients(600, 1000, 100, 10, 30) == 1
    assert break_even_clients(1200, 1000, 100, 10, 30) == 2
    assert break_even_clients(None, 1000, 100, 10, 30) is None
    assert break_even_clients(600, 100, 100, 1, 5) is None


def test_pricing_guardrails_price_the_three_antipatterns():
    from app.api.v1.finance import pricing_guardrails_for

    hourly = pricing_guardrails_for({"one_liner": "Consulting billed hourly at $150/hr"}, None)
    assert hourly and any("hourly" in warning.lower() for warning in hourly)

    rev_share = pricing_guardrails_for({"one_liner": "Performance pricing: 10% rev-share on sales"}, None)
    assert rev_share and any("rev-share" in warning.lower() or "share" in warning.lower() for warning in rev_share)

    uncapped = pricing_guardrails_for({"one_liner": "Flat fee, uncapped AI usage included"}, None)
    assert uncapped and any("flat" in warning.lower() for warning in uncapped)

    assert pricing_guardrails_for({"one_liner": "Fixed-scope productized tiers with usage caps"}, None) == []
    assert pricing_guardrails_for(None, None) == []
    assert pricing_guardrails_for({"one_liner": "Hourly work"}, {"model": "hourly"})


@pytest.mark.asyncio
async def test_cash_block_rides_the_daily_report_null_honest(api_client, auth_headers, async_db_session):
    async_db_session.add(DailyReport(report_date=today_utc()))  # UTC anchor — the API reads UTC days
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/reports/daily", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    block = resp.json()["cash_block"]
    assert block["cash_on_hand"] is None
    assert block["in_30d"] is None
    assert block["out_30d"] is None
    assert block["tax_set_aside"] is None
    assert len(block["next_tax_dates"]) == 4
    assert block["safe_harbor_note"]
    assert block["tax_disclaimer"] == TAX_LINE
    assert block["sales_tax_note"]
    assert block["reasons"], "every null in the cash block carries a reason"


@pytest.mark.asyncio
async def test_cash_block_measured_readings(api_client, auth_headers, async_db_session):
    today = today_utc()  # UTC anchor — the cash block reads UTC-dated rows
    async_db_session.add(DailyReport(report_date=today))
    async_db_session.add(RevenueSnapshot(snapshot_date=today, stripe_balance_cents=12345))
    async_db_session.add(ExpenseRecord(date=today, category="tools", vendor="n8n", amount_cents=4000, currency="usd"))
    async_db_session.add(ExpenseRecord(date=today, category="taxes", vendor="MN Revenue", amount_cents=1000, currency="usd"))
    async_db_session.add(StripeEvent(stripe_event_id="evt_v9_in", event_type="charge.succeeded", amount_cents=10000))
    async_db_session.add(StripeEvent(stripe_event_id="evt_v9_old", event_type="charge.succeeded", amount_cents=7777,
                                     processed_at=utcnow() - timedelta(days=40)))
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/reports/daily", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    block = resp.json()["cash_block"]
    assert block["cash_on_hand"] == 123.45
    assert block["in_30d"] == 100.0
    assert block["out_30d"] == 50.0
    assert block["tax_set_aside"] == 10.0
