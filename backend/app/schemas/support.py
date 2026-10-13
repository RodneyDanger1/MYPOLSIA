"""Wire shapes for /api/v1/support/inbox (R17 — the owner's inbox ingest surface)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class SupportInboxItemCreate(BaseModel):
    """Submit one customer message.

    NO field constraints here on purpose: `support_inbox_service.submit_item` is the single
    validation authority (blank fields, identifier lengths, timezone-less `received_at` are
    all named `E_VALIDATION` refusals there) — duplicating a subset in the schema would let
    the two rules drift.
    """

    provider_message_id: str
    from_address: str
    subject: str
    body: str
    received_at: datetime | None = None


class SupportInboxItemResponse(ORMModel):
    id: int
    provider_message_id: str
    from_address: str
    subject: str
    body: str
    status: str
    received_at: datetime | None = None
    task_id: int | None = None
    created_at: datetime | None = None
