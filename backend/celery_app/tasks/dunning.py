"""The dunning ladder — daily beat tick for unpaid invoices (wave V9, seat b4).

**The beat ENTRY is the PARENT's** (V9-D4: the parent appends the daily `dunning` entry to
`celery_app/beat_schedule.py` at settle, with the pin moves, in the same patch).  This module
lands the task; nothing here touches the schedule file.

THE LADDER (frozen): pre-due −3 days, due day, +5, +15, +30 (final).  Every email carries the
invoice NUMBER, the AMOUNT (printed exactly, or "amount not recorded" — money never fabricates
zeros), the DUE DATE, the SAME `payment_link` as the invoice row, and exactly ONE ask.

EACH STEP AT MOST ONCE, **enforced column-free** — the plan's own ruling (frozen plan §9 B4):

    "use activity rows as the ledger: send each step at most once by checking the
     activity/email_logs for that invoice+step — honest and column-free"

So the ledger is activity rows with action `invoice_dunning`, keyed by
`(detail.invoice_id, detail.step)`.  A step enters the ledger ONLY after a CONFIRMED provider
delivery (the same "real send or named refusal" rule the send path keeps): a refused send
consumes nothing and is retried on the next tick — the refusal is reported BY NAME in the tick
result AND recorded as an `invoice_dunning_refused` activity row (never silent).

Catch-up: a tick fires **the LATEST owed-and-unsent step, at most one per invoice** — no
cascade of stale reminders.  Steps the tick supersedes that way are NAMED in the fired row's
`superseded` detail, never silently dropped.

The one write this ladder makes besides the ledger: a still-`sent` invoice fires its first
POST-due step (+5 and later) and IS factually overdue, so the row is transitioned
`sent -> overdue` as part of the same step (via `invoice_service.transition_invoice`, the one
enforcement point of the status matrix).

Residual, recorded: two concurrent ticks could both read a ledger without a step and both
send — the same accepted column-free window the plan rules; the beat is single-worker daily.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.errors import E_VALIDATION, PolsiaError
from app.core.timeutil import company_tz, now_utc
from app.models.invoice import Invoice, InvoiceStatus
from app.models.report import ActivityLog
from app.services.activity_service import log_activity
from app.services.query_helpers import fetch_scalars
from celery_app.tasks._common import run_async
from celery_app.worker import app

logger = logging.getLogger(__name__)

#: The ledger action — one row per REALLY SENT step (the once-per-step enforcement).
DUNNING_ACTION = "invoice_dunning"
#: The named-refusal record (warning level) — never silent about a refused tick.
DUNNING_REFUSAL_ACTION = "invoice_dunning_refused"
#: The offset (days past due) at which a still-`sent` invoice is factually overdue.
PAST_DUE_FLIP_AT_DAYS = 5


@dataclass(frozen=True)
class DunningStep:
    """One rung.  `offset_days` is relative to `due_date` (negative = before it);
    `ask` is THE one ask the email carries (verbatim)."""

    key: str
    offset_days: int
    ask: str


DUNNING_STEPS: tuple[DunningStep, ...] = (
    DunningStep("pre_due", -3, "Please pay by the due date above."),
    DunningStep("due", 0, "Please pay today — this invoice is due."),
    DunningStep("due_plus_5", 5, "Please pay now to keep the account in good standing."),
    DunningStep("due_plus_15", 15, "Please pay now — this invoice is 15 days past due."),
    DunningStep("due_plus_30", 30, "Final notice: please pay now to avoid further collection steps."),
)


def step_date(due: date, step: DunningStep) -> date:
    """The calendar date a step is owed for an invoice due at `due`."""
    return due + timedelta(days=step.offset_days)


def compose_dunning_email(invoice: Invoice, step: DunningStep) -> tuple[str, str]:
    """`(subject, body)` for one ladder step — number, amount, due date, link, ONE ask.

    A missing `payment_link` refuses BY NAME: the step's whole ask is "pay", and an email that
    carries no way to pay is a dead end — refused, recorded, retried once the link exists.
    """
    if not invoice.payment_link:
        raise PolsiaError(
            E_VALIDATION,
            f"invoice {invoice.number} has no payment_link; a dunning email cannot carry a "
            f"payment route",
            how_to_fix=(
                "set payment_link on the invoice, then the next tick can fire this step "
                "(nothing was consumed)"
            ),
            context={"invoice_id": invoice.id, "step": step.key},
        )
    amount = (
        "amount not recorded"
        if invoice.total is None
        else f"{invoice.total:.2f} {invoice.currency}"
    )
    subject = f"Invoice {invoice.number}: {amount} due {invoice.due_date.isoformat()}"
    body = "\n".join(
        [
            step.ask,
            "",
            f"Invoice {invoice.number}",
            f"Amount: {amount}",
            f"Due date: {invoice.due_date.isoformat()}",
            f"Pay here: {invoice.payment_link}",
        ]
    )
    return subject, body


async def _sent_steps(db) -> dict[int, set[str]]:  # noqa: ANN001 - AsyncSession, kept local
    """The column-free ledger: `{invoice_id: {step keys}}` from the `invoice_dunning` rows."""
    rows = await fetch_scalars(
        db,
        select(ActivityLog)
        .where(ActivityLog.action == DUNNING_ACTION)
        .order_by(ActivityLog.id),
        what="dunning ledger",
    )
    ledger: dict[int, set[str]] = {}
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        invoice_id = detail.get("invoice_id")
        step = detail.get("step")
        if isinstance(invoice_id, int) and isinstance(step, str):
            ledger.setdefault(invoice_id, set()).add(step)
    return ledger


@app.task(name="celery_app.tasks.dunning.run_dunning_ladder")
def run_dunning_ladder() -> dict[str, Any]:
    """The daily Beat tick (the beat entry itself is the parent's, V9-D4)."""
    return run_async(_run_dunning())


async def _dun_one(
    session_factory: Any,  # noqa: ANN401 - async_sessionmaker, kept dependency-free for tests
    invoice_id: int,
    today: date,
    *,
    sent: list[dict[str, Any]],
    refused: list[dict[str, Any]],
) -> None:
    """One invoice, one session: re-read state, fire at most ONE owed step, commit the ledger."""
    from app.services.invoice_service import deliver_invoice_email, transition_invoice

    async with session_factory() as db:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == invoice_id))
        ).scalar_one_or_none()
        if invoice is None or invoice.status not in (
            InvoiceStatus.SENT,
            InvoiceStatus.OVERDUE,
        ):
            return  # paid/void/draft never get a dunning mail
        already = (await _sent_steps(db)).get(invoice_id, set())
        pending = [
            step
            for step in DUNNING_STEPS
            if step_date(invoice.due_date, step) <= today and step.key not in already
        ]
        if not pending:
            return
        step = pending[-1]  # the LATEST owed step: one email per invoice per tick, no cascade
        superseded = [earlier.key for earlier in pending[:-1]]
        try:
            subject, body = compose_dunning_email(invoice, step)
            result = await deliver_invoice_email(db, invoice, subject=subject, body=body)
        except PolsiaError as exc:
            refused.append(
                {
                    "invoice_id": invoice_id,
                    "number": invoice.number,
                    "step": step.key,
                    "code": exc.code,
                    "message": exc.message,
                }
            )
            await log_activity(
                db,
                "finance",
                DUNNING_REFUSAL_ACTION,
                summary=(
                    f"Dunning {step.key} for invoice {invoice.number} refused by name: "
                    f"{exc.code}: {exc.message}"
                ),
                level="warning",
                detail={
                    "invoice_id": invoice_id,
                    "number": invoice.number,
                    "step": step.key,
                    "code": exc.code,
                    "message": exc.message,
                },
            )
            await db.commit()  # the refusal record is durable; nothing was sent or consumed
            return
        flipped_to: str | None = None
        if step.offset_days >= PAST_DUE_FLIP_AT_DAYS and invoice.status is InvoiceStatus.SENT:
            await transition_invoice(db, invoice, InvoiceStatus.OVERDUE)
            flipped_to = InvoiceStatus.OVERDUE.value
        provider_result = result.get("result")
        message_id = str(provider_result.get("message_id") or "") if isinstance(provider_result, dict) else ""
        await log_activity(
            db,
            "finance",
            DUNNING_ACTION,
            summary=(
                f"Dunning {step.key} sent for invoice {invoice.number} "
                f"({step_date(invoice.due_date, step).isoformat()})"
            ),
            detail={
                "invoice_id": invoice_id,
                "number": invoice.number,
                "step": step.key,
                "step_date": step_date(invoice.due_date, step).isoformat(),
                "superseded": superseded,
                "status_flipped_to": flipped_to,
                "message_id": message_id or None,
            },
        )
        sent.append(
            {
                "invoice_id": invoice_id,
                "number": invoice.number,
                "step": step.key,
                "superseded": superseded,
                "status_flipped_to": flipped_to,
                "message_id": message_id or None,
            }
        )
        # The ledger row must be durable the moment it exists — "at most once" IS this commit.
        await db.commit()


async def _run_dunning(now: datetime | None = None) -> dict[str, Any]:
    """The async body of `run_dunning_ladder`; `now` is the frozen-clock seam for the tests
    (production passes nothing and reads the real clock once per tick)."""
    from app.core.database import get_session_factory

    today = (now or now_utc()).astimezone(company_tz()).date()
    session_factory = get_session_factory()
    sent: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    async with session_factory() as db:
        open_ids = await fetch_scalars(
            db,
            select(Invoice.id)
            .where(Invoice.status.in_((InvoiceStatus.SENT, InvoiceStatus.OVERDUE)))
            .order_by(Invoice.id),
            what="open invoices",
        )
    for invoice_id in open_ids:
        await _dun_one(
            session_factory, int(invoice_id), today, sent=sent, refused=refused
        )

    result: dict[str, Any] = {
        "status": "completed" if (sent or refused) else "no_due",
        "date": today.isoformat(),
        "sent": sent,
        "refused": refused,
        "steps": [step.key for step in DUNNING_STEPS],
    }
    logger.info("dunning ladder: %s", result)
    return result


__all__ = [
    "DUNNING_ACTION",
    "DUNNING_REFUSAL_ACTION",
    "DUNNING_STEPS",
    "PAST_DUE_FLIP_AT_DAYS",
    "DunningStep",
    "compose_dunning_email",
    "run_dunning_ladder",
    "step_date",
]
