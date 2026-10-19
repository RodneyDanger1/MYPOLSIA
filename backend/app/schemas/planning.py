"""Wire model for `GET /api/v1/finance/planning` (V20/L1 — closes V19/L2-06 for that route).

WHY: the route declared `-> dict[str, Any]`; the shape (burn line items, capacity, margin
template, the 1:1 `reasons` map, guardrails, tax notes and the dated-catalog provenance) lived
only in `build_planning_view`'s docstring (v14 S3-05 "THE REASON CONTRACT").  The model below is
that contract, in the SAME key order the dict is built, so the served bytes do not move.

THE REASON CONTRACT, encoded:
  * every input the view could not measure is `null`, and its cause is in `reasons` keyed 1:1 to
    the field it explains (`capacity.<field>`, `margin_template.<field>`, `burn_line_items[.<cat>]`);
  * a burn row with no measured amount is still ANSWERED — `monthly_usd: null` + an inline
    `reason` (hence `reason` is optional and OMITTED when absent: `model_validate` on the service
    dict leaves it unset, and the route serializes with `exclude_unset`, so a null we never wrote
    can never appear on the wire — that would be a wire change and a fake "unmeasured").
  * `catalog_as_of` stays a STRING on purpose: `pricing_service.catalog_provenance` reports an
    unparseable stored date verbatim plus a `reason` (F24) instead of erasing it; typing it as
    `date` would turn that documented state into a validation failure.
"""
from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "BurnLineItemResponse",
    "FinancePlanningResponse",
    "PlanningCapacityResponse",
    "PlanningMarginTemplateResponse",
]


class BurnLineItemResponse(BaseModel):
    """One measured expense category for the current month (or one unmeasured row + reason)."""

    label: str
    #: USD; `None` = the category recorded expenses with no measured amount (never 0).
    monthly_usd: float | None
    source: str | None
    #: Present ONLY on an unmeasured row (see the module docstring).
    reason: str | None = None


class PlanningCapacityResponse(BaseModel):
    """Clients-at-capacity math; every input is null when it was not measured."""

    hours_per_client_lo: float | None
    hours_per_client_hi: float | None
    max_clients: int | None
    mrr_at_capacity: float | None
    break_even_clients: int | None
    #: The catalog row the hours came from — named so an hours figure is always attributable.
    hours_per_client_source: str | None


class PlanningMarginTemplateResponse(BaseModel):
    """Per-client margin at capacity; `margin` is null unless every input is measured."""

    price: float | None
    ai_cost: float | None
    human_hours: float | None
    human_rate: float | None
    margin: float | None


class FinancePlanningResponse(BaseModel):
    """`GET /finance/planning` — the frozen planning object (plan §9 lines 451-456)."""

    burn_line_items: list[BurnLineItemResponse]
    capacity: PlanningCapacityResponse
    margin_template: PlanningMarginTemplateResponse
    #: field path -> the cause it was not measured.  Written for UNMEASURED fields only.
    reasons: dict[str, str]
    pricing_guardrails: list[str]
    sales_tax_note: str
    tax_disclaimer: str
    # --- dated-snapshot provenance (`pricing_service.catalog_provenance`) -------------------
    catalog_as_of: str | None
    staleness_days: int | None
    stale: bool | None
    #: Present only when the catalog's newest date is unparseable (F24).
    reason: str | None = None
