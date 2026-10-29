"""Wave A3 / S6 — SPEND TRUTH: money is measured, or it says UNKNOWN.

Every test here fails on the pre-fix tree; the assertion that failed is named in its docstring
so the RED leg is not a matter of memory.

Findings covered
----------------
A2A7-01  `AdCampaign.total_spent_usd` defaulted to a fabricated `$0.00` and `record_ads_run`
         never refreshed it: a campaign that later reported $250 still read $0.00.
A2A7-03  the currency-blind expense/inflow sums — the finance summary's month total and the
         evening cash block's `out_30d` / `in_30d` / `tax_set_aside` — added every currency and
         served the result as dollars, with nothing on the payload to say a row was left out.
A1-03 / A8-02 / F-19 / F-20  the planner's spend gate read an unmeasured day as `$0.00` and
         ALLOWED.  A finished run with no `cost_usd` (an unpriced served-model alias, or a failed
         run whose measurement was dropped) must refuse; a run still IN FLIGHT must not.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import utcnow
from app.core.errors import E_BUDGET_EXCEEDED, MeridianError
from app.core.timeutil import today_in
from app.models.ads import AdCampaign
from app.models.finance import ExpenseRecord, StripeEvent
from app.models.task import AgentRun

# ── A2A7-01: the ad spend the owner is shown ------------------------------------------------


def _run_payload(**campaign) -> dict:
    """One `ads_management` result exactly as the worker hands it to `record_ads_run`."""
    entry = {"platform": "google_ads", "external_id": "camp-77", "name": "Spring launch"}
    entry.update(campaign)
    return {"campaigns": [entry], "metrics": []}


async def _stored_campaign(db) -> AdCampaign:
    return (await db.execute(select(AdCampaign).order_by(AdCampaign.id))).scalars().one()


@pytest.mark.asyncio
async def test_an_unreported_campaign_spend_is_unknown_not_a_measured_zero(async_db_session):
    """RED (pre-fix): the row persisted `Decimal('0.0000')` and the summary served 0.0.

    An ad run that reports no `total_spent_usd` measured NOTHING.  The column must read NULL
    (`None` on the wire) — the same law `ad_metrics.spend_usd`/`revenue_usd` already carry —
    and the finance summary must serve `None`, never a fabricated `$0.00`.
    """
    from app.services.ads_service import record_ads_run
    from app.services.finance_service import get_finance_summary

    report = await record_ads_run(async_db_session, _run_payload())
    assert report["ok"] is True, report

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd is None, "an unreported spend is UNKNOWN, not a measured zero"
    assert stored.daily_budget_usd is None

    summary = await get_finance_summary(async_db_session)
    assert summary["total_ad_spend_usd"] is None, "the card must read an em dash, not $0.00"


@pytest.mark.asyncio
async def test_a_campaign_created_without_money_is_unknown_not_zero(async_db_session):
    """RED (pre-fix): `create_campaign` persisted the column DEFAULT `0` for both money columns.

    The manual/API create path is the second writer of the same column: a campaign nobody has
    measured must not be born as a measured `$0.00` either.
    """
    from app.services.ads_service import create_campaign

    campaign = await create_campaign(async_db_session, "google_ads", "Hand-made")
    stored = await async_db_session.scalar(
        select(AdCampaign.total_spent_usd).where(AdCampaign.id == campaign.id)
    )
    assert stored is None
    assert campaign.daily_budget_usd is None


@pytest.mark.asyncio
async def test_a_measured_zero_is_still_a_measured_zero(async_db_session):
    """Guard on the fix: only ABSENCE is unknown — a reported `0.0` is a real $0.00 day."""
    from app.services.ads_service import record_ads_run

    await record_ads_run(async_db_session, _run_payload(total_spent_usd=0.0, daily_budget_usd=0.0))

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("0.0000")
    assert stored.daily_budget_usd == Decimal("0.0000")


@pytest.mark.asyncio
async def test_a_later_run_refreshes_the_campaigns_cumulative_spend(async_db_session):
    """RED (pre-fix): `duplicates_skipped=1`, the row stayed `0.0000` and `campaigns_updated` did not exist.

    `total_spent_usd` is a CUMULATIVE provider measure: the 6h beat reporting $250 for a campaign
    first seen at $10 must move the row (and the finance summary) to 250.
    """
    from app.services.ads_service import record_ads_run
    from app.services.finance_service import get_finance_summary

    first = await record_ads_run(async_db_session, _run_payload(total_spent_usd=10.0))
    assert first["campaigns_written"] == 1

    second = await record_ads_run(async_db_session, _run_payload(total_spent_usd=250.0))
    assert second["ok"] is True, second
    assert second["duplicates_skipped"] == 1, "the campaign is the same provider id"
    assert second["campaigns_updated"] == 1, "a refreshed measurement is a write the report shows"

    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("250.0000")
    summary = await get_finance_summary(async_db_session)
    assert summary["total_ad_spend_usd"] == Decimal("250.0000")


@pytest.mark.asyncio
async def test_a_repeat_run_with_the_same_spend_writes_nothing(async_db_session):
    """Idempotency: the beat re-reports the same cumulative number every 6h — that is not an update."""
    from app.services.ads_service import record_ads_run

    await record_ads_run(async_db_session, _run_payload(total_spent_usd=250.0))
    again = await record_ads_run(async_db_session, _run_payload(total_spent_usd=250.0))

    assert again["duplicates_skipped"] == 1
    assert again["campaigns_updated"] == 0, "an unchanged number is not a refresh"


@pytest.mark.asyncio
async def test_a_run_that_reports_no_spend_does_not_erase_the_measured_total(async_db_session):
    """An unreported value never overwrites a measured one — the refresh is reported-values only."""
    from app.services.ads_service import record_ads_run

    await record_ads_run(async_db_session, _run_payload(total_spent_usd=250.0))
    silent = await record_ads_run(async_db_session, _run_payload())

    assert silent["campaigns_updated"] == 0
    stored = await _stored_campaign(async_db_session)
    assert stored.total_spent_usd == Decimal("250.0000")


# ── A2A7-03: the currency-blind sums --------------------------------------------------------


async def _usd_expense(db, *, cents: int, category: str = "ads", vendor: str = "Google") -> None:
    from app.services.finance_service import record_expense

    await record_expense(
        db, category=category, vendor=vendor, amount_cents=cents, currency="USD", date=today_in()
    )


@pytest.mark.asyncio
async def test_the_month_expense_total_is_usd_only_and_names_the_excluded_rows(async_db_session):
    """RED (pre-fix): `total_expenses_month_cents` read 60000 — a EUR vendor was counted as dollars.

    10000 cents USD + 50000 cents EUR in the same month: the USD total is 10000 and the payload
    says one row was excluded (rather than silently converting or silently dropping it).
    """
    from app.services.finance_service import get_finance_summary, record_expense

    await _usd_expense(async_db_session, cents=10000)
    await record_expense(
        async_db_session,
        category="ads",
        vendor="Vendor GmbH",
        amount_cents=50000,
        currency="EUR",
        date=today_in(),
    )

    summary = await get_finance_summary(async_db_session)
    assert summary["total_expenses_month_cents"] == 10000, "a EUR row is not 50000 USD cents"
    assert summary["foreign_expenses_month_count"] == 1, "the exclusion is COUNTED, never silent"


@pytest.mark.asyncio
async def test_an_unset_currency_is_unknown_not_usd(async_db_session):
    """A NULL currency is a raw/ops writer leaving it unset: excluded from the total AND counted.

    Written with raw SQL on purpose: `expense_records.currency` carries `DEFAULT 'usd'`
    (`models/finance.py:64-66`), so an ORM insert can never produce the NULL this rule is about —
    the fixture must model the writer the rule exists for (the same shape
    `tests/unit/api/test_r26_ads_edges.py:302` uses for its nullable-column cases).
    """
    from sqlalchemy import text

    from app.services.finance_service import get_finance_summary

    await async_db_session.execute(
        text(
            "INSERT INTO expense_records (category, vendor, amount_cents, currency, date)"
            " VALUES ('ads', 'raw-sql', 7000, NULL, :day)"
        ),
        {"day": today_in().isoformat()},
    )
    stored = await async_db_session.scalar(
        select(ExpenseRecord.currency).where(ExpenseRecord.vendor == "raw-sql")
    )
    assert stored is None, "the fixture must really carry the NULL currency"

    summary = await get_finance_summary(async_db_session)
    assert summary["total_expenses_month_cents"] is None, "unknown currency is not dollars"
    assert summary["foreign_expenses_month_count"] == 1


@pytest.mark.asyncio
async def test_the_cash_block_sums_usd_only_and_labels_every_excluded_figure(async_db_session):
    """RED (pre-fix): out_30d 600.0 / in_30d 550.0 / tax 20.0 and no exclusion label at all.

    USD 10000c expense + EUR 50000c expense, a EUR 2000c tax expense, a USD 5000c Stripe charge
    and a EUR 50000c one.  Every figure is USD-only and each affected field carries its reason.
    """
    from app.services.report_service import build_cash_block

    await _usd_expense(async_db_session, cents=10000)
    async_db_session.add_all(
        [
            ExpenseRecord(category="ads", vendor="Vendor GmbH", amount_cents=50000,
                          currency="eur", date=today_in()),
            ExpenseRecord(category="taxes", vendor="Berlin Finanzamt", amount_cents=2000,
                          currency="eur", date=today_in()),
            StripeEvent(stripe_event_id="evt_usd_in", event_type="charge.succeeded",
                        amount_cents=5000, currency="usd", processed_at=utcnow()),
            StripeEvent(stripe_event_id="evt_eur_in", event_type="charge.succeeded",
                        amount_cents=50000, currency="eur", processed_at=utcnow()),
        ]
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)
    assert block["out_30d"] == 100.0, "the €500 vendor is not $500 of outflow"
    assert block["in_30d"] == 50.0, "the €500 charge is not $500 of money in"
    assert block["tax_set_aside"] == 0.0, "the €20 tax row is not $20 set aside"
    assert block["foreign_currency_rows"] == 3, "2 expense rows + 1 money-in event, COUNTED"
    reasons = block["reasons"]
    assert "excluded" in reasons["out_30d.currency"].lower()
    assert "excluded" in reasons["in_30d.currency"].lower()
    assert "excluded" in reasons["tax_set_aside.currency"].lower()


@pytest.mark.asyncio
async def test_a_ledger_that_is_only_foreign_currency_reports_unmeasured(async_db_session):
    """RED (pre-fix): the block served `out_30d=500.0` / `tax_set_aside=500.0` as dollars.

    With no USD row at all the USD outflow is UNMEASURED, and the reason names the currency
    rather than pretending the ledger was empty.
    """
    from app.services.report_service import build_cash_block

    async_db_session.add(
        ExpenseRecord(category="taxes", vendor="Vendor GmbH", amount_cents=50000,
                      currency="eur", date=today_in())
    )
    await async_db_session.flush()

    block = await build_cash_block(async_db_session)
    assert block["out_30d"] is None
    assert block["tax_set_aside"] is None
    assert block["foreign_currency_rows"] == 1
    assert "currency" in block["reasons"]["out_30d"]


# ── A1-03 / A8-02 / F-19 / F-20: the planner gate ------------------------------------------


def _run(*, cost: str | None, finished: bool, agent: str = "competitor_research") -> AgentRun:
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    return AgentRun(
        agent_type=agent,
        status="completed" if finished else "running",
        cost_usd=None if cost is None else Decimal(cost),
        started_at=started,
        ended_at=started + timedelta(minutes=1) if finished else None,
    )


@pytest.mark.asyncio
async def test_the_gate_refuses_a_finished_run_with_no_cost(async_db_session):
    """RED (pre-fix): the gate folded the NULL into `$0` and allowed the next `$2.00` step.

    One measured $3.00 run beside one FINISHED run whose cost was never written (an unpriced
    served-model alias, or F-20's failed attempt): the day cannot be summed, so the gate refuses
    BY NAME instead of spending against an invented zero.
    """
    from app.services import planner_service

    async_db_session.add_all([_run(cost="3.0000", finished=True), _run(cost=None, finished=True)])
    await async_db_session.commit()

    with pytest.raises(MeridianError) as excinfo:
        await planner_service._gate_planner_budget(
            async_db_session, "company_planner", step="propose"
        )

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert "UNMEASURED" in excinfo.value.message
    assert excinfo.value.context["unmeasured_runs"] == 1
    assert excinfo.value.how_to_fix, "a refusal must say how to fix it"


@pytest.mark.asyncio
async def test_an_in_flight_run_does_not_stop_the_gate(async_db_session):
    """A run still open is not lost accounting: its cost is bounded by the per-run cap.

    RED (pre-fix, arity): there was no reader that could even report `in_flight`; the new
    `day_spend_truth` must sum the measured rows and count the open one without pretending
    it is $0.00.
    """
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    async_db_session.add_all([_run(cost="3.0000", finished=True), _run(cost=None, finished=False)])
    await async_db_session.commit()

    await planner_service._gate_planner_budget(async_db_session, "company_planner", step="propose")

    truth = await day_spend_truth(async_db_session)
    assert truth.total == Decimal("3.0000")
    assert truth.in_flight == 1
    assert truth.finished_unpriced == 0
    assert truth.measured_runs == 1


@pytest.mark.asyncio
async def test_a_day_of_only_unpriced_runs_is_unknown_not_zero(async_db_session):
    """RED (pre-fix): two NULL-cost runs read `$0.00` (the coalesce) and the gate allowed.

    This is F-19's shape end to end: every run of an aliased/renamed served model prices to
    NULL, so the day has no measured dollar at all.
    """
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    async_db_session.add_all([_run(cost=None, finished=True), _run(cost=None, finished=False)])
    await async_db_session.commit()

    truth = await day_spend_truth(async_db_session)
    assert truth.total is None
    assert truth.per_agent == {}, "no fabricated $0.00 per-agent entry"

    with pytest.raises(MeridianError) as excinfo:
        await planner_service._gate_planner_budget(
            async_db_session, "company_planner", step="propose"
        )
    assert excinfo.value.code == E_BUDGET_EXCEEDED


@pytest.fixture
async def company(async_db_session):
    from app.models.company import CompanyConfig

    row = CompanyConfig(
        name="Spend Truth Co", owner_hours_per_week=10, owner_budget_usd=Decimal("500.00")
    )
    async_db_session.add(row)
    await async_db_session.commit()
    return row


def _spies(monkeypatch) -> list[str]:
    from app.services import planner_service

    calls: list[str] = []
    fetched = "https://research.example/pricing"
    idea = {
        "name": "Cafe launch kit",
        "one_liner": "one page + email sequence for one cafe",
        "buyer": "independent cafe owners",
        "price_band": [100.0, 150.0],
        "channel": "stripe payment link",
        "unit_cost_sketch": "2h setup",
        "est_variable_cost": 20.0,
        "launch_cost_usd": 50.0,
        "days_to_first_dollar": 10,
        "competition_level": "medium",
        "fleet_fit": "high",
        "demand_grade": "likely",
        "owner_hours_per_week_needed": 4,
        "claims": [{"claim": "cafes pay $100+", "source_url": fetched, "quote": "from $100"}],
    }
    monkeypatch.setattr(
        planner_service,
        "_research_sync",
        lambda profile: calls.append("research") or {
            "summary": "notes",
            "competitors": [{"name": "Incumbent", "source_urls": [fetched], "sources": [fetched]}],
            "findings": ["prices from $100"],
            "search_queries": ["q1"],
        },
    )
    monkeypatch.setattr(
        planner_service,
        "_proposal_sync",
        lambda profile, research: calls.append("model") or {
            "ideas": [{**idea, "name": f"{idea['name']} {i}"} for i in range(3)],
            "agent_type": "company_planner",
        },
    )
    return calls


@pytest.mark.asyncio
async def test_the_propose_route_refuses_before_any_agent_work(
    async_db_session, company, monkeypatch, mock_redis
):
    """Wiring proof: the refusal is on the REAL `/planner/propose` path, before any agent step."""
    from app.services import planner_service

    calls = _spies(monkeypatch)
    async_db_session.add(_run(cost="3.0000", finished=True))
    async_db_session.add(_run(cost=None, finished=True))
    await async_db_session.commit()

    with pytest.raises(MeridianError) as excinfo:
        await planner_service.propose(async_db_session)

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert calls == [], "nothing may run once the day's spend is unmeasured"
