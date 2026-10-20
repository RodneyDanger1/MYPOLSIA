"""The owner's prospect CRM (V27/L5): `/api/v1/prospects` CRUD.

    GET    /api/v1/prospects                    -> 200 {"items": [ProspectResponse], "total": n}
    POST   /api/v1/prospects                    -> 201  ProspectResponse
    GET    /api/v1/prospects/{prospect_id}      -> 200  ProspectResponse
    PATCH  /api/v1/prospects/{prospect_id}      -> 200  ProspectResponse  (partial: provided keys only)
    DELETE /api/v1/prospects/{prospect_id}      -> 204  (no body)

This file adds NO business rules of its own: `app/services/prospect_service.py` owns the email
normalisation, the duplicate/blank/unusable refusals, the exact status filter, the search
predicate and the newest-first order.  The routes are the wire shape over it.

The read answers a WRAPPED body (`{items, total}`) because this is a NEW route — the existing
bare-array reads (`/outreach/prospects`) cannot change shape without breaking their clients,
and a page whose length equals its `limit` must be distinguishable from the whole table.  The
same true count also rides `X-Polsia-Total`, the header every paged owner read already carries
(and `main.py` already exposes cross-origin).

Commit discipline: each write builds its response and commits explicitly in the route body (the
`schedules.py` precedent), so the write lands even under a plain test-client session override
instead of riding `get_db`'s teardown.

AUTH: the router-level `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER
from app.schemas.prospect import (
    ProspectCreate,
    ProspectListResponse,
    ProspectResponse,
    ProspectUpdate,
)
from app.services import prospect_service as prospects

router = APIRouter(
    prefix="/prospects", tags=["prospects"], dependencies=[Depends(verify_api_key)]
)


@router.get("", response_model=ProspectListResponse)
async def list_prospect_rows(
    response: Response,
    search: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, alias="status", max_length=50),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> ProspectListResponse:
    """One newest-first page + the full match count for `search`/`status`.

    `search` matches email, first name, last name and company (contains, case-insensitive);
    `status` is EXACT equality on the stored word — the column is open text, so no enum is
    imposed here (the service docstring states why).  No rows is an honest `{items: [], total: 0}`.
    """
    with refusal_guard():
        rows, total = await prospects.list_prospects(
            db, search=search, status=status_filter, limit=limit
        )
        body = ProspectListResponse(
            items=[ProspectResponse.model_validate(row) for row in rows], total=total
        )
    response.headers[TOTAL_HEADER] = str(total)
    return body


@router.post("", response_model=ProspectResponse, status_code=status.HTTP_201_CREATED)
async def create_prospect_row(
    payload: ProspectCreate, db: AsyncSession = Depends(get_db)
) -> ProspectResponse:
    """Create one prospect; the address is trimmed + lowercased before the unique insert."""
    with refusal_guard():
        row = await prospects.create_prospect(
            db,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            company=payload.company,
            title=payload.title,
            source=payload.source,
            status=payload.status,
        )
        body = ProspectResponse.model_validate(row)
        await db.commit()
    return body


@router.get("/{prospect_id}", response_model=ProspectResponse)
async def get_prospect_row(
    prospect_id: int, db: AsyncSession = Depends(get_db)
) -> ProspectResponse:
    """One stored prospect; an unknown id is `E_NOT_FOUND` 404 (never an empty 200)."""
    with refusal_guard():
        row = await prospects.get_prospect(db, prospect_id)
        body = ProspectResponse.model_validate(row)
    return body


@router.patch("/{prospect_id}", response_model=ProspectResponse)
async def update_prospect_row(
    prospect_id: int, payload: ProspectUpdate, db: AsyncSession = Depends(get_db)
) -> ProspectResponse:
    """Apply only the provided fields; `{"status": "contacted"}` is the inline status edit."""
    with refusal_guard():
        row = await prospects.update_prospect(
            db, prospect_id, payload.model_dump(exclude_unset=True)
        )
        body = ProspectResponse.model_validate(row)
        await db.commit()
    return body


@router.delete("/{prospect_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prospect_row(prospect_id: int, db: AsyncSession = Depends(get_db)) -> Response:
    """Remove the row: 204 on success, `E_NOT_FOUND` 404 for an id that is not there."""
    with refusal_guard():
        await prospects.delete_prospect(db, prospect_id)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
