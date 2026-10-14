"""GET /api/v1/pricing — the dated pricing-catalog snapshot read (V9 B2).

Market-rate CLAIMS with their sources, served from `backend/app/data/pricing_catalog.json` through
`pricing_service` (loaded once): `?q=` matches service names; an absent/empty `q` BROWSEs the
snapshot in file order.  Owner-key, the same `verify_api_key` dependency every sibling v1 router
carries.  A catalog that is missing or drifted off 50 rows is a named `E_CONFIG` refusal — never
a silent empty list (see `pricing_service.load_catalog`).

The payload is a DATED SNAPSHOT of the research file, NOT live pricing: every response also
carries the computed provenance (`catalog_as_of`, `staleness_days`, `stale` against
`CATALOG_STALENESS_THRESHOLD_DAYS`) so no client can mistake it for current market truth.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import refusal_guard, verify_api_key
from app.services import pricing_service

router = APIRouter(prefix="/pricing", tags=["pricing"], dependencies=[Depends(verify_api_key)])


@router.get("")
async def pricing_catalog(
    q: str | None = Query(default=None, description="case-insensitive substring of the service name"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """`{query, count, items, catalog_as_of, staleness_days, stale}` — dated-snapshot rows."""
    with refusal_guard():
        rows = pricing_service.search(q, limit=limit)
        provenance = pricing_service.catalog_provenance()
    return {"query": q, "count": len(rows), "items": rows, **provenance}
