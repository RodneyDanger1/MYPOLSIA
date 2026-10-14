"""invoices v1 — numbering, transitions, rendering and the firewalled send path (wave V9, seat b4).

Plan §9 B4 (frozen): the `invoices` table + model + schemas landed in V8/migration 0008; THIS
module grows the service on top of them — NO schema changes, no new columns.

Pinned behaviours, and the module's honest edges:

* **Numbering.** `create_invoice` allocates `INV-YYYY-NNN` (year = the issue date's year,
  sequence = max existing suffix for that year + 1) and writes the row inside a SAVEPOINT.
  The database's UNIQUE constraint on `invoices.number` is the real arbiter: a concurrent
  writer that wins the race makes the flush raise `IntegrityError`, the savepoint rolls back,
  the number is re-derived and the insert is retried (bounded — `MAX_NUMBER_ATTEMPTS`) before a
  named `E_DB` refusal.  The retry is proven against the live constraint in
  `tests/unit/test_v9_invoices.py` (the collision is forced, not simulated).
* **Status transitions.** `draft -> sent -> paid | overdue | void` (and `draft -> void`);
  `paid`/`void` are terminal.  Every illegal attempt is refused BY NAME, naming the current
  status, the requested one and the legal targets of the current state.
* **Send.** Renders text/markdown (cover note + line items + the honest money lines — `null`
  prints as "not recorded", never a fabricated `0.00`) and delivers through the firewalled
  email path: the COMPLIANCE FOOTER first (`app/services/email_compliance.py`, seat b3, written
  in parallel — a late import plus a named refusal keep this seat honest while it does not
  exist), then the policy firewall (`app.agents.side_effects.request_side_effect`), then
  `app.agents.integrations.send_email`.  Sender identity unconfigured ⇒ `E_CONFIG`; sandbox mode
  ⇒ `E_SANDBOX_BLOCKED`; the email caps refuse by name (`E_RATE_LIMITED` / the firewall's
  cap-unknown posture) with the REAL counts from `app.services.channel_service`.  The invoice
  flips to `sent` ONLY on a confirmed provider delivery — a refused send stays a labelled draft.

CROSS-FILE NOTES (recorded in STATE.md; the files are other seats' territory):

* `policy_firewall.AGENT_CAPABILITIES` has no "system invoices" capability; the email channel is
  owned by the `email_outreach` capability, so the firewall consultation carries that agent slot
  (`_SEND_AGENT_SLOT`).  The durable ledger rows this module writes name the real actor ("finance").
* Invoice sends are third-party mail and therefore do NOT write `email_logs` (that table's
  `prospect_id` is NOT NULL): the firewall's email counter undercounts this path — named here
  rather than silently collapsed.
"""
from __future__ import annotations

import inspect
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.policy_firewall import ActionClass
from app.core.errors import (
    E_CONFIG,
    E_DB,
    E_INTEGRATION,
    E_NOT_FOUND,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.core.timeutil import now_utc, today_in
from app.models.invoice import Invoice, InvoiceStatus
from app.services.activity_service import log_activity
from app.services.query_helpers import fetch_scalars

logger = logging.getLogger(__name__)

#: The `INV-YYYY-NNN` wire prefix.
NUMBER_PREFIX = "INV"
#: Bounded retries for the UNIQUE-collision loop (see the module docstring).
MAX_NUMBER_ATTEMPTS = 5
#: The model's documented default terms.
DEFAULT_NET_DAYS = 30
#: The firewall capability consulted for mail on the `email` channel — see the cross-file note.
_SEND_AGENT_SLOT = "email_outreach"

#: Legal status transitions.  `paid`/`void` are terminal; a draft can be sent or voided;
#: a sent invoice can be paid, go overdue or be voided; an overdue one can still be paid/voided.
LEGAL_TRANSITIONS: dict[InvoiceStatus, tuple[InvoiceStatus, ...]] = {
    InvoiceStatus.DRAFT: (InvoiceStatus.SENT, InvoiceStatus.VOID),
    InvoiceStatus.SENT: (InvoiceStatus.PAID, InvoiceStatus.OVERDUE, InvoiceStatus.VOID),
    InvoiceStatus.OVERDUE: (InvoiceStatus.PAID, InvoiceStatus.VOID),
    InvoiceStatus.PAID: (),
    InvoiceStatus.VOID: (),
}


def _format_number(year: int, sequence: int) -> str:
    return f"{NUMBER_PREFIX}-{year:04d}-{sequence:03d}"


async def _next_number(db: AsyncSession, year: int) -> str:
    """The next candidate `INV-YYYY-NNN` for `year`: highest existing suffix + 1.

    A CANDIDATE only — the UNIQUE constraint arbitrates (see `create_invoice`).  Suffixes are
    parsed as integers, so the sequence keeps counting past `NNN = 999` without re-using a gap.
    """
    prefix = f"{NUMBER_PREFIX}-{year:04d}-"
    numbers = await fetch_scalars(
        db, select(Invoice.number).where(Invoice.number.like(f"{prefix}%")), what="invoice numbers"
    )
    highest = 0
    for number in numbers:
        suffix = str(number)[len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return _format_number(year, highest + 1)


def _coerce_status(value: Any) -> InvoiceStatus:
    if isinstance(value, InvoiceStatus):
        return value
    try:
        return InvoiceStatus(value)
    except ValueError as exc:
        legal = ", ".join(status.value for status in InvoiceStatus)
        raise PolsiaError(
            E_VALIDATION,
            f"{value!r} is not an invoice status; the legal values are: {legal}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"value": repr(value)[:100], "legal": legal},
        ) from exc


async def create_invoice(
    db: AsyncSession,
    *,
    bill_to: dict[str, Any] | None = None,
    line_items: list[dict[str, Any]] | None = None,
    issue_date: date | None = None,
    due_date: date | None = None,
    net_days: int | None = None,
    currency: str | None = None,
    subtotal: Decimal | None = None,
    tax: Decimal | None = None,
    total: Decimal | None = None,
    payment_link: str | None = None,
) -> Invoice:
    """Insert one DRAFT invoice with a server-assigned number (UNIQUE-retry, see the docstring).

    Money passes through EXACTLY as given: `None` stays NULL ("unmeasured" — never a fabricated
    zero), so the caller decides what was measured.  `due_date` defaults to
    `issue_date + net_days` (net_days default 30); `issue_date` defaults to the company-local
    today (`today_in`).  The caller owns the transaction — this flushes, it never commits.
    """
    issue = issue_date or today_in()
    days = DEFAULT_NET_DAYS if net_days is None else int(net_days)
    if days < 0:
        raise PolsiaError(
            E_VALIDATION,
            f"net_days must be >= 0, got {days}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"net_days": days},
        )
    due = due_date or issue + timedelta(days=days)

    last_collision: IntegrityError | None = None
    for _attempt in range(1, MAX_NUMBER_ATTEMPTS + 1):
        number = await _next_number(db, issue.year)
        invoice = Invoice(
            number=number,
            status=InvoiceStatus.DRAFT,
            issue_date=issue,
            due_date=due,
            net_days=days,
            currency=currency or "USD",
            bill_to=bill_to,
            line_items=line_items,
            subtotal=subtotal,
            tax=tax,
            total=total,
            payment_link=payment_link,
        )
        try:
            # SAVEPOINT: a UNIQUE loss must roll back ONLY this insert — the caller's
            # transaction (and any prior work in it) stays intact for the retry.
            async with db.begin_nested():
                db.add(invoice)
                await db.flush()
        except IntegrityError as exc:
            last_collision = exc
            if invoice in db:  # savepoint rollback normally expunges it; belt and braces
                db.expunge(invoice)
            continue
        return invoice

    raise PolsiaError(
        E_DB,
        f"invoice number for {issue.year} could not be allocated: every candidate collided "
        f"with a constraint ({MAX_NUMBER_ATTEMPTS} attempts); last error: {last_collision}",
        how_to_fix=ERROR_HINTS[E_DB],
        context={"year": issue.year, "attempts": MAX_NUMBER_ATTEMPTS},
    )


async def get_invoice(db: AsyncSession, invoice_id: int) -> Invoice:
    """One invoice by id, or a named `E_NOT_FOUND`."""
    invoice = (
        await db.execute(
            select(Invoice)
            .where(Invoice.id == invoice_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"invoice {invoice_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"invoice_id": invoice_id},
        )
    return invoice


async def list_invoices(
    db: AsyncSession, *, status: str | None = None, limit: int = 100
) -> list[Invoice]:
    """Invoices newest-first; optionally filtered by status (an illegal status refuses by name)."""
    stmt = select(Invoice)
    if status is not None:
        stmt = stmt.where(Invoice.status == _coerce_status(status))
    return await fetch_scalars(db, stmt.order_by(Invoice.id.desc()).limit(limit), what="invoices")


async def transition_invoice(
    db: AsyncSession,
    invoice: Invoice,
    to: InvoiceStatus | str,
    *,
    at: datetime | None = None,
) -> Invoice:
    """Enforce the status matrix; illegal moves refuse BY NAME, naming the legal targets.

    Entering `paid` records `paid_at` (the moment the payment was recorded) — every other
    target leaves the other columns untouched.  Flushes; never commits.
    """
    target = _coerce_status(to)
    current = _coerce_status(invoice.status)
    if target is current:
        raise PolsiaError(
            E_VALIDATION,
            f"invoice {invoice.number} is already {current.value}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"invoice_id": invoice.id, "status": current.value},
        )
    legal = LEGAL_TRANSITIONS[current]
    if target not in legal:
        targets = ", ".join(status.value for status in legal) or "none (terminal state)"
        raise PolsiaError(
            E_VALIDATION,
            f"illegal invoice transition {current.value} -> {target.value} for invoice "
            f"{invoice.number}; from {current.value} the legal targets are: {targets}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"invoice_id": invoice.id, "from": current.value, "to": target.value},
        )
    invoice.status = target
    if target is InvoiceStatus.PAID:
        invoice.paid_at = at or now_utc()
    await db.flush()
    return invoice


async def void_invoice(
    db: AsyncSession, invoice_id: int, *, reason: str | None = None
) -> Invoice:
    """Void one invoice (legal from draft/sent/overdue; terminal) and record it in the feed."""
    invoice = await get_invoice(db, invoice_id)
    await transition_invoice(db, invoice, InvoiceStatus.VOID)
    suffix = f": {reason}" if reason else ""
    await log_activity(
        db,
        "finance",
        "invoice_voided",
        summary=f"Invoice {invoice.number} voided{suffix}",
        detail={"invoice_id": invoice.id, "number": invoice.number, "reason": reason},
    )
    return invoice


def _money_text(value: Decimal | float | None, currency: str) -> str:
    """A stored money value printed exactly — `None` is "not recorded", never a fake `0.00`."""
    if value is None:
        return "not recorded"
    return f"{Decimal(value):.2f} {currency}"


def render_invoice_email(invoice: Invoice, *, cover_note: str | None = None) -> tuple[str, str]:
    """`(subject, body)` — markdown/text for one invoice.  No footer here: the footer is added
    by the firewalled send path (`deliver_invoice_email`), which is the only code that may
    compose a customer-facing message.

    Every money line prints the stored value or "not recorded" — an unmeasured invoice never
    renders `$0.00` (the same state rule the finance surfaces keep).
    """
    amount = _money_text(invoice.total, invoice.currency)
    subject = f"Invoice {invoice.number}: {amount} due {invoice.due_date.isoformat()}"
    lines: list[str] = []
    if cover_note and cover_note.strip():
        lines.extend([cover_note.strip(), ""])
    lines.append(f"# Invoice {invoice.number}")
    lines.append(f"- Issued: {invoice.issue_date.isoformat()}")
    lines.append(f"- Due: {invoice.due_date.isoformat()} (net {invoice.net_days})")
    bill_to = invoice.bill_to if isinstance(invoice.bill_to, dict) else None
    if bill_to and bill_to.get("name"):
        lines.append(f"- Bill to: {bill_to['name']}")
    if invoice.line_items:
        lines.append("")
        lines.append("## Items")
        for item in invoice.line_items:
            if isinstance(item, dict):
                description = str(item.get("description") or "item")
                lines.append(
                    f"- {description} — {_money_text(item.get('amount'), invoice.currency)}"
                )
            else:
                lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Summary",
            f"- Subtotal: {_money_text(invoice.subtotal, invoice.currency)}",
            f"- Tax: {_money_text(invoice.tax, invoice.currency)}",
            f"- Total: {amount}",
            "",
        ]
    )
    if invoice.payment_link:
        lines.append(f"Pay here: {invoice.payment_link}")
    else:
        lines.append("Payment link: not recorded")
    return subject, "\n".join(lines).strip() + "\n"


async def _compliance_footer(db: AsyncSession) -> str:
    """The COMPLIANCE FOOTER for a customer-facing invoice email — or a named refusal.

    Seat b3's `app/services/email_compliance.py` landed mid-wave; its real signature is
    `footer_for_company(company, *, include_ai_disclosure=True) -> str` — SYNC, taking the
    `CompanyConfig` row (or a mapping) and raising its own `E_CONFIG` ("sender identity not
    configured") while the 0008 identity pair / `website_url` is unset.  This call site loads
    the company row and hands it over; `inspect.isawaitable` stays as the compatibility seam if
    the module ever becomes async (recorded in STATE.md), and the empty-text guard below means
    this function can never hand a non-compliant message to the provider.
    """
    try:
        from app.services.company_service import get_company
        from app.services.email_compliance import footer_for_company
    except ImportError as exc:
        raise PolsiaError(
            E_CONFIG,
            "invoice email refused: app/services/email_compliance.py is not present, so no "
            "compliant footer can be rendered (sender identity cannot be verified)",
            how_to_fix=(
                "land the compliance module (wave V9, seat b3) or configure the sender identity "
                "fields (sender_legal_name / sender_postal_address) and retry"
            ),
            context={"module": "app.services.email_compliance"},
        ) from exc
    company = await get_company(db)
    footer = footer_for_company(company)  # its own E_CONFIG refusal propagates by name
    if inspect.isawaitable(footer):
        footer = await footer
    text = str(footer or "").strip()
    if not text:
        raise PolsiaError(
            E_CONFIG,
            "invoice email refused: the compliance footer is empty — sender identity is not "
            "configured",
            how_to_fix=(
                "set sender_legal_name and sender_postal_address in the company settings, then retry"
            ),
            context={},
        )
    return text


def _recipient(invoice: Invoice) -> str:
    """The bill-to email, or a named refusal (an invoice with no address cannot be sent)."""
    bill_to = invoice.bill_to
    email = bill_to.get("email") if isinstance(bill_to, dict) else None
    if not isinstance(email, str) or not email.strip():
        raise PolsiaError(
            E_VALIDATION,
            f"invoice {invoice.number} has no bill_to.email; the notice cannot be addressed",
            how_to_fix=(
                "set bill_to.email on the invoice (POST /invoices accepts it at create time) "
                "before sending"
            ),
            context={"invoice_id": invoice.id, "bill_to": bill_to},
        )
    return email.strip()


async def deliver_invoice_email(
    db: AsyncSession, invoice: Invoice, *, subject: str, body: str
) -> dict[str, Any]:
    """The ONE firewalled egress for invoice mail: footer, recipient, policy, provider.

    Order, and why:

    1. the COMPLIANCE FOOTER (`_compliance_footer`) — a compliant message is a hard
       prerequisite of composing one at all;
    2. the recipient (`_recipient`) — no address, no send;
    3. the POLICY FIREWALL (`request_side_effect` with the email channel capability): sandbox
       mode refuses the external action (`E_SANDBOX_BLOCKED`), and the configured email caps
       refuse by name (`E_RATE_LIMITED`, or the cap-unknown posture when the count has no
       source) from the REAL counts `channel_service` measures;
    4. the provider call (`integrations.send_email`, lazy import — no provider libraries at
       import time, CLAUDE.md:116).  A provider refusal is a named `E_INTEGRATION`, never a
       success-shaped empty result.

    Writes NOTHING to the database (the caller owns the ledger row and the transaction) and
    never fabricates: a message only leaves this function via a confirmed `{"ok": True}`.
    """
    from app.agents import integrations  # lazy: the provider library edge
    from app.agents.side_effects import request_side_effect
    from app.services import channel_service

    footer = await _compliance_footer(db)
    recipient = _recipient(invoice)
    payload = {
        "action": "send_invoice",
        "invoice": invoice.number,
        "to_domain": recipient.split("@")[-1] if "@" in recipient else "",
    }
    outcome = request_side_effect(
        _SEND_AGENT_SLOT,
        ActionClass.EXTERNAL_REVERSIBLE,
        "email",
        action="send_invoice",
        # An invoice EMAIL moves no money (the payment link does); the firewall's value
        # thresholds gate spend/refund actions, so they are not the measure here.
        value_usd=0.0,
        payload=payload,
        sends_today=await channel_service.channel_sends_today(db, "email"),
        sends_this_hour=await channel_service.channel_sends_this_hour(db, "email"),
    )
    if not outcome.allowed:
        if outcome.decision is not None:
            raise outcome.decision.to_polsia_error(agent_type=_SEND_AGENT_SLOT, action="send_invoice")
        raise PolsiaError(  # pragma: no cover - outcome always carries its decision
            "E_POLICY_DENIED",
            f"invoice {invoice.number} email refused by policy: {outcome.reason}",
            how_to_fix=ERROR_HINTS["E_POLICY_DENIED"],
        )
    composed = f"{body.rstrip()}\n\n{footer}\n"
    result = integrations.send_email(to=recipient, subject=subject, body=composed)
    if not isinstance(result, dict) or not result.get("ok"):
        detail = ""
        if isinstance(result, dict):
            error = result.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
        raise PolsiaError(
            E_INTEGRATION,
            f"invoice {invoice.number} email to {recipient} was refused by the provider: "
            f"{detail or result!r}",
            how_to_fix=ERROR_HINTS[E_INTEGRATION],
            context={"invoice_id": invoice.id, "to": recipient},
        )
    return result


async def send_invoice(
    db: AsyncSession, invoice_id: int, *, cover_note: str | None = None
) -> Invoice:
    """Send a DRAFT invoice: render, deliver through the firewalled path, flip to `sent`.

    The draft->sent flip happens ONLY on a confirmed provider delivery; every refusal
    (`E_CONFIG` / `E_SANDBOX_BLOCKED` / `E_RATE_LIMITED` / `E_INTEGRATION`) propagates by name
    and leaves the invoice exactly as it was — a labelled draft, never a fake `sent`.
    """
    invoice = await get_invoice(db, invoice_id)
    if invoice.status is not InvoiceStatus.DRAFT:
        raise PolsiaError(
            E_VALIDATION,
            f"only a draft invoice can be sent; invoice {invoice.number} is "
            f"{_coerce_status(invoice.status).value} (the dunning ladder owns follow-ups on a "
            f"sent invoice)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"invoice_id": invoice.id, "status": str(invoice.status)},
        )
    subject, body = render_invoice_email(invoice, cover_note=cover_note)
    result = await deliver_invoice_email(db, invoice, subject=subject, body=body)
    await transition_invoice(db, invoice, InvoiceStatus.SENT)
    provider_result = result.get("result")
    message_id = str(provider_result.get("message_id") or "") if isinstance(provider_result, dict) else ""
    await log_activity(
        db,
        "finance",
        "invoice_sent",
        summary=f"Invoice {invoice.number} sent to {_recipient(invoice)}",
        detail={
            "invoice_id": invoice.id,
            "number": invoice.number,
            "to": _recipient(invoice),
            # `message_id or None`: a provider that returned none is "not recorded", not "".
            "message_id": message_id or None,
        },
    )
    return invoice


__all__ = [
    "DEFAULT_NET_DAYS",
    "LEGAL_TRANSITIONS",
    "MAX_NUMBER_ATTEMPTS",
    "NUMBER_PREFIX",
    "create_invoice",
    "deliver_invoice_email",
    "get_invoice",
    "list_invoices",
    "render_invoice_email",
    "send_invoice",
    "transition_invoice",
    "void_invoice",
]
