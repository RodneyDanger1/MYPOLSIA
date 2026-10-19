"""Wire schema for the invoice row (wave V8 schema, migration 0008; V9 grows the service).

V8 lands the schema ONLY (drift ledger V8-D2: no invoices router before V9).  This response model
describes the frozen columns of `app.models.invoice.Invoice`: money stays `None` when unmeasured
(never a fabricated 0 — the same wire rule the finance surfaces keep) and `status` is one of
draft|sent|paid|void|overdue.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.schemas.common import ORMModel, UtcDatetime


class InvoiceResponse(ORMModel):
    id: int
    number: str
    bill_to: dict[str, Any] | None = None
    issue_date: date
    due_date: date
    net_days: int
    currency: str
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    status: str
    payment_link: str | None = None
    paid_at: UtcDatetime | None = None
    late_fee_note: str | None = None
    line_items: list[dict[str, Any]] | None = None
