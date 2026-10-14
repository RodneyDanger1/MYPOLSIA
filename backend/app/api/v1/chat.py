"""`/api/v1/chat` — the frozen chat contract (wave V8, seat C1).

FROZEN (plan §7 C1 / WAVE-PLAN "Frozen interface — CHAT"); the UI codes against this exactly:

    POST /api/v1/chat           body {"message": str}  -> 201 ChatResponse
    POST /api/v1/chat/confirm   body {"confirm_token": str} -> 200 {"task_id": int} | 409
    GET  /api/v1/chat?limit=50  -> 200 {"items": [{role, content, created_at, task_id}], "total"}

All three carry the owner's API key (`verify_api_key`, the same router-level dependency every
sibling v1 router uses).  The deterministic router, the confirm-token mint/verify, the task
hand-off path and the persistence all live in `app/services/chat_service.py`; this module is
only the wire form.  Refusals travel as the repo's named envelope via `refusal_guard`
(`E_POLICY_DENIED` money refusals are, by contract, a 201 body field — `refusal` — not an
HTTP error: the chat reply IS the UI surface for them).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.chat import (
    ChatHistoryResponse,
    ChatRequest,
    ChatResponse,
    ConfirmRequest,
    ConfirmResponse,
)
from app.services.chat_service import confirm_task, handle_message, list_history

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(verify_api_key)])

#: GET /chat page bounds (the frozen default is 50).
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


@router.post("", response_model=ChatResponse, status_code=status.HTTP_201_CREATED)
async def chat_message(payload: ChatRequest, db: AsyncSession = Depends(get_db)) -> ChatResponse:
    """One owner message in, one classified reply out (frozen 201 body)."""
    with refusal_guard():
        return await handle_message(db, payload.message)


@router.post("/confirm", response_model=ConfirmResponse)
async def chat_confirm(payload: ConfirmRequest, db: AsyncSession = Depends(get_db)) -> ConfirmResponse:
    """Consume the single-use confirm token and create the task exactly once (409 otherwise)."""
    with refusal_guard():
        task_id = await confirm_task(db, payload.confirm_token)
    return ConfirmResponse(task_id=task_id)


@router.get("", response_model=ChatHistoryResponse)
async def chat_history(
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> ChatHistoryResponse:
    """The persisted chat transcript (reading order) + the full stored-row count."""
    with refusal_guard():
        return await list_history(db, limit)
