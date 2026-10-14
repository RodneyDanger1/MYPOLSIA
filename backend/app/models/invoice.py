"""invoices — outgoing invoices (wave V8 migration 0008; V9 grows the service/routes).

Frozen interface (plan section 7 C2): the table `invoices`, the class `Invoice` importable from
`app.models.invoice`, the columns named exactly below.  V8 lands the schema ONLY — no router exists
until V9 (drift ledger V8-D2, which is also why `main.py` does not import an invoices router).

Column semantics:

* `number` is the human identity, UNIQUE at the database (`INV-YYYY-NNN` on the wire; the sequence
  is computed under a transaction by V9's service — the unique constraint is the backstop);
* `bill_to` is the recipient block (JSON) and `line_items` the itemized rows (JSON v0 — no child
  table yet);
* `issue_date` / `due_date` are calendar dates; `net_days` the payment terms linking them (default
  30); `currency` defaults to 'USD';
* money is `NUMERIC(12, 2)` like every money column (A-T17/T18: exact at rest) and stays NULL when
  unmeasured — `subtotal` / `tax` / `total` are ALL nullable; `tax` defaults to 0 only as the
  starting point of a fresh invoice line, never as a fabricated measurement;
* `status` is the named value set `draft` -> `sent` -> `paid` | `void` | `overdue` (named CHECK,
  `ck_invoices_status`);
* `payment_link`, `paid_at` and `late_fee_note` carry the collections story — all nullable.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, Enum, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class InvoiceStatus(enum.StrEnum):
    """The `invoices.status` value set (the invoice lifecycle)."""

    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    VOID = "void"
    OVERDUE = "overdue"


def invoice_status_values(enum_cls: type[enum.Enum] = InvoiceStatus) -> list[str]:
    """`Enum.values_callable`: persist the member VALUES, in definition order (task.py's rule)."""
    return [str(member.value) for member in enum_cls]


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    bill_to: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    net_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30", default=30)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="USD", default="USD"
    )
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), server_default="0", default=0)
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(
            InvoiceStatus,
            native_enum=False,          # VARCHAR + CHECK on every backend
            create_constraint=True,     # the named CHECK is the at-rest enforcement
            name="ck_invoices_status",
            length=50,
            values_callable=invoice_status_values,
        ),
        nullable=False,
    )
    payment_link: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    late_fee_note: Mapped[str | None] = mapped_column(Text)
    line_items: Mapped[list[dict[str, Any]] | None] = mapped_column(json_type())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Invoice id={self.id} {self.number!r} {self.status}>"
