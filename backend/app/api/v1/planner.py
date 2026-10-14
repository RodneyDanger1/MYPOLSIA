"""`/api/v1/planner` — the frozen planner contract (wave V9, seat B1).

    POST /api/v1/planner/propose  {}                        -> 201 ProposeResponse (3-5 candidates)
    POST /api/v1/planner/confirm  {"candidate_id": str}     -> 200 {"confirmed": true}
    GET  /api/v1/planner/status                             -> 200 {"confirmed": {...}|null,
                                                                "last_proposed_at": str|null}

NOT wired into `app/main.py` (V9-D3: the parent wires planner + invoices routers at settle and
moves the route pins in the same patch) — tests include this router directly.  All three routes
carry the owner's API key (`verify_api_key`, the same router-level dependency every sibling v1
router uses); refusals travel as the repo's named envelope via `refusal_guard` (422
`E_VALIDATION` for the who-pays/count gates, 404 `E_NOT_FOUND` for an unknown candidate id or
a missing company row).  The service behind these routes writes at most activity rows +
`company_config.confirmed_idea`: no task, no run, no send.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.planner import (
    ConfirmRequest,
    ConfirmResponse,
    ProposeResponse,
    StatusResponse,
)
from app.services import planner_service

router = APIRouter(prefix="/planner", tags=["planner"], dependencies=[Depends(verify_api_key)])


@router.post("/propose", response_model=ProposeResponse, status_code=status.HTTP_201_CREATED)
async def propose(db: AsyncSession = Depends(get_db)) -> ProposeResponse:
    """Fetch -> propose -> gate -> rubric; the frozen 201 body; nothing spends."""
    with refusal_guard():
        result = await planner_service.propose(db)
    return ProposeResponse.model_validate(result)


@router.post("/confirm", response_model=ConfirmResponse)
async def confirm(payload: ConfirmRequest, db: AsyncSession = Depends(get_db)) -> ConfirmResponse:
    """Write the chosen candidate into `company_config.confirmed_idea` (and nothing else)."""
    with refusal_guard():
        confirmed = await planner_service.confirm(db, payload.candidate_id)
    return ConfirmResponse(confirmed=confirmed)


@router.get("/status", response_model=StatusResponse)
async def planner_status(db: AsyncSession = Depends(get_db)) -> StatusResponse:
    """The confirmed idea (or null) + when the last proposal ran (or null)."""
    with refusal_guard():
        result = await planner_service.status(db)
    return StatusResponse.model_validate(result)
