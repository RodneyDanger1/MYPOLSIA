"""WAVE 5 — the FINAL one-PUT body for the sourced COGS, proven end to end in the TEST environment.

`raw-w5/w5-cogs-one-put-body.json` is the body the Lead can apply with ONE `PUT /api/v1/config`
(owner decision — the live profile is not written by this lane).  This module is the storage and
rendering half of that dry run, on a real request through the real route:

  1. the body is ACCEPTED by the live write schema (`CompanyConfigUpdate`) — with the negative
     control next to it, so the acceptance is not vacuous;
  2. `PUT /api/v1/config` stores it (200, row carries `amount_cents`/`source`/`observed_at`);
  3. reading the stored profile back through `company_service.get_full_context` and the finance
     agent's own `cost_basis_from_context` renders the RECORDED UNIT ECONOMICS block with the
     INDEPENDENTLY recomputed arithmetic — 9 oz: fee 105c, contribution 1546c, break-even 9 on the
     recorded $125 = 12500c budget; 4 oz: 85c / 1166c / 11;
  4. the DUO (no published landed cost) stays UNMEASURED — `None`, never 0, never a break-even;
  5. applying the body twice is idempotent (a retried PUT stores the same row, not a second cost).

The live profile is deliberately NOT written by this lane: the numbers are the owner's sourced
inputs and the wave-5 brief holds the write for the Lead's go-ahead.  This test proves the body is
correct NOW, so the only remaining step after the go-ahead is the single PUT itself.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents.finance.agent import FinanceAgent
from app.models.company import CompanyConfig
from app.schemas.company import CompanyConfigUpdate
from app.services.company_service import get_full_context, invalidate_company_context
from app.services.cost_basis_service import cost_basis_from_context, derive_unit_economics

PRINTONIC = "https://printonic.com/blog/candle-dropshipping"
STRIPE = "https://stripe.com/pricing"

#: THE body (dict-identical to raw-w5/w5-cogs-one-put-body.json, asserted by the lane's dry run —
#: generated from the live row, so applying it changes ONLY the costs; names, prices, descriptions
#: and availability are the live ones, and the public catalogue cannot print a cost (wave-4 leak
#: tests)).
BODY: dict = {
    "pricing_model": {
        "skus": [
            {
                "sku": "CL-WSL-9",
                "name": "White Sage + Lavender 9 oz",
                "retail": 26,
                "description": (
                    "Soy candle in a 9 oz straight-sided glass with a cotton wick. White sage + "
                    "lavender. Made to order; printed and shipped from Chatsworth, CA."
                ),
                "availability": "made_to_order",
                "unit_cost": {
                    "amount_cents": 949,
                    "kind": "sourced",
                    "source": PRINTONIC,
                    "observed_at": "2026-10-28",
                    "note": "9 oz POD base price, before shipping (Printonic US rate card, wave-4 read)",
                },
            },
            {
                "sku": "CL-WSL-4",
                "name": "White Sage + Lavender 4 oz",
                "retail": 19,
                "description": (
                    "The same scent in a 4 oz amber glass with a cork lid. Same soy wax, same "
                    "label, smaller pour."
                ),
                "availability": "made_to_order",
                "unit_cost": {
                    "amount_cents": 649,
                    "kind": "sourced",
                    "source": PRINTONIC,
                    "observed_at": "2026-10-28",
                    "note": "4 oz cork-lid POD base price, before shipping",
                },
            },
            {
                "sku": "CL-DUO",
                "name": "Two-Candle Duo - 9 oz + 4 oz",
                "retail": 42,
                "description": (
                    "One 9 oz glass and one 4 oz amber, boxed together. Cheaper than buying the "
                    "two apart."
                ),
                "availability": "made_to_order",
            },
        ],
        "currency": "USD",
        "payment_fee": {
            "percent": 2.9,
            "fixed_cents": 30,
            "kind": "sourced",
            "source": STRIPE,
            "observed_at": "2026-10-28",
            "note": "Stripe standard US card rate (bridge S15)",
        },
    }
}

#: The live catalogue as it stands today: retail only, no cost (the state the PUT changes).
LIVE_NO_COSTS: dict = {
    "currency": "USD",
    "skus": [
        {
            "sku": row["sku"],
            "name": row["name"],
            "retail": row["retail"],
            "description": row["description"],
            "availability": row["availability"],
        }
        for row in BODY["pricing_model"]["skus"]
    ],
}


def _company(**overrides) -> CompanyConfig:
    values: dict = {
        "name": "Cinder & Lye",
        "value_prop": "Light, then lather.",
        "mission": "Made-to-order soy candles, cotton wick.",
        "description": "Printed and shipped from Chatsworth, CA.",
        "notification_email": "hello@cinderlye.example",
        "timezone": "UTC",
        "daily_cycle_hour": 6,
        "owner_budget_usd": Decimal("125.00"),
        "pricing_model": dict(LIVE_NO_COSTS),
        # The live profile's KPI map (owner-entered zeros): provenance "configured", which is what
        # the prompt label is chosen from — an EMPTY map is "unset" and renders differently.
        "kpis": {"mrr_usd": 0, "active_customers": 0},
    }
    values.update(overrides)
    return CompanyConfig(**values)


# ── 1. the write boundary accepts the body (and still refuses the ambiguous shape) ──────────────
def test_the_final_body_is_accepted_by_the_write_schema() -> None:
    parsed = CompanyConfigUpdate(**BODY)
    assert parsed.pricing_model is not None
    costs = {row["sku"]: row.get("unit_cost") for row in parsed.pricing_model["skus"]}
    assert costs["CL-WSL-9"]["amount_cents"] == 949
    assert costs["CL-WSL-4"]["amount_cents"] == 649
    assert costs["CL-DUO"] is None  # absent, never 0


def test_the_boundary_still_refuses_a_bare_float_next_to_the_body() -> None:
    """The negative control: acceptance of the body must not mean the rule is off."""
    with pytest.raises(ValueError) as excinfo:
        CompanyConfigUpdate(
            pricing_model={
                "currency": "USD",
                "skus": [{"sku": "CL-WSL-9", "name": "x", "retail": 26, "unit_cost": 9.49}],
            }
        )
    assert "pricing_model.row[0].unit_cost" in str(excinfo.value)


# ── 2. storage: the ONE PUT, through the real route ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_one_put_stores_the_costs_on_the_profile(
    async_db_session, api_client, auth_headers
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()

    put = await api_client.put("/api/v1/config", json=BODY, headers=auth_headers)
    assert put.status_code == 200, put.text
    served = put.json()["pricing_model"]
    by_sku = {row["sku"]: row for row in served["skus"]}
    assert by_sku["CL-WSL-9"]["unit_cost"]["amount_cents"] == 949
    assert by_sku["CL-WSL-9"]["unit_cost"]["source"] == PRINTONIC
    assert by_sku["CL-WSL-4"]["unit_cost"]["amount_cents"] == 649
    assert by_sku["CL-DUO"].get("unit_cost") is None
    assert served["payment_fee"]["fixed_cents"] == 30

    async_db_session.expire_all()
    stored = (
        await async_db_session.execute(select(CompanyConfig).order_by(CompanyConfig.id).limit(1))
    ).scalar_one()
    assert stored.pricing_model["skus"][0]["unit_cost"]["observed_at"] == "2026-10-28"


@pytest.mark.asyncio
async def test_applying_the_body_twice_is_idempotent(
    async_db_session, api_client, auth_headers
) -> None:
    """A retried PUT must re-store the same model, not append a second cost to a SKU row."""
    async_db_session.add(_company())
    await async_db_session.commit()
    first = await api_client.put("/api/v1/config", json=BODY, headers=auth_headers)
    second = await api_client.put("/api/v1/config", json=BODY, headers=auth_headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["pricing_model"] == second.json()["pricing_model"]


# ── 3. the finance agent's block, from the STORED profile ───────────────────────────────────────
@pytest.mark.asyncio
async def test_the_stored_profile_renders_the_expected_finance_block(
    async_db_session, api_client, auth_headers
) -> None:
    async_db_session.add(_company())
    await async_db_session.commit()
    put = await api_client.put("/api/v1/config", json=BODY, headers=auth_headers)
    assert put.status_code == 200, put.text

    invalidate_company_context()
    context = await get_full_context(async_db_session, use_cache=False)
    block = cost_basis_from_context(context)
    assert block is not None, "the finance agent would be told NOTHING about the recorded costs"

    # The independently recomputed arithmetic (fee = round(0.029 x 2600) + 30 = 105; ...):
    view = derive_unit_economics(
        context["company"]["pricing_model"],
        budget_cents=int(Decimal("125.00") * 100),
    )
    nine = next(row for row in view["rows"] if row["sku"] == "CL-WSL-9")
    four = next(row for row in view["rows"] if row["sku"] == "CL-WSL-4")
    duo = next(row for row in view["rows"] if row["sku"] == "CL-DUO")
    assert (nine["fee_cents"], nine["contribution_cents"], nine["break_even_units"]) == (105, 1546, 9)
    assert (four["fee_cents"], four["contribution_cents"], four["break_even_units"]) == (85, 1166, 11)
    assert duo["computable"] is False
    assert duo["unit_cost_cents"] is None and duo["contribution_cents"] is None
    assert duo["break_even_units"] is None

    for expected in (
        "WORKING BUDGET: 12500 cents ($125.00)",
        "PAYMENT FEE: 2.9% + 30 cents",
        "contribution: 1546 cents ($15.46) (59.5% of retail) — DERIVED",
        "contribution: 1166 cents ($11.66) (61.4% of retail) — DERIVED",
        "break-even: 9 unit(s)",
        "break-even: 11 unit(s)",
        "NOT COMPUTABLE — no unit_cost is recorded for this row",
        f"recorded source {PRINTONIC}",
        f"recorded source {STRIPE}",
    ):
        assert expected in block, f"the block lost its line: {expected!r}"

    # The DUO section must not carry a fabricated contribution or a zero.
    duo_section = block.split("SKU 3 —", 1)[1].split("PROVENANCE VOCABULARY", 1)[0]
    assert "contribution: 0" not in duo_section
    assert "NOT COMPUTABLE" in duo_section

    assembled = FinanceAgent().assemble_prompt(
        {"id": 1, "title": "Unit economics", "description": "cost the business"},
        context,
        extra_context=block,
    )
    assert block.splitlines()[0] in assembled.volatile_suffix
    assert "KPIs (CONFIGURED" in assembled.stable_prefix
    assert assembled.taint is None or assembled.taint.tainted is False
