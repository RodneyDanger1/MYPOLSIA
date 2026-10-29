"""WAVE 4 — F-W3-1 backend stance: a configured `0` is never rendered as a measured one.

THE LIVE DEFECT (wave 3, finding F-W3-1).  The live Cinder & Lye profile stores
`kpis {"mrr_usd": 0, "active_customers": 0}` — values the OWNER typed into Settings.  The dashboard
wire had already been fixed to declare `kpis_provenance: "configured"` and to carry the measured
headline separately (`measured_finance.mrr_cents: null`), but the AGENT-FACING renderings were not:
the `business_state` prompt layer printed

    KPIs:
    - active_customers: 0
    - mrr_usd: 0

with no source, and finance run 313 then reported *"Company KPI active_customers = 0"* as a company
fact on the same stack where `/finance/summary` said MRR and subscribers were UNMEASURED.  Same
file, second surface: `build_context_prompt` (the CLI fallback) printed the same bare heading.

THE STANCE UNDER TEST — configured 0 != measured 0:

  * `company_service.kpis_provenance(kpis)` is the ONE rule: `configured` when the row holds KPI
    keys, `unset` when it holds none.  The dashboard route and the agent context both call it, so
    the two surfaces cannot drift;
  * every KPI rendering names the source ("CONFIGURED - owner-entered Settings values, NOT
    measurements") and says a configured KPI must never be reported as a measured figure;
  * there is no `measured` KPI state in this app, and this suite does not invent one — measured
    money stays in `revenue_snapshots` (`/finance/summary`, the dashboard's `measured_finance`);
  * `owner_budget_usd` rides the company context so the money agent's break-even has a recorded
    denominator (it was previously absent from every agent's context).

The RED leg (the provenance label removed from the prompt renderer) fails the two prompt tests
below; the restore turns them green.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.finance.agent import FinanceAgent
from app.models.company import CompanyConfig
from app.services.company_service import (
    KPI_PROVENANCE_CONFIGURED,
    KPI_PROVENANCE_UNSET,
    build_context_prompt,
    get_full_context,
    invalidate_company_context,
    kpis_provenance,
)

LIVE_KPIS = {"mrr_usd": 0, "active_customers": 0}


async def _company(db, **overrides) -> CompanyConfig:
    values = {
        "name": "Cinder & Lye",
        "mission": "Make a small, honest home-fragrance line.",
        "timezone": "UTC",
        "daily_cycle_hour": 6,
        "kpis": LIVE_KPIS,
        "owner_budget_usd": Decimal("125.00"),
        "pricing_model": {"currency": "USD", "skus": [{"name": "9 oz candle", "retail": 26}]},
    }
    values.update(overrides)
    company = CompanyConfig(**values)
    db.add(company)
    await db.commit()
    invalidate_company_context()
    return company


@pytest.fixture(autouse=True)
def _clean_context_cache():
    invalidate_company_context()
    yield
    invalidate_company_context()


# ── 1. the ONE rule ────────────────────────────────────────────────────────────────────────────


def test_the_provenance_rule_distinguishes_configured_values_from_no_values() -> None:
    assert kpis_provenance(LIVE_KPIS) == "configured"
    assert kpis_provenance({"mrr_usd": None}) == "configured", (
        "an explicitly-cleared KPI is still a configured (stored) value — null is not absent"
    )
    assert kpis_provenance({}) == "unset"
    assert kpis_provenance(None) == "unset"
    assert kpis_provenance(["not", "a", "mapping"]) == "unset", (
        "a mis-shaped cell reads as unset (the dashboard coerces the same way)"
    )
    assert (KPI_PROVENANCE_CONFIGURED, KPI_PROVENANCE_UNSET) == ("configured", "unset")


# ── 2. the agent context carries the provenance and the recorded budget ────────────────────────


@pytest.mark.asyncio
async def test_the_agent_context_carries_the_kpi_provenance_and_the_budget(async_db_session) -> None:
    await _company(async_db_session)

    context = await get_full_context(async_db_session, use_cache=False)

    assert context["kpis"] == LIVE_KPIS, "the owner's values are carried verbatim"
    assert context["kpis_provenance"] == "configured"
    assert context["company"]["owner_budget_usd"] == Decimal("125.00"), (
        "the recorded launch budget reaches the money agent (break-even denominator)"
    )


@pytest.mark.asyncio
async def test_an_empty_kpi_row_reads_unset_not_configured(async_db_session) -> None:
    await _company(async_db_session, kpis={})
    context = await get_full_context(async_db_session, use_cache=False)
    assert context["kpis"] == {}
    assert context["kpis_provenance"] == "unset"


@pytest.mark.asyncio
async def test_an_unset_budget_stays_none_never_zero(async_db_session) -> None:
    await _company(async_db_session, owner_budget_usd=None)
    context = await get_full_context(async_db_session, use_cache=False)
    assert context["company"]["owner_budget_usd"] is None


# ── 3. the prompt renderings say where the KPIs came from ─────────────────────────────────────


def test_the_business_state_layer_labels_a_configured_kpi_as_configured() -> None:
    """The exact layer run 313 read: `KPIs:\\n- active_customers: 0` with no source."""
    agent = FinanceAgent()
    context = {"kpis": LIVE_KPIS, "kpis_provenance": "configured"}
    layer = agent.layers_for(
        {"id": 1, "title": "Unit economics", "description": "cost it"}, context
    )["business_state"].body()

    assert "KPIs (CONFIGURED - owner-entered Settings values, NOT measurements):" in layer
    assert "- active_customers: 0" in layer, "the configured 0 is still shown (it is the owner's data)"
    assert "never report it as a measured figure" in layer
    assert "KPIs:\n" not in layer, "the bare measurement-shaped heading is what the defect was"


def test_a_caller_that_passes_no_provenance_still_never_claims_measurement() -> None:
    """Default is honest: the only KPI writer is the profile, so an unlabelled list is configured."""
    agent = FinanceAgent()
    layer = agent.layers_for({"id": 1, "title": "t", "description": "d"},
                             {"kpis": LIVE_KPIS})["business_state"].body()
    assert "CONFIGURED" in layer
    assert "(MEASURED)" not in layer


def test_the_cli_fallback_prompt_carries_the_same_label() -> None:
    """`build_context_prompt` is a second rendering of the same numbers — it must not lie either."""
    text = build_context_prompt(
        {"company": {"name": "Cinder & Lye"}, "kpis": LIVE_KPIS, "kpis_provenance": "configured"}
    )
    assert "KPIS (CONFIGURED - owner-entered Settings values, NOT measurements):" in text
    assert "never be reported as a measured figure" in text


def test_an_empty_kpi_map_is_still_rendered_as_unmeasured_not_zero() -> None:
    agent = FinanceAgent()
    layer = agent.layers_for({"id": 1, "title": "t", "description": "d"},
                             {"kpis": {}, "kpis_provenance": "unset"})["business_state"].body()
    assert "unmeasured (no KPI values in context)" in layer


# ── 4. the dashboard wire uses the SAME rule (no drift between wire and prompt) ───────────────


@pytest.mark.asyncio
async def test_the_dashboard_declares_configured_for_the_live_profile(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session)
    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()

    assert data["kpis"] == LIVE_KPIS
    assert data["kpis_provenance"] == "configured"
    # the measured side is separate and says unmeasured — never 0
    assert data["measured_finance"]["mrr_cents"] is None
    assert data["measured_finance"]["active_subscribers"] is None


@pytest.mark.asyncio
async def test_the_dashboard_declares_unset_when_no_kpi_value_is_configured(
    async_db_session, api_client, auth_headers
) -> None:
    await _company(async_db_session, kpis={})
    data = (await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()
    assert data["kpis"] == {}
    assert data["kpis_provenance"] == "unset"


@pytest.mark.asyncio
async def test_the_wire_and_the_agent_context_agree_on_the_same_row(async_db_session) -> None:
    """One row, two surfaces, one label — the drift class F-W3-1 was about."""
    await _company(async_db_session)
    context = await get_full_context(async_db_session, use_cache=False)
    assert context["kpis_provenance"] == kpis_provenance(context["kpis"])
