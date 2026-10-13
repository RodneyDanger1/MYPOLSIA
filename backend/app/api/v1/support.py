"""POST/GET /api/v1/support/inbox — the customer-support inbox ingest (R17).

WHY this file exists (RD12 receipt §7 REQUESTS 1+2): the customer_support consumer shipped in
RD12 (a task's `metadata.inbox_item` reaches the agent's prompt, quarantined), but NOTHING
produced an inbox item — the 3h `email-inbox-sweep` ran title-only.  This route is the
operator's submit/read surface; the sweep (`_email_inbox_sweep`) is the attach; together they
close the producer chain end to end.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`memory.py:17`, `tasks.py:22`).  These are CUSTOMER EMAILS: they may be read by the owner
(the API key holder) and by nobody else — there is no public variant of this route.
Pagination mirrors the memory list (limit) + tasks list (offset).

Idempotency: `POST` is keyed on the provider's own message id.  A repeated id answers 201
again with the STORED row unchanged — "the resource exists after this call" is the honest
statement for an at-least-once ingest; neither a duplicate row nor an overwritten customer
message is ever the outcome (see `support_inbox_service.submit_item`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.support import SupportInboxItemCreate, SupportInboxItemResponse
from app.services.support_inbox_service import list_items, submit_item

router = APIRouter(prefix="/support", tags=["support"], dependencies=[Depends(verify_api_key)])


@router.post("/inbox", response_model=SupportInboxItemResponse, status_code=status.HTTP_201_CREATED)
async def submit_inbox_item(
    payload: SupportInboxItemCreate, db: AsyncSession = Depends(get_db)
) -> SupportInboxItemResponse:
    """Store one customer message (validated by the service; idempotent on provider id)."""
    with refusal_guard():
        item = await submit_item(
            db,
            provider_message_id=payload.provider_message_id,
            from_address=payload.from_address,
            subject=payload.subject,
            body=payload.body,
            received_at=payload.received_at,
        )
    return SupportInboxItemResponse.model_validate(item)


@router.get("/inbox", response_model=list[SupportInboxItemResponse])
async def read_inbox(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[SupportInboxItemResponse]:
    """The stored items, newest first. `?status=pending|dispatched` filters; unknown → 422."""
    with refusal_guard():
        items = await list_items(db, status=status_filter, limit=limit, offset=offset)
    return [SupportInboxItemResponse.model_validate(item) for item in items]
