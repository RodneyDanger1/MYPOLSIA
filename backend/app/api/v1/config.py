"""GET/PUT /api/v1/config, plus the DeepSeek key lifecycle under /config/deepseek-key.

`PUT` is also the explicit invalidation point for the agent-context cache: it is the
one route that can change `company_config`, so it drops the cached context
(`company_service.invalidate_company_context()`) before replying — otherwise agents
would keep planning against the previous profile for up to the cache TTL.

The DeepSeek key routes (`GET`/`PUT`/`DELETE /deepseek-key`, `POST /deepseek-key/verify`)
deal in masked status only: the raw key goes INTO `PUT` and never back out — no response
body, no log line, no error detail (verify scrubs it) ever carries it.  Refusals follow
the app's named-refusal convention (`refusal_guard` + `E_VALIDATION` for a blank key,
`E_STORAGE` when the store cannot be written).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_NOT_FOUND, ERROR_HINTS
from app.schemas.company import CompanyConfigResponse, CompanyConfigUpdate
from app.schemas.secret import (
    DeepSeekKeyStatus,
    DeepSeekKeyUpdate,
    DeepSeekVerifyRequest,
    DeepSeekVerifyResult,
)
from app.services.company_service import get_company, invalidate_company_context
from app.services.secret_store import (
    clear_deepseek_api_key,
    deepseek_key_status,
    set_deepseek_api_key,
    verify_deepseek_key,
)

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


# ── DeepSeek key lifecycle ──────────────────────────────────────────────────────────────────
# The raw key is accepted by PUT and never returned.  Everything below answers with the masked
# status (`configured` / `source` / `masked`) or the result of a REAL probe.


@router.get("/deepseek-key", response_model=DeepSeekKeyStatus)
async def read_deepseek_key() -> DeepSeekKeyStatus:
    with refusal_guard():
        status_dict = deepseek_key_status()
    return DeepSeekKeyStatus(**status_dict)


@router.put("/deepseek-key", response_model=DeepSeekKeyStatus)
async def update_deepseek_key(payload: DeepSeekKeyUpdate) -> DeepSeekKeyStatus:
    """Store the owner's key durably.  Blank is refused with `E_VALIDATION`."""
    with refusal_guard():
        set_deepseek_api_key(payload.api_key)
        status_dict = deepseek_key_status()
    return DeepSeekKeyStatus(**status_dict)


@router.delete("/deepseek-key", response_model=DeepSeekKeyStatus)
async def delete_deepseek_key() -> DeepSeekKeyStatus:
    """Remove the stored key.  An environment key may still configure the runtime."""
    with refusal_guard():
        clear_deepseek_api_key()
        status_dict = deepseek_key_status()
    return DeepSeekKeyStatus(**status_dict)


@router.post("/deepseek-key/verify", response_model=DeepSeekVerifyResult)
async def verify_deepseek_key_route(
    payload: DeepSeekVerifyRequest | None = None,
) -> DeepSeekVerifyResult:
    """Probe a candidate key (verify-before-save) or the configured one, for real.

    A resolved failure is `ok: false` with the genuine reason, not an HTTP refusal: the
    caller asked "does this key work?" and the answer is data either way.
    """
    with refusal_guard():
        result = await verify_deepseek_key(payload.api_key if payload else None)
    return DeepSeekVerifyResult(**result)
