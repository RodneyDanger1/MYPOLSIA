"""WAVE 4 — the COGS seam: a recorded supplier cost can reach the money agent, honestly.

THE MEASURED GAP (run 313, 2026-10-28).  The finance agent was asked to cost the candle business and
REFUSED, correctly: *"No supplier, packaging, label, or shipping-supply cost appears anywhere in the
context or memories: every COGS line … is UNMEASURED … Stating a quantity would be fabrication."*
The company profile carried retail prices in `pricing_model` and nothing carried a per-unit COST, so
no contribution or break-even number could exist on any surface.  The supplier prices the research
lanes had sourced lived in documents the agent cannot read.

THE SEAM UNDER TEST (`app/services/cost_basis_service.py` + `app/schemas/company.py` +
`app/agents/finance/agent.py`):

  1. a per-SKU cost may be RECORDED on the profile beside its retail price, carrying its own
     provenance (`unit_cost: {amount_cents, kind, source, observed_at}`) and an optional catalogue
     `payment_fee`;
  2. the one write boundary (`PUT /config` → `CompanyConfigUpdate`) REFUSES an ambiguous or
     unprovenanced cost by path — a bare number, a float `amount_cents`, a `sourced` cost with no
     source/date, an unknown key, a currency the catalogue cannot be compared to;
  3. the finance agent's prompt carries a RECORDED UNIT ECONOMICS block in which unrecorded inputs
     are UNMEASURED (never 0), the derived contribution/break-even lines are labelled DERIVED, and
     the provenance of each cost is printed beside it;
  4. a PUBLIC page can never print the cost or its source (the storefront reads only selling
     prices) — pinned against the served pages AND the downloadable site package;
  5. a legacy bare `unit_cost: 9.4` (the wave-2 trial's dollars shape) still renders, labelled
     UNSOURCED and read as DOLLARS — never silently re-interpreted as cents.

Every assertion is named so the RED leg (the seam removed / a number fabricated) fails HERE.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from app.agents.finance.agent import FinanceAgent
from app.models.company import CompanyConfig
from app.services import site_package_service, site_service
from app.services.cost_basis_service import (
    CATALOGUE_LIST_KEYS,
    COST_STALENESS_DAYS,
    cost_basis_from_context,
    cost_input_errors,
    derive_unit_economics,
    recorded_skus,
    render_cost_basis_block,
)

TODAY = date(2026, 10, 29)
SOURCE = "https://printonic.com/blog/candle-dropshipping"
FEE_SOURCE = "https://stripe.com/pricing"

#: The live Cinder & Lye catalogue, with the bridge's SOURCED Printonic costs and Stripe fee.
MODEL = {
    "currency": "USD",
    "skus": [
        {
            "sku": "CL-WSL-9",
            "name": "White Sage + Lavender 9 oz",
            "retail": 26,
            "unit_cost": {
                "amount_cents": 949,
                "kind": "sourced",
                "source": SOURCE,
                "observed_at": "2026-10-28",
                "note": "9 oz POD base price, before shipping",
            },
        },
        {
            "sku": "CL-WSL-4",
            "name": "White Sage + Lavender 4 oz",
            "retail": 19,
            "unit_cost": {
                "amount_cents": 649,
                "kind": "sourced",
                "source": SOURCE,
                "observed_at": "2026-10-28",
            },
        },
        {"sku": "CL-DUO", "name": "Two-Candle Duo", "retail": 42},
    ],
    "payment_fee": {
        "percent": 2.9,
        "fixed_cents": 30,
        "kind": "sourced",
        "source": FEE_SOURCE,
        "observed_at": "2026-10-28",
    },
}

#: The LIVE profile before this wave: retail prices only, no cost anywhere (run 313's world).
NO_COSTS = {
    "currency": "USD",
    "skus": [
        {"sku": "CL-WSL-9", "name": "White Sage + Lavender 9 oz", "retail": 26},
        {"sku": "CL-WSL-4", "name": "White Sage + Lavender 4 oz", "retail": 19},
        {"sku": "CL-DUO", "name": "Two-Candle Duo", "retail": 42},
    ],
}


def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Cinder & Lye",
        "value_prop": "Light, then lather.",
        "description": "Soy candles poured in Los Angeles.",
        "mission": "An ordinary evening, made a ritual.",
        "notification_email": "hello@cinderlye.example",
        "timezone": "UTC",
        "daily_cycle_hour": 6,
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _block(model=MODEL, *, budget_cents=12500, today=TODAY) -> str:
    block = render_cost_basis_block(model, budget_cents=budget_cents, today=today)
    assert block is not None
    return block


# ── 1. the arithmetic: an INDEPENDENT recompute of the bridge's unit economics ────────────────


def test_the_derived_contribution_and_break_even_reproduce_the_independent_arithmetic() -> None:
    """9 oz at $26 with a $9.49 sourced cost and 2.9% + 30c: fee 105, contribution 1546, BE 9.

    Recomputed here from the formula, not copied from `bridge/UNIT-ECONOMICS.md`:
    fee = round(0.029 x 2600) + 30 = 75 + 30 = 105; contribution = 2600 - 949 - 105 = 1546
    (59.5% of retail); break-even = ceil(12500 / 1546) = ceil(8.08) = 9.
    """
    view = derive_unit_economics(MODEL, budget_cents=12500, today=TODAY)
    nine = next(row for row in view["rows"] if row["sku"] == "CL-WSL-9")

    assert nine["retail_cents"] == 2600
    assert nine["unit_cost_cents"] == 949
    assert nine["fee_cents"] == 105
    assert nine["contribution_cents"] == 1546
    assert nine["contribution_pct"] == 59.5
    assert nine["break_even_units"] == 9
    assert nine["computable"] is True

    four = next(row for row in view["rows"] if row["sku"] == "CL-WSL-4")
    # fee = round(0.029 x 1900) + 30 = 55 + 30 = 85; contribution = 1900 - 649 - 85 = 1166
    assert (four["fee_cents"], four["contribution_cents"], four["break_even_units"]) == (85, 1166, 11)


def test_a_sku_with_no_recorded_cost_is_unmeasured_never_zero() -> None:
    """CL-DUO records no cost: its contribution and break-even MUST be `None`, with a reason."""
    view = derive_unit_economics(MODEL, budget_cents=12500, today=TODAY)
    duo = next(row for row in view["rows"] if row["sku"] == "CL-DUO")

    assert duo["unit_cost_cents"] is None
    assert duo["contribution_cents"] is None
    assert duo["break_even_units"] is None
    assert duo["computable"] is False
    assert "no unit_cost is recorded" in duo["reason"]
    # and the block never turns that absence into a 0
    assert not re.search(r"unit_cost: 0 cents", _block())


def test_without_a_recorded_payment_fee_the_contribution_is_not_computed() -> None:
    """A fee the profile never recorded is not assumed: contribution stays unmeasured."""
    model = dict(MODEL)
    model.pop("payment_fee")
    view = derive_unit_economics(model, budget_cents=12500, today=TODAY)
    nine = next(row for row in view["rows"] if row["sku"] == "CL-WSL-9")

    assert nine["unit_cost_cents"] == 949, "the recorded cost is still reported"
    assert nine["fee_cents"] is None
    assert nine["contribution_cents"] is None
    assert nine["break_even_units"] is None
    assert "payment fee unmeasured" in nine["reason"]


def test_no_recorded_budget_means_no_break_even_number() -> None:
    view = derive_unit_economics(MODEL, budget_cents=None, today=TODAY)
    nine = next(row for row in view["rows"] if row["sku"] == "CL-WSL-9")
    assert nine["contribution_cents"] == 1546
    assert nine["break_even_units"] is None
    assert "no working budget" in nine["reason"]
    assert "WORKING BUDGET: UNMEASURED" in _block(budget_cents=None)


def test_a_non_positive_contribution_never_yields_a_break_even_count() -> None:
    """Cost above retail: the honest answer is "never", not a negative/absurd unit count."""
    model = {
        "currency": "USD",
        "skus": [
            {
                "sku": "LOSER",
                "name": "Sold below cost",
                "retail": 10,
                "unit_cost": {"amount_cents": 1200, "kind": "estimate"},
            }
        ],
        "payment_fee": {"percent": 2.9, "fixed_cents": 30, "kind": "estimate"},
    }
    view = derive_unit_economics(model, budget_cents=12500, today=TODAY)
    row = view["rows"][0]
    # fee = round(0.029 x 1000) + 30 = 29 + 30 = 59; contribution = 1000 - 1200 - 59 = -259
    assert row["contribution_cents"] == -259
    assert row["break_even_units"] is None
    assert "not positive" in row["reason"]


# ── 2. the provenance: sourced vs estimate vs unsourced vs legacy dollars ─────────────────────


def test_a_sourced_cost_prints_its_source_and_the_age_of_the_observation() -> None:
    block = _block()
    assert f"SOURCED {chr(0x2014)} recorded source {SOURCE}, observed 2026-10-28" in block
    assert "1 day(s) old" in block, "the block states the age of the observation, not just the date"


def test_a_stale_cost_is_flagged_with_the_same_180_day_rule_as_the_pricing_catalog() -> None:
    old = date(2026, 10, 28)
    later = date(old.year + 1, old.month, old.day)
    assert COST_STALENESS_DAYS == 180
    view = derive_unit_economics(MODEL, budget_cents=12500, today=later)
    nine = next(row for row in view["rows"] if row["sku"] == "CL-WSL-9")
    assert nine["cost_stale"] is True
    assert "STALE" in _block(today=later)


def test_an_estimate_is_labelled_as_an_assumption_not_an_observation() -> None:
    model = {
        "currency": "USD",
        "skus": [
            {
                "sku": "EST",
                "name": "Packaged unit",
                "retail": 20,
                "unit_cost": {"amount_cents": 800, "kind": "estimate", "note": "freight included"},
            }
        ],
        "payment_fee": {"percent": 2.9, "fixed_cents": 30, "kind": "estimate"},
    }
    block = render_cost_basis_block(model, budget_cents=1000, today=TODAY)
    assert block is not None
    assert f"ESTIMATE {chr(0x2014)} a stated assumption, not an observation" in block
    assert "SOURCED" not in block.split("PROVENANCE VOCABULARY")[0], (
        "an estimate must never be presented as a sourced price"
    )


def test_a_legacy_bare_unit_cost_is_read_as_DOLLARS_and_labelled_unsourced() -> None:
    """The wave-2 trial wrote `unit_cost: 9.4` beside `retail: 28` — dollars, not cents.

    It must still render (the data exists), labelled UNSOURCED and read as dollars: silently
    treating it as 9 cents, or hiding it, would both be lies about a stored number.
    """
    model = {"currency": "USD", "skus": [{"name": "Fireside", "retail": 28, "unit_cost": 9.4}]}
    rows = recorded_skus(model)

    assert rows[0].cost is not None
    assert rows[0].cost.amount_cents == 940, "9.4 dollars is 940 cents, not 9.4 cents"
    assert rows[0].cost.kind == "unsourced"
    assert rows[0].cost.recorded_as == "legacy_dollars"
    block = render_cost_basis_block(model, budget_cents=None, today=TODAY)
    assert block is not None
    assert f"UNSOURCED {chr(0x2014)} recorded with no source (legacy bare number, read as dollars)" in block


def test_a_future_observation_date_is_not_treated_as_a_completed_observation() -> None:
    model = {
        "currency": "USD",
        "skus": [
            {
                "name": "Tomorrow's price",
                "retail": 20,
                "unit_cost": {
                    "amount_cents": 800,
                    "kind": "sourced",
                    "source": SOURCE,
                    "observed_at": "2027-01-01",
                },
            }
        ],
        "payment_fee": {"percent": 2.9, "fixed_cents": 30, "kind": "estimate"},
    }
    view = derive_unit_economics(model, budget_cents=1000, today=TODAY)
    row = view["rows"][0]
    assert row["computable"] is False
    assert "in the future" in row["reason"]


# ── 3. the write boundary: a cost is refused unless it can be read honestly ────────────────────


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        (9.49, "bare"),
        ({"amount_cents": 9.49, "kind": "estimate"}, "INTEGER count of cents"),
        ({"amount_cents": -5, "kind": "estimate"}, "positive INTEGER"),
        ({"amount_cents": 949, "kind": "sourced", "observed_at": "2026-10-28"}, "source is required"),
        ({"amount_cents": 949, "kind": "sourced", "source": SOURCE}, "observed_at is required"),
        ({"amount_cents": 949, "kind": "market"}, "kind is"),
        ({"amount_cents": 949, "kind": "estimate", "ammount_cents": 1}, "unknown key"),
        ({"amount_cents": 949, "kind": "estimate", "basis": "per_order"}, "basis is"),
        ({"amount_cents": 949, "kind": "estimate", "currency": "EUR"}, "currency is"),
        ("9.49", "bare str"),
        ([949], "bare list"),
    ],
)
def test_an_ambiguous_or_unprovenanced_cost_is_refused_by_path(cost, expected) -> None:
    errors = cost_input_errors({"currency": "USD", "skus": [{"name": "X", "retail": 26,
                                                              "unit_cost": cost}]})
    assert errors, f"{cost!r} must be refused at the write boundary"
    assert "pricing_model.row[0].unit_cost" in errors[0]
    assert expected in errors[0]


@pytest.mark.parametrize(
    ("fee", "expected"),
    [
        ({"percent": 290}, "percent must be"),
        ({"percent": 2.9, "fixed_cents": -1}, "fixed_cents must be"),
        ({"note": "no rate recorded"}, "neither percent nor fixed_cents"),
        ({"percent": "2.9"}, "percent must be"),
        ({}, "neither percent nor fixed_cents"),
        ({"percent": 2.9, "kind": "sourced"}, "source is required"),
        ({"percent": 2.9, "kind": "sourced", "source": FEE_SOURCE}, "observed_at is required"),
    ],
)
def test_a_malformed_payment_fee_is_refused_by_path(fee, expected) -> None:
    errors = cost_input_errors({"currency": "USD", "payment_fee": fee})
    assert errors
    assert "pricing_model.payment_fee" in errors[0]
    assert expected in errors[0]


def test_the_live_profile_shape_and_a_null_cost_are_legal() -> None:
    """The unmodified live catalogue must not become a 422, and `unit_cost: null` clears a cost."""
    assert cost_input_errors(NO_COSTS) == []
    assert cost_input_errors(MODEL) == []
    assert cost_input_errors({"currency": "USD", "skus": [{"name": "X", "retail": 1,
                                                           "unit_cost": None}]}) == []
    # the seed's SaaS shape writes no cost at all — not a refusal either
    assert cost_input_errors({"plans": [{"name": "Starter", "price": 49}]}) == []


@pytest.mark.asyncio
async def test_put_config_stores_a_provenanced_cost_and_refuses_a_bare_number(
    async_db_session, api_client, auth_headers
) -> None:
    """The API half: a 422 naming the path for a bad cost, a 200 + DB row for a good one."""
    async_db_session.add(_company(pricing_model=dict(NO_COSTS)))
    await async_db_session.commit()

    bad = await api_client.put(
        "/api/v1/config",
        json={"pricing_model": {"currency": "USD", "skus": [
            {"name": "X", "retail": 26, "unit_cost": 9.49}]}},
        headers=auth_headers,
    )
    assert bad.status_code == 422, bad.text
    assert "pricing_model.row[0].unit_cost" in bad.text

    good = await api_client.put(
        "/api/v1/config", json={"pricing_model": MODEL}, headers=auth_headers
    )
    assert good.status_code == 200, good.text
    served = good.json()["pricing_model"]
    assert served["skus"][0]["unit_cost"]["amount_cents"] == 949

    async_db_session.expire_all()
    stored = (
        await async_db_session.execute(
            __import__("sqlalchemy").select(CompanyConfig).order_by(CompanyConfig.id).limit(1)
        )
    ).scalar_one()
    assert stored.pricing_model["skus"][0]["unit_cost"]["source"] == SOURCE

    # and a present-but-null cost CLEARS it without touching the rest of the row
    cleared = await api_client.put(
        "/api/v1/config",
        json={"pricing_model": {"currency": "USD", "skus": [
            {"sku": "CL-WSL-9", "name": "White Sage + Lavender 9 oz", "retail": 26,
             "unit_cost": None}]}},
        headers=auth_headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["pricing_model"]["skus"][0]["unit_cost"] is None


# ── 4. the finance agent actually receives the block (the capability gap, closed) ────────────


def test_the_finance_agent_prompt_carries_the_recorded_cost_block() -> None:
    agent = FinanceAgent()
    context = {
        "company": {"name": "Cinder & Lye", "pricing_model": MODEL, "owner_budget_usd": Decimal("125.00")},
        "kpis": {"mrr_usd": 0},
        "kpis_provenance": "configured",
    }
    block = cost_basis_from_context(context)
    assert block is not None
    assembled = agent.assemble_prompt(
        {"id": 1, "title": "Unit economics", "description": "cost the business"}, context,
        extra_context=block,
    )

    assert "RECORDED UNIT ECONOMICS" in assembled.volatile_suffix
    assert f"contribution: 1546 cents ($15.46) (59.5% of retail) {chr(0x2014)} DERIVED" in assembled.volatile_suffix
    assert f"break-even: 9 unit(s) {chr(0x2014)} DERIVED" in assembled.volatile_suffix
    assert "unit_cost: UNMEASURED" in assembled.volatile_suffix, "the uncosted SKU is named"
    assert "SAY COGS IS UNMEASURED" in assembled.stable_prefix.upper(), (
        "the role must tell the agent what to do when no block is present"
    )


def test_the_finance_RUN_wires_the_cost_block_into_the_prompt(monkeypatch) -> None:
    """The WIRING, not just the renderer: `FinanceAgent.run` must pass the block to the assembler.

    `run()` does not return the prompt, so the seam is observed where the agent hands its material
    over (`assemble_prompt`'s `extra_context`) while the REAL assembler still builds the prompt.
    Without this, the renderer could be perfect and the run could never see it — exactly the
    capability gap this wave is closing.
    """
    agent = FinanceAgent()
    captured: dict[str, str] = {}
    original = FinanceAgent.assemble_prompt

    def spy(self, task, context, *, memories=None, extra_context=None):
        captured["extra_context"] = extra_context or ""
        return original(self, task, context, memories=memories, extra_context=extra_context)

    monkeypatch.setattr(FinanceAgent, "assemble_prompt", spy)
    payload = agent.run(
        {"id": 1, "title": "Unit economics", "description": "cost each SKU"},
        {
            "company": {
                "name": "Cinder & Lye",
                "pricing_model": MODEL,
                "owner_budget_usd": Decimal("125.00"),
            },
            "kpis": {},
            "kpis_provenance": "unset",
        },
    )

    assert "RECORDED UNIT ECONOMICS" in captured["extra_context"], (
        "the finance run did not pass the recorded-cost block to the prompt"
    )
    assert f"break-even: 9 unit(s) {chr(0x2014)} DERIVED" in captured["extra_context"]
    assert "unit_cost: UNMEASURED" in captured["extra_context"]
    # the run still behaves: no revenue was measured, so no snapshot may be claimed
    assert payload["snapshot"] is None
    assert payload["snapshot_written"] is False


def test_a_run_without_a_pricing_model_carries_no_cost_block() -> None:
    """No catalogue, no block — the run degrades to its exact previous behaviour."""
    assert cost_basis_from_context({}) is None
    assert cost_basis_from_context({"company": {"name": "X"}}) is None
    assert render_cost_basis_block(None) is None
    assert render_cost_basis_block({}) is None


def test_the_budget_reaches_the_block_from_the_context_as_exact_cents() -> None:
    for value in (Decimal("125.00"), 125.0, "125.00"):
        block = cost_basis_from_context(
            {"company": {"pricing_model": MODEL, "owner_budget_usd": value}}
        )
        assert block is not None
        assert "WORKING BUDGET: 12500 cents ($125.00)" in block
    assert "WORKING BUDGET: UNMEASURED" in (cost_basis_from_context(
        {"company": {"pricing_model": MODEL}}) or "")


# ── 5. a public page must never print a cost (the leak the seam could introduce) ─────────────


def test_the_served_storefront_never_prints_a_cost_or_its_source() -> None:
    pages = site_service.render_pages(
        _company(pricing_model=MODEL), slug="cinder-lye", base_url="http://127.0.0.1:8000/"
    )
    joined = "\n".join(pages.values())

    assert "$26" in joined and "White Sage + Lavender 9 oz" in joined, "the selling price still renders"
    for leak in ("949", "9.49", "649", "6.49", "unit_cost", "amount_cents", SOURCE, "printonic"):
        assert leak not in joined, f"the public page leaked {leak!r}"


def test_the_downloadable_site_package_never_prints_a_cost_or_its_source() -> None:
    package = site_package_service.render_site_package(_company(pricing_model=MODEL))
    joined = "\n".join(package.values())
    for leak in ("949", "9.49", "649", "6.49", "unit_cost", "amount_cents", SOURCE, "printonic"):
        assert leak not in joined, f"the site package leaked {leak!r}"


def test_the_cost_reader_and_the_storefront_read_the_SAME_row_set() -> None:
    """One row set: the page you sell from and the agent that costs it cannot disagree."""
    assert CATALOGUE_LIST_KEYS == site_service._PRODUCT_LIST_KEYS


# ── 6. determinism: the block is a pure function of the recorded inputs and the date ─────────


def test_the_block_is_deterministic_for_the_same_inputs() -> None:
    assert _block() == _block()
    assert _block(today=TODAY + __import__("datetime").timedelta(days=1)).count("1 day(s) old") == 0


def test_the_block_names_every_line_as_measured_sourced_or_derived() -> None:
    block = _block()
    assert "DERIVED" in block
    assert "PROVENANCE VOCABULARY" in block
    assert "never use an UNMEASURED line as 0" in block
    # the block is instruction-bearing DATA: it must never fabricate a measured claim
    assert "MEASURED" not in block.replace("UNMEASURED", "")
