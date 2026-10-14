"""POST/GET /api/v1/invoices/* — invoices v1 routes (wave V9, seat b4).

NOT wired into `app/main.py` by this seat: the parent wires the invoices router at settle
(V9-D3, the pin/wiring ownership split).  Until then this router is reachable only through the
local test app in `tests/unit/test_v9_invoices.py` — which is exactly what that local app pins.

The request models are declared HERE, not in `app/schemas/`: the schemas package is frozen to
V8's edit list this wave (read-only for this seat), so the additive wire fields live in the
module that owns the behaviour — the same ruling `app/api/v1/approvals.py` records for
`ApprovalIssueRequestWithTask`.  The RESPONSE model is the frozen `app.schemas.invoice.InvoiceResponse`.

Money on the wire: floats in, converted at this boundary through the finance module's `money()`
(`Decimal(str(x))` — the exact-decimal rule of A-T18) before the service sees them; `null` stays
`null` (unmeasured — never coerced to 0).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.invoice import InvoiceResponse
from app.services import invoice_service
from app.services.finance_service import money

router = APIRouter(prefix="/invoices", tags=["invoices"], dependencies=[Depends(verify_api_key)])


class InvoiceCreateRequest(BaseModel):
    """The wire body of `POST /invoices` — everything optional but the meaning.

    The service assigns `number`, `status` (draft) and the derived `due_date`; everything else
    is stored exactly as measured.  Absent money stays absent (NULL on the wire).
    """

    bill_to: dict[str, Any] | None = None
    line_items: list[dict[str, Any]] | None = None
    issue_date: date | None = None
    due_date: date | None = None
    net_days: int | None = Field(default=None, ge=0, le=365)
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    payment_link: str | None = None


class InvoiceSendRequest(BaseModel):
    """`POST /invoices/{id}/send` — the optional cover note rendered above the invoice."""

    cover_note: str | None = None


class InvoiceVoidRequest(BaseModel):
    """`POST /invoices/{id}/void` — the optional reason recorded in the activity feed."""

    reason: str | None = None


@router.post("", response_model=InvoiceResponse, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    request: InvoiceCreateRequest, db: AsyncSession = Depends(get_db)
) -> InvoiceResponse:
    with refusal_guard():
        invoice = await invoice_service.create_invoice(
            db,
            bill_to=request.bill_to,
            line_items=request.line_items,
            issue_date=request.issue_date,
            due_date=request.due_date,
            net_days=request.net_days,
            currency=request.currency,
            subtotal=money(request.subtotal),
            tax=money(request.tax),
            total=money(request.total),
            payment_link=request.payment_link,
        )
    return InvoiceResponse.model_validate(invoice)


@router.get("", response_model=list[InvoiceResponse])
async def list_invoices(
    status_value: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceResponse]:
    with refusal_guard():
        rows = await invoice_service.list_invoices(db, status=status_value, limit=limit)
    return [InvoiceResponse.model_validate(row) for row in rows]


@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(invoice_id: int, db: AsyncSession = Depends(get_db)) -> InvoiceResponse:
    with refusal_guard():
        invoice = await invoice_service.get_invoice(db, invoice_id)
    return InvoiceResponse.model_validate(invoice)


@router.post("/{invoice_id}/send", response_model=InvoiceResponse)
async def send_invoice(
    invoice_id: int,
    request: InvoiceSendRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> InvoiceResponse:
    """Render + deliver a DRAFT invoice through the firewalled email path, then flip it to sent.

    Every refusal is the SERVICE's named `PolsiaError` (E_CONFIG / E_SANDBOX_BLOCKED /
    E_RATE_LIMITED / E_INTEGRATION) translated to its HTTP form by `refusal_guard` — the
    invoice stays a draft on every one of them.
    """
    with refusal_guard():
        invoice = await invoice_service.send_invoice(
            db, invoice_id, cover_note=request.cover_note if request else None
        )
    return InvoiceResponse.model_validate(invoice)


@router.post("/{invoice_id}/void", response_model=InvoiceResponse)
async def void_invoice(
    invoice_id: int,
    request: InvoiceVoidRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> InvoiceResponse:
    with refusal_guard():
        invoice = await invoice_service.void_invoice(
            db, invoice_id, reason=request.reason if request else None
        )
    return InvoiceResponse.model_validate(invoice)
