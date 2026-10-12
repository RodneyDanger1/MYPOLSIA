"""GET/PUT /api/v1/config.

`PUT` is also the explicit invalidation point for the agent-context cache: it is the
one route that can change `company_config`, so it drops the cached context
(`company_service.invalidate_company_context()`) before replying — otherwise agents
would keep planning against the previous profile for up to the cache TTL.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_NOT_FOUND, ERROR_HINTS
from app.schemas.company import CompanyConfigResponse, CompanyConfigUpdate
from app.services.company_service import get_company, invalidate_company_context

router = APIRouter(prefix="/config", tags=["config"], dependencies=[Depends(verify_api_key)])


def _not_seeded() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": E_NOT_FOUND,
            "message": "company configuration has not been seeded yet",
            "how_to_fix": ERROR_HINTS[E_NOT_FOUND],
            "context": {"route": "/api/v1/config"},
        },
    )


@router.get("", response_model=CompanyConfigResponse)
async def read_config(db: AsyncSession = Depends(get_db)) -> CompanyConfigResponse:
    with refusal_guard():
        company = await get_company(db)
    if company is None:
        raise _not_seeded()
    return CompanyConfigResponse.model_validate(company)


@router.put("", response_model=CompanyConfigResponse)
async def update_config(
    payload: CompanyConfigUpdate, db: AsyncSession = Depends(get_db)
) -> CompanyConfigResponse:
    with refusal_guard():
        company = await get_company(db)
    if company is None:
        raise _not_seeded()
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    await db.flush()
    # CLAUDE.md: flush + refresh when you need the server-generated updated_at
    await db.refresh(company)
    # The profile just changed: any cached agent context is now wrong.
    invalidate_company_context()
    return CompanyConfigResponse.model_validate(company)
