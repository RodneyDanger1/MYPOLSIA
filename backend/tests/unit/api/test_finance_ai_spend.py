"""R24 — `GET /finance/summary`'s additive `ai_spend` block, one measured state per test.

Consumer evidence for the block: `app/agents/accounting.py:276-335` ships `spend_summary`
("The ONE honest spend summary"), `burn_rate` ("Burn-rate view for the dashboard") and
`unit_economics` ("Cost per completed task") with no production caller at all; the route now
renders them from the real `agent_runs` day ledger, against the cap the dispatch gate enforces.

States pinned here (the module's "missing is not zero" discipline — D3: an ABSENT day is a
measured 0, an unmeasured one stays None):

* reported spend -> `spent`/`remaining` from the seeded rows, `burn_rate` present, `unit_economics`
  priced;
* over-cap -> `remaining` CLAMPED at 0.0 (never negative headroom);
* rows with no reported cost -> `spent`/`remaining`/`burn_rate` None — never a fabricated `$0.00`;
* a reported `0` -> a measured `0.0` (not None);
* no rows at all -> a measured `0.0` (not None), the gate's fresh-day state;
* rows outside the company-local day -> excluded;
* per-agent pricing covers only agents whose day spend was measured; the rest are NAMED in
  `unmeasured_spend_agents` (they must not be priced at a fake `$0.00`/task).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest


async def _seed_run(
    db: Any,
    *,
    agent_type: str = "finance",
    status: str = "completed",
    cost_usd: str | None = None,
    started_at: Any = None,
) -> Any:
    """One `agent_runs` row on the app's session.  `cost_usd=None` is the documented NULL state:
    the CLI never reported a cost (accounting.py:20-23) — NOT a measured zero."""
    from app.core.database import utcnow
    from app.models.task import AgentRun

    run = AgentRun(
        agent_type=agent_type,
        status=status,
        cost_usd=None if cost_usd is None else Decimal(cost_usd),
        started_at=utcnow() if started_at is None else started_at,
    )
    db.add(run)
    await db.commit()
    return run


def _pin_cap(monkeypatch: pytest.MonkeyPatch, cap_usd: float) -> None:
    """The cap the gate reads, pinned for this test (same field `budget_policy_from_settings`
    reads — see `tests/unit/celery/test_budget_gate.py::_policy`)."""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_daily_cost_cap_usd", cap_usd)


@pytest.mark.asyncio
async def test_ai_spend_block_reports_the_measured_day(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """A completed run that reported its cost: block, burn rate and unit economics all priced."""
    _pin_cap(monkeypatch, 10.0)
    await _seed_run(async_db_session, agent_type="finance", status="completed", cost_usd="2.5000")

    resp = await api_client.get("/api/v1/finance/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    ai = data["ai_spend"]
    assert ai is not None, "the ai_spend block must be present on the summary (R24 wire)"
    assert ai["limit"] == 10.0, "the cap is the settings field the dispatch gate enforces"
    assert ai["spent"] == 2.5
    assert ai["remaining"] == 7.5

    burn = ai["burn_rate"]
    assert burn is not None, "a measured spend is projectable"
    assert burn["spent_usd"] == 2.5
    assert burn["day_limit_usd"] == 10.0
    assert 0.0 <= burn["elapsed_fraction"] <= 1.0
    if burn["elapsed_fraction"] > 0:
        # The function's own math, recomputed from the elapsed fraction it reports (the wire
        # rounds it to 4 dp, so the comparison is approximate by construction).
        assert burn["projected_day_total_usd"] == pytest.approx(
            2.5 / max(0.05, burn["elapsed_fraction"]), rel=0.01
        )
    else:  # the instant the local day starts: the function refuses to project (never divides)
        assert burn["projected_day_total_usd"] is None
    assert burn["status"] in {"ok", "burning_fast", "over_budget_projection"}

    economics = ai["unit_economics"]
    assert economics is not None
    assert economics["total_spend_usd"] == 2.5
    assert economics["total_completed"] == 1
    assert economics["cost_per_completed_task_usd"] == 2.5
    assert economics["per_agent"] == {
        "finance": {"spend_usd": 2.5, "completed": 1, "cost_per_completed_task_usd": 2.5}
    }
    assert economics["unmeasured_spend_agents"] == []


@pytest.mark.asyncio
async def test_ai_spend_over_cap_clamps_remaining_at_zero(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """An over-cap day reports the real spend and 0.0 headroom — never a negative remaining."""
    _pin_cap(monkeypatch, 10.0)
    await _seed_run(async_db_session, agent_type="social_media", status="completed", cost_usd="30.0000")

    data = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()

    ai = data["ai_spend"]
    assert ai is not None
    assert ai["limit"] == 10.0
    assert ai["spent"] == 30.0
    assert ai["remaining"] == 0.0, "over-cap headroom clamps at 0, it never goes negative"
    assert ai["burn_rate"]["spent_usd"] == 30.0
    assert ai["unit_economics"]["cost_per_completed_task_usd"] == 30.0


@pytest.mark.asyncio
async def test_ai_spend_reported_zero_is_a_measured_zero(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """A run that reported `0` spend is a measured 0.0 — it must not collapse into "unmeasured"."""
    _pin_cap(monkeypatch, 25.0)
    await _seed_run(async_db_session, agent_type="finance", status="completed", cost_usd="0.0000")

    ai = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()["ai_spend"]

    assert ai is not None
    assert ai["spent"] == 0.0
    assert ai["spent"] is not None
    assert ai["remaining"] == 25.0
    assert ai["burn_rate"]["projected_day_total_usd"] in (None, 0.0)
    assert ai["burn_rate"]["status"] == "ok"
    assert ai["unit_economics"]["total_completed"] == 1
    assert ai["unit_economics"]["cost_per_completed_task_usd"] == 0.0


@pytest.mark.asyncio
async def test_ai_spend_absent_day_is_a_measured_zero(api_client, auth_headers, monkeypatch):
    """No `agent_runs` at all: a measured 0.0 — the same fresh-day state the dispatch gate sees
    (test_budget_gate.py: "measured spend 0 must not block a dispatch")."""
    _pin_cap(monkeypatch, 25.0)

    ai = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()["ai_spend"]

    assert ai is not None
    assert ai["limit"] == 25.0
    assert ai["spent"] == 0.0
    assert ai["spent"] is not None
    assert ai["remaining"] == 25.0
    economics = ai["unit_economics"]
    assert economics["total_spend_usd"] == 0.0
    assert economics["total_completed"] == 0
    assert economics["cost_per_completed_task_usd"] is None, "no completions measured -> unmeasured"
    assert economics["per_agent"] == {}
    assert economics["unmeasured_spend_agents"] == []


@pytest.mark.asyncio
async def test_ai_spend_stays_unmeasured_when_no_run_reported_a_cost(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """Runs exist but no cost was ever reported (a still-running run — cost NULL by contract):
    `spent`/`remaining` stay None (never `$0.00`) and no burn-rate projection is invented."""
    _pin_cap(monkeypatch, 25.0)
    await _seed_run(async_db_session, agent_type="finance", status="running", cost_usd=None)

    ai = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()["ai_spend"]

    assert ai is not None
    assert ai["limit"] == 25.0, "the cap is known even when the spend is not"
    assert ai["spent"] is None, "an unmeasured day is never dressed up as $0.00"
    assert ai["remaining"] is None
    assert ai["burn_rate"] is None, "a projection from an unmeasured spend would be fabricated"
    economics = ai["unit_economics"]
    assert economics["total_completed"] == 0
    assert economics["cost_per_completed_task_usd"] is None
    assert economics["per_agent"] == {}, "an unpriced agent is not given a $0.00 row"
    assert economics["unmeasured_spend_agents"] == ["finance"], "it is NAMED instead"


@pytest.mark.asyncio
async def test_ai_spend_window_excludes_rows_outside_the_company_local_day(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The window is the company-local day: a run that started one minute before the local
    midnight must not be counted into today's spend."""
    from app.core.timeutil import day_window

    _pin_cap(monkeypatch, 25.0)
    start, _end = day_window(async_db_session)
    await _seed_run(
        async_db_session,
        agent_type="finance",
        status="completed",
        cost_usd="2.0000",
        started_at=start - timedelta(minutes=1),
    )

    ai = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()["ai_spend"]

    assert ai is not None
    assert ai["spent"] == 0.0
    assert ai["unit_economics"]["unmeasured_spend_agents"] == []


@pytest.mark.asyncio
async def test_ai_spend_prices_only_agents_with_a_measured_day(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """Two agents: one reported its costs (2 completed runs), one has not — the priced set covers
    the reporter only, and the other is named rather than priced at a fabricated `$0.00`/task."""
    _pin_cap(monkeypatch, 25.0)
    await _seed_run(async_db_session, agent_type="finance", status="completed", cost_usd="3.0000")
    await _seed_run(async_db_session, agent_type="finance", status="completed", cost_usd="1.0000")
    await _seed_run(async_db_session, agent_type="social_media", status="running", cost_usd=None)

    ai = (await api_client.get("/api/v1/finance/summary", headers=auth_headers)).json()["ai_spend"]

    assert ai is not None
    assert ai["spent"] == 4.0
    economics = ai["unit_economics"]
    assert economics["total_spend_usd"] == 4.0
    assert economics["total_completed"] == 2
    assert economics["cost_per_completed_task_usd"] == 2.0
    assert economics["per_agent"] == {
        "finance": {"spend_usd": 4.0, "completed": 2, "cost_per_completed_task_usd": 2.0}
    }
    assert economics["unmeasured_spend_agents"] == ["social_media"]
