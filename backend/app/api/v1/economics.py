"""GET /api/v1/economics/context — the honest macro-context route (wave V9, seat b7).

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

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import refusal_guard, verify_api_key
from app.services import economics_service

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
