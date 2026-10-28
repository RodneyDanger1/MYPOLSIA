"""W2 TS-4 — the dashboard's KPI card is CONFIGURED data; the measured facts ride beside it.

THE DEFECT.  `GET /dashboard/summary` served `company_config.kpis` verbatim — values the OWNER
typed into Settings — and the dashboard page rendered them as "MRR / Active customers / Churn
rate".  A configured `mrr_usd: 0` therefore read as a measured `$0.00` on the same stack where
`GET /finance/summary` correctly answered `mrr_cents: null` (unmeasured).  Two surfaces disagreed
about one fact and the dashboard's copy was the false one.

THE FIX under test (backend half):
  * `kpis_provenance: "configured"` DECLARES where the KPI values come from;
  * `measured_finance` carries the newest snapshot's measured MRR / active subscribers / date and
    the TS-1 coverage marker, read from the SAME row `/finance/summary` uses — as extra columns
    of the ONE dashboard statement (the V24 one-round-trip law is re-asserted below);
  * the configured `kpis` themselves are unchanged (verbatim), because they are the owner's data —
    relabelling the copy is the frontend half (REQUESTS to weaver).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models.company import CompanyConfig
from app.models.finance import RevenueSnapshot

KPI_ZEROS = {"mrr_usd": 0, "churn_rate": 0, "active_customers": 0}
ALL_SEVEN = {
    "mrr_cents": 250000,
    "arr_cents": 3000000,
    "active_subscribers": 0,
    "churned_today": 0,
    "new_today": 2,
    "total_revenue_month_cents": 450000,
    "stripe_balance_cents": 125000,
}


async def _company(db, kpis: dict) -> None:
    db.add(CompanyConfig(name="TS4 Co", kpis=kpis, timezone="UTC", daily_cycle_hour=6))
    await db.commit()


@pytest.mark.asyncio
async def test_configured_zeros_are_declared_configured_and_measured_is_unmeasured(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session, KPI_ZEROS)

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The configured values are served verbatim (the owner's data is not rewritten) …
    assert data["kpis"] == KPI_ZEROS
    # … but their provenance is now declared on the wire.
    assert data["kpis_provenance"] == "configured"
    # … and the measured facts are present as their own block, saying "unmeasured" — never 0.
    assert data["measured_finance"] == {
        "mrr_cents": None,
        "active_subscribers": None,
        "last_snapshot_date": None,
        "provenance": "unmeasured",
    }

    # The finance summary agrees: the same fact is unmeasured on both surfaces.
    finance = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert finance.status_code == 200
    assert finance.json()["mrr_cents"] is None


@pytest.mark.asyncio
async def test_the_measured_block_serves_the_ledger_values_beside_the_configured_ones(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session, KPI_ZEROS)
    async_db_session.add(
        RevenueSnapshot(
            snapshot_date=date(2026, 10, 27),
            mrr_cents=250000,
            active_subscribers=0,  # a measured zero, not an absent value
            stripe_balance_cents=0,
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    data = resp.json()

    assert data["kpis"]["mrr_usd"] == 0, "the configured value is untouched"
    measured = data["measured_finance"]
    assert measured["mrr_cents"] == 250000
    assert measured["active_subscribers"] == 0
    assert measured["last_snapshot_date"] == "2026-10-27"
    assert measured["provenance"] == "partial"
    assert data["kpis_provenance"] == "configured"

    # The dashboard and the finance summary now quote the SAME measured MRR.
    finance = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert finance.json()["mrr_cents"] == measured["mrr_cents"]


@pytest.mark.asyncio
async def test_the_measured_block_is_complete_when_all_seven_columns_are_measured(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session, {"mrr_usd": 1234})
    async_db_session.add(RevenueSnapshot(snapshot_date=date(2026, 10, 28), **ALL_SEVEN))
    await async_db_session.commit()

    data = (
        await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    ).json()
    assert data["measured_finance"] == {
        "mrr_cents": ALL_SEVEN["mrr_cents"],
        "active_subscribers": ALL_SEVEN["active_subscribers"],
        "last_snapshot_date": "2026-10-28",
        "provenance": "complete",
    }


@pytest.mark.asyncio
async def test_the_dashboard_summary_is_still_one_statement(async_db_session) -> None:
    """TS-4's honesty must not cost a second round trip (the V24 optimisation law)."""
    from app.api.v1.dashboard import _summary_statement
    from app.services.query_helpers import fetch_one
    from tests.unit.test_v24_perf import count_statements

    with count_statements(async_db_session) as seen:
        await fetch_one(async_db_session, _summary_statement(async_db_session), what="dash")
    assert len(seen) == 1, "\n".join(seen)
