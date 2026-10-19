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
`null` (unmeasured — never coerced to 0).  NON-FINITE floats (`NaN` / `±Infinity`) are refused at
the wire (`allow_inf_nan=False`, S4-F1 wave v14) — they used to be persisted, 500 the create
response, and then poison every `GET /invoices` read.  V19/L2 extends the same rule to the
FREE-FORM JSON columns (`bill_to`/`line_items`): a NaN/Infinity inside them is refused 422 on
write, and a row that already carries one is refused `E_DB` on read instead of being served as
`null` ("unmeasured").
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.core.errors import E_DB, PolsiaError
from app.models.invoice import Invoice, InvoiceStatus
from app.schemas.company import non_finite_paths
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
    # S4-F1: `allow_inf_nan=False` — pydantic accepts NaN/Infinity for `float` by default, and
    # these three were the money fields without the finiteness belt.  A non-finite value is
    # refused as a named validation error BEFORE the service sees it; the row is never written.
    subtotal: float | None = Field(default=None, allow_inf_nan=False)
    tax: float | None = Field(default=None, allow_inf_nan=False)
    total: float | None = Field(default=None, allow_inf_nan=False)
    payment_link: str | None = None

    @model_validator(mode="after")
    def _no_non_finite_numbers_in_free_form_json(self) -> InvoiceCreateRequest:
        """`bill_to`/`line_items` are FREE-FORM JSON — the S4-F1 rule must be applied by hand.

        V19/L2 measured at 257e20c: `POST /invoices {"bill_to": {"x": NaN}}` answered **201**
        and stored the non-standard token; the read then served `{"x": null}` — pydantic
        serializes non-finite floats as null, so corruption came back as "not measured", the
        exact laundering L3-02b closed for `company_config`'s JSON columns.  The typed money
        fields above already refuse NaN; these two never passed a typed field, so nothing
        looked at their inner numbers.  Refuse at the WRITE boundary (422, field named).
        """
        for field in ("bill_to", "line_items"):
            paths = non_finite_paths(getattr(self, field, None), field)
            if paths:
                raise ValueError(
                    f"{', '.join(paths)} carries NaN/Infinity — not valid JSON and not a "
                    "measurement; send finite numbers (or null for unmeasured)"
                )
        return self


class InvoiceSendRequest(BaseModel):
    """`POST /invoices/{id}/send` — the optional cover note rendered above the invoice."""

    cover_note: str | None = None


class InvoiceVoidRequest(BaseModel):
    """`POST /invoices/{id}/void` — the optional reason recorded in the activity feed."""

    reason: str | None = None


#: The money columns whose stored value must be FINITE before the row may be rendered.
_MONEY_COLUMNS: tuple[str, ...] = ("subtotal", "tax", "total")
#: The FREE-FORM JSON columns whose inner numbers must be FINITE before the row may be rendered
#: (V19/L2 — see `_invoice_response`'s second refusal).
_JSON_COLUMNS: tuple[str, ...] = ("bill_to", "line_items")


def _is_finite_money(value: Any) -> bool:
    """`None` (not measured) and finite numbers pass; NaN / ±Infinity do not."""
    if value is None:
        return True
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _invoice_response(row: Any) -> InvoiceResponse:
    """Serialize one invoice row; a row carrying NON-FINITE money is refused BY NAME (S4-F1).

    A row that predates the boundary refusal (an import script, a hand-edited DB) must not
    500 the whole collection at response render — that is exactly what
    `json.dumps(..., allow_nan=False)` did for a stored `Decimal('Infinity')` (wave-v13/
    s4-errors F1: one dirty row made every `GET /api/v1/invoices` an uncoded 500 forever).
    The refusal (E_DB) names the row and the columns; the value is NOT silently turned into
    `null` ("not measured") — a stored NaN is corruption, not an absence, and laundering it
    would be the silent-None the error doctrine forbids.
    """
    bad = [name for name in _MONEY_COLUMNS if not _is_finite_money(getattr(row, name))]
    if bad:
        raise PolsiaError(
            E_DB,
            f"invoice {row.number} (id {row.id}) carries non-finite money in {', '.join(bad)}"
            " — refusing to render it",
            how_to_fix=(
                "fix or delete this invoice row; non-finite money is import/corruption "
                "damage, not a measurement"
            ),
            context={"invoice_id": row.id, "number": row.number, "columns": bad},
        )
    # V19/L2: the FREE-FORM JSON columns are the same class.  The write boundary now refuses
    # NaN/Infinity (`InvoiceCreateRequest._no_non_finite_numbers_in_free_form_json`), but a
    # pre-fix write, an import or a hand-edited row can still carry one — and pydantic would
    # serve it as `null`, i.e. "not measured" with no reason (the L3-02b laundering).  Refuse
    # the row BY NAME instead; the list stays a coded 503, never a 200 whose nulls lie.
    bad_json = {
        name: paths
        for name in _JSON_COLUMNS
        if (paths := non_finite_paths(getattr(row, name, None), name))
    }
    if bad_json:
        raise PolsiaError(
            E_DB,
            f"invoice {row.number} (id {row.id}) carries non-finite numbers in "
            f"{', '.join(bad_json)} — refusing to render it",
            how_to_fix=(
                "fix or delete this invoice row; a NaN/Infinity inside bill_to/line_items is "
                "import/corruption damage, not a measurement"
            ),
            context={"invoice_id": row.id, "number": row.number, "columns": list(bad_json),
                     "paths": bad_json},
        )
    return InvoiceResponse.model_validate(row)


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
    return _invoice_response(invoice)


@router.get("", response_model=list[InvoiceResponse])
async def list_invoices(
    response: Response,
    status_value: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceResponse]:
    """The invoice list, newest first.

    V20/L1 (card L2-07): `X-Polsia-Total` declares the FULL filtered count (the same `WHERE`,
    without LIMIT) so a full page is distinguishable from a truncated one.  The body stays a bare
    array — every existing client parses it as one.  `status` is validated by the service
    (`invoice_service._coerce_status`), unchanged.
    """
    with refusal_guard():
        rows = await invoice_service.list_invoices(db, status=status_value, limit=limit)
        total = await declared_total(
            db,
            Invoice,
            [] if status_value is None else [Invoice.status == InvoiceStatus(status_value)],
            what="invoices total",
        )
    response.headers[TOTAL_HEADER] = str(total)
    return [_invoice_response(row) for row in rows]


@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    # S4-F3: explicit upper bound — an int past int64 reached the SQLite driver and 500'd
    # (`OverflowError: Python int too large to convert to SQLite INTEGER`); the wire now
    # names the field instead.  Everything <= int64 keeps its old path (404 for unknown ids).
    invoice_id: int = Path(le=2**63 - 1),
    db: AsyncSession = Depends(get_db),
) -> InvoiceResponse:
    """Read one invoice by id — API-completeness read at this HEAD.

    INTENDED STATUS (tracker S3-04, wave v13 wiring sweep; annotated wave v14): the product's
    invoice surface (`frontend/src/app/invoices/page.tsx`) fetches the LIST (`GET /invoices?…`) and
    acts on the rows that list already returned — no in-product control issues this single-item
    read; the only consumer is `e2e/invoices.spec.ts`. It is the honest completeness route (and the
    exact row a future detail sheet would fetch), so it stays. The next sweep should not re-report
    the missing UI caller as a defect without naming that decision.
    """
    with refusal_guard():
        invoice = await invoice_service.get_invoice(db, invoice_id)
    return _invoice_response(invoice)


@router.post("/{invoice_id}/send", response_model=InvoiceResponse)
async def send_invoice(
    invoice_id: int = Path(le=2**63 - 1),  # S4-F3: same int64 bound as the read above
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
    return _invoice_response(invoice)


@router.post("/{invoice_id}/void", response_model=InvoiceResponse)
async def void_invoice(
    invoice_id: int = Path(le=2**63 - 1),  # S4-F3: same int64 bound as the read above
    request: InvoiceVoidRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> InvoiceResponse:
    with refusal_guard():
        invoice = await invoice_service.void_invoice(
            db, invoice_id, reason=request.reason if request else None
        )
    return _invoice_response(invoice)
