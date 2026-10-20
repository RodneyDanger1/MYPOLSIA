"""Economics routes — the honest macro-context read and the display-only FX conversion.

`GET /economics/context` (wave V9, seat b7) is the macro-context route; `GET /economics/convert`
(V29/L4) converts ONE amount for display and never writes an invoice total.

NOT wired into `app/main.py` by this seat: the parent wires the economics router at settle
(V9-D3, the pin/wiring ownership split — same as planner/invoices).  Until then this router is
reachable only through the local test app in `tests/unit/test_v9_economics.py`.

The response models are declared HERE, not in `app/schemas/`: the schemas package is frozen to
V8's edit list this wave (read-only for this seat) — the same ruling `app/api/v1/invoices.py`
records for its request models.  The shape mirrors the frozen service contract:

  * `generated_at` is THIS response's clock; each section carries its own `fetched_at`;
  * a section is `null` ONLY when its source could not be read — the sibling
    `<section>_reason` string is then `"<ExceptionClass>: <message>"`;
  * a source that answered with a null VALUE stays an object: `value: null` + `reason`
    (the World Bank's newest year is null today — never coerced to 0, never dropped);
  * sections keep their ORIGINAL fetched_at across cache hits — provenance is never restamped.

Read-only, no DB, no side effects: safe to poll.  This service is MACRO context only — it is
NOT a live-prices oracle (no public API publishes live service-market prices).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import refusal_guard, verify_api_key
from app.services import economics_service, fx_service

router = APIRouter(prefix="/economics", tags=["economics"], dependencies=[Depends(verify_api_key)])


class InflationSection(BaseModel):
    """Annual US inflation (%), World Bank — newest year; null value + reason when unreported."""

    value: float | None = None
    period: str | None = None
    source: str
    fetched_at: str
    reason: str | None = None


class FxSection(BaseModel):
    """ECB-reference FX rates, USD base (frankfurter.dev) — the contracted currency set."""

    usd_base: bool
    date: str
    rates: dict[str, float]
    source: str
    fetched_at: str
    reason: str | None = None


class CpiSection(BaseModel):
    """The monthly US CPI-U index (FRED CPIAUCSL) — latest observation."""

    index_value: float | None = None
    observation_date: str | None = None
    source: str
    fetched_at: str
    reason: str | None = None


class EconomicsContextResponse(BaseModel):
    """The frozen `/economics/context` wire shape (see the module docstring for the null rules)."""

    generated_at: str
    inflation: InflationSection | None = None
    inflation_reason: str | None = None
    fx: FxSection | None = None
    fx_reason: str | None = None
    cpi: CpiSection | None = None
    cpi_reason: str | None = None


@router.get("/context", response_model=EconomicsContextResponse)
async def get_economics_context() -> EconomicsContextResponse:
    """The current macro context; a failed source nulls only its own section, with its reason."""
    with refusal_guard():
        context = await economics_service.get_context()
    return EconomicsContextResponse.model_validate(context)


class FxConvertResponse(BaseModel):
    """One display-only conversion (V29/L4) — the wire spells the pair `from`/`to`.

    `source` is the exact Frankfurter URL the rate came from, or `"identity"` for a same-currency
    conversion (which makes no network call and carries `date: null`).  `cached: true` means the
    rate was served from the in-process 6 h cache and `fetched_at` is the ORIGINAL fetch stamp.
    """

    amount: float
    from_currency: str = Field(alias="from")
    to_currency: str = Field(alias="to")
    rate: float
    converted: float
    date: str | None
    source: str
    fetched_at: str
    cached: bool


@router.get("/convert", response_model=FxConvertResponse)
async def convert_currency(
    amount: float = Query(ge=0, le=1_000_000_000_000),
    from_currency: str = Query(alias="from", min_length=3, max_length=3),
    to_currency: str = Query(alias="to", min_length=3, max_length=3),
) -> FxConvertResponse:
    """Convert an amount for DISPLAY only (invoices never rewrite a stored total).

    The service is sync (the tool runners are sync), so it runs in a worker thread rather than
    blocking the event loop.  A code the rate source does not know is the caller's `E_VALIDATION`,
    never dressed up as an upstream fault.
    """
    with refusal_guard():
        result = await run_in_threadpool(fx_service.convert, amount, from_currency, to_currency)
    return FxConvertResponse.model_validate(result)
