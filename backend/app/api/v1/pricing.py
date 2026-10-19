"""GET /api/v1/pricing — the dated pricing-catalog snapshot read (V9 B2).

Market-rate CLAIMS with their sources, served from `backend/app/data/pricing_catalog.json` through
`pricing_service` (loaded once): `?q=` matches service names; an absent/empty `q` BROWSEs the
snapshot in file order.  Owner-key, the same `verify_api_key` dependency every sibling v1 router
carries.  A catalog that is missing or drifted off 50 rows is a named `E_CONFIG` refusal — never
a silent empty list (see `pricing_service.load_catalog`).

The payload is a DATED SNAPSHOT of the research file, NOT live pricing: every response also
carries the computed provenance (`catalog_as_of`, `staleness_days`, `stale` against
`CATALOG_STALENESS_THRESHOLD_DAYS`) so no client can mistake it for current market truth.

V20/L1 (cards L2-06 + L2-11):
  * the response is the declared `PricingCatalogResponse` (it was `dict[str, Any]`, i.e.
    `additionalProperties: true` in OpenAPI), so the row shape the Settings → Pricing panel reads
    is a published contract (`app/schemas/pricing.py` explains each field's typing choice);
  * `count` is the number of rows SERVED (page length — the field the panel already prints as
    "N rows served") and `total` is the FULL match count for `q`.  A caller can therefore no
    longer read a truncated page as the whole catalog, and `count` keeps its shipped meaning so
    no client changes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import refusal_guard, verify_api_key
from app.schemas.pricing import PricingCatalogResponse
from app.services import pricing_service

router = APIRouter(prefix="/pricing", tags=["pricing"], dependencies=[Depends(verify_api_key)])

#: The page ceiling.  It EQUALS the catalog's frozen size (`load_catalog` refuses any other row
#: count), which is what makes the `total` derivation below exact.
MAX_LIMIT = 50


@router.get(
    "",
    response_model=PricingCatalogResponse,
    # V20/L1: the payload is assembled from the dataset dicts; `exclude_unset` keeps an ABSENT
    # optional key absent (`price_text` on the 49 rows that carry no free-form claim, the F24
    # provenance `reason`).  A `null` we never wrote would be a wire change AND a fake
    # "unmeasured" value.
    response_model_exclude_unset=True,
)
async def pricing_catalog(
    q: str | None = Query(default=None, description="case-insensitive substring of the service name"),
    limit: int = Query(default=10, ge=1, le=MAX_LIMIT),
) -> PricingCatalogResponse:
    """`{query, count, total, items, catalog_as_of, staleness_days, stale}` — dated-snapshot rows."""
    with refusal_guard():
        rows = pricing_service.search(q, limit=limit)
        # The FULL match count: the catalog is EXACTLY 50 rows (load_catalog refuses otherwise),
        # so a limit=MAX_LIMIT search IS the complete match set — never a truncated count, and
        # never an invented number (this is the same read, not a second implementation).
        total = len(pricing_service.search(q, limit=MAX_LIMIT))
        provenance = pricing_service.catalog_provenance()
    return PricingCatalogResponse.model_validate(
        {"query": q, "count": len(rows), "total": total, "items": rows, **provenance}
    )
