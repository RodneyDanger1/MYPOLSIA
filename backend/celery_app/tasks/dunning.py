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

The ledger is activity rows keyed by `(detail.invoice_id, detail.step)`.  F5 (V11/W2) made the
consumption DURABLE across the send itself: the tick **CLAIMS before it SENDS** — it writes and
COMMITS an `invoice_dunning_claim` row (state `pending`) first, so a crash between a confirmed
delivery and the ledger write can never re-send a step (the claim blocks it; at-most-once is
the chosen side of the trade — a lost reminder beats a duplicate ask).  After the delivery the
claim is finalized (`state: sent` + message_id) and the delivered `invoice_dunning` row is
written; a send REFUSED BY NAME releases the claim (`state: refused`) — a refusal consumes
nothing and the step fires on a later tick (the V9 refusal contract, unchanged).  Claims still
`pending` at a tick's start are NAMED in its result (`unfinalized_claims`) — a crash window is
visible, never silent.

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
#: F5 (V11/W2): the pre-send CLAIM row — written and COMMITTED before the provider call, so
#: the crash window between send and ledger can never re-send a step.  `state` in its detail
#: moves pending -> sent (finalized) or pending -> refused (released).
CLAIM_ACTION = "invoice_dunning_claim"
CLAIM_STATE_PENDING = "pending"
CLAIM_STATE_SENT = "sent"
CLAIM_STATE_REFUSED = "refused"
#: The COMPANY-LOCAL hour the ladder self-gates on (V12/W-C, audit F12 — the cycles' T2 pattern:
#: the beat entry is hourly and the 23 non-matching ticks return {"status": "off"} in ms).
DUNNING_LOCAL_HOUR = 8
#: The named-refusal record (warning level) — never silent about a refused tick.
DUNNING_REFUSAL_ACTION = "invoice_dunning_refused"
#: L3-07 (V18): the named-failure record (error level) for one step that raised a NON-`PolsiaError`.
#: The step's claim stays `pending` (at-most-once: the provider may have seen the message), but
#: the failure is durable and named instead of aborting the remaining invoices.
DUNNING_ERROR_ACTION = "invoice_dunning_error"
#: The code that failure carries — this module's named failure, the `E_RUN_ARTIFACTS_PERSIST_FAILED`
#: precedent (`celery_app/tasks/agent_tasks.py:202`).
DUNNING_STEP_FAILED_CODE = "E_DUNNING_STEP_FAILED"
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
    currency = str(invoice.currency or "").strip()  # F20/V12-W-C: rows can carry NULL currency
    amount = (
        "amount not recorded"
        if invoice.total is None
        else (
            f"{invoice.total:.2f} {currency}"
            if currency
            else f"{invoice.total:.2f} (currency not recorded)"
        )
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


def _consumed_keys(detail: dict[str, Any]) -> list[str]:
    """The step keys one ledger/claim row CONSUMES: its own `step` plus every `superseded` rung.

    L3-01 (V17): a fired rung records the earlier owed rungs it superseded in `detail.superseded`
    ("the LATEST owed step: one email per invoice per tick, no cascade").  Before this helper only
    the fired rung counted as consumed, so the NEXT daily tick walked the ladder BACKWARDS through
    the stale rungs it had just declared superseded — a pre-due "please pay by the due date"
    reminder arriving after the final notice, one per day.  `superseded` is therefore consumption,
    not decoration; a REFUSED claim is still not (a refusal consumes nothing).
    """
    keys: list[str] = []
    step = detail.get("step")
    if isinstance(step, str):
        keys.append(step)
    superseded = detail.get("superseded")
    if isinstance(superseded, list):
        keys.extend(item for item in superseded if isinstance(item, str))
    return keys


async def _sent_steps(db, *, claims=None) -> dict[int, set[str]]:  # noqa: ANN001 - AsyncSession, kept local
    # F16 (V12/W-C): `claims` lets the tick pass its ONE claim read through instead of this
    # reading the claim ledger again (and the delivered ledger was re-read per invoice).
    """The column-free ledger: `{invoice_id: {step keys}}` the tick treats as CONSUMED.

    Consumed = a delivered `invoice_dunning` row (the V9 ledger), OR a claim row (F5/V11)
    whose state is anything but `refused` — a pending claim is exactly the crash-window row
    that must keep the step from re-sending.  A row ALSO consumes the rungs it names in
    `detail.superseded` (`_consumed_keys`): the catch-up rule fires the latest owed rung and
    drops the stale earlier ones, so those must never come back on a later tick.
    """
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
        if isinstance(invoice_id, int):
            ledger.setdefault(invoice_id, set()).update(_consumed_keys(detail))
    for invoice_id, by_step in (claims if claims is not None else await _claim_rows(db)).items():
        for _step, row in by_step.items():
            detail = row.detail if isinstance(row.detail, dict) else {}
            if detail.get("state") != CLAIM_STATE_REFUSED:
                ledger.setdefault(invoice_id, set()).update(_consumed_keys(detail))
    return ledger


async def _claim_rows(db) -> dict[int, dict[str, ActivityLog]]:  # noqa: ANN001 - AsyncSession
    """`{invoice_id: {step: claim row}}` from the `invoice_dunning_claim` rows (latest wins)."""
    rows = await fetch_scalars(
        db,
        select(ActivityLog)
        .where(ActivityLog.action == CLAIM_ACTION)
        .order_by(ActivityLog.id),
        what="dunning claims",
    )
    claims: dict[int, dict[str, ActivityLog]] = {}
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        invoice_id = detail.get("invoice_id")
        step = detail.get("step")
        if isinstance(invoice_id, int) and isinstance(step, str):
            claims.setdefault(invoice_id, {})[step] = row
    return claims


@app.task(name="celery_app.tasks.dunning.run_dunning_ladder")
def run_dunning_ladder() -> dict[str, Any]:
    """The hourly Beat tick, SELF-GATED on the company-local hour (V12/W-C, audit F12).

    The old beat entry pinned 13:00 UTC and claimed "08:00 company-CT" — for any other
    COMPANY_TIMEZONE the ladder's date boundary moved with it.  Ticks outside
    `DUNNING_LOCAL_HOUR` return `off` and write nothing (the morning/evening/night T2 gate).
    """
    local_now = now_utc().astimezone(company_tz())
    if local_now.hour != DUNNING_LOCAL_HOUR:
        return {
            "status": "off",
            "date": local_now.date().isoformat(),
            "company_hour": local_now.hour,
            "self_gate_hour": DUNNING_LOCAL_HOUR,
        }
    return run_async(_run_dunning())


async def _dun_one(
    session_factory: Any,  # noqa: ANN401 - async_sessionmaker, kept dependency-free for tests
    invoice_id: int,
    today: date,
    *,
    sent: list[dict[str, Any]],
    refused: list[dict[str, Any]],
    consumed: dict[int, set[str]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> None:
    """One invoice, one session: re-read state, fire at most ONE owed step, commit the ledger.

    `consumed` is the tick's ONCE-read consumed-steps map (F16/V12-W-C); a direct call without
    it still reads the ledger for itself (the per-invoice fallback keeps single calls honest).

    `errors` (L3-07/V18) is the tick's named-failure sink: a NON-`PolsiaError` raised inside the
    send step is recorded there (and in `invoice_dunning_error`) instead of escaping and killing
    the remaining invoices.  The step's claim stays `pending` — the at-most-once rule is
    unchanged: an unknown failure never re-sends.
    """
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
        already = (
            consumed if consumed is not None else await _sent_steps(db)
        ).get(invoice_id, set())
        pending = [
            step
            for step in DUNNING_STEPS
            if step_date(invoice.due_date, step) <= today and step.key not in already
        ]
        if not pending:
            return
        step = pending[-1]  # the LATEST owed step: one email per invoice per tick, no cascade
        superseded = [earlier.key for earlier in pending[:-1]]
        # F5 (V11/W2) — CLAIM BEFORE SEND.  The claim row is committed BEFORE the provider can
        # be reached: from this instant the step counts as consumed (`_sent_steps` reads it), so
        # a crash between a confirmed delivery and the ledger write can never re-send it.  The
        # claim is released again if — and only if — the send is refused BY NAME below.
        claim = await log_activity(
            db,
            "finance",
            CLAIM_ACTION,
            summary=(
                f"Dunning {step.key} for invoice {invoice.number} claimed for dispatch "
                f"(at most once from this instant)"
            ),
            detail={
                "invoice_id": invoice_id,
                "number": invoice.number,
                "step": step.key,
                "state": CLAIM_STATE_PENDING,
                "superseded": superseded,
                "claim_of": DUNNING_ACTION,
            },
        )
        await db.commit()  # the claim must be DURABLE before the send (F5's whole point)
        try:
            subject, body = compose_dunning_email(invoice, step)
            result = await deliver_invoice_email(db, invoice, subject=subject, body=body)
        except PolsiaError as exc:
            # Release the claim: a refusal consumes nothing (the V9 rule), so the step stays
            # owed and fires on a later tick once the refusal's cause is gone.
            claim.detail = {
                **(claim.detail if isinstance(claim.detail, dict) else {}),
                "state": CLAIM_STATE_REFUSED,
                "code": exc.code,
                "message": exc.message,
            }
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
            await db.commit()  # the refusal record + the released claim are durable
            return
        except Exception as exc:  # noqa: BLE001 — L3-07: one invoice must not kill the ladder
            # An UNCODED failure inside the step.  Never a fabricated outcome and never a
            # whole-tick abort: the exception is logged with its traceback, the step is named as
            # failed in the tick's result and in a durable error row, and the ladder continues
            # with the next invoice.  The claim stays `pending` (at-most-once: the provider may
            # have seen the message; the next tick names the window in `unfinalized_claims`).
            failure = {
                "invoice_id": invoice_id,
                "number": invoice.number,
                "step": step.key,
                "code": DUNNING_STEP_FAILED_CODE,
                "message": f"{type(exc).__name__}: {exc}",
            }
            logger.exception(
                "dunning: %s for invoice %s failed with an uncoded %s",
                step.key,
                invoice.number,
                type(exc).__name__,
            )
            if errors is not None:
                errors.append(failure)
            # Release this connection's read transaction BEFORE the fresh write below (SQLite
            # would otherwise hold a SHARED lock against the error row's INSERT/COMMIT).
            await db.rollback()
            try:
                async with session_factory() as err_db:
                    await log_activity(
                        err_db,
                        "finance",
                        DUNNING_ERROR_ACTION,
                        summary=(
                            f"Dunning {step.key} for invoice {failure['number']} failed "
                            f"unexpectedly: {failure['message']}"
                        ),
                        level="error",
                        detail={
                            **failure,
                            "superseded": superseded,
                            "claim_state": CLAIM_STATE_PENDING,
                        },
                    )
                    await err_db.commit()
            except Exception:  # noqa: BLE001 — recording the failure must not re-abort the tick
                logger.exception(
                    "dunning: could not record the uncoded failure for invoice %s", invoice_id
                )
            return
        flipped_to: str | None = None
        if step.offset_days >= PAST_DUE_FLIP_AT_DAYS and invoice.status is InvoiceStatus.SENT:
            await transition_invoice(db, invoice, InvoiceStatus.OVERDUE)
            flipped_to = InvoiceStatus.OVERDUE.value
        provider_result = result.get("result")
        message_id = str(provider_result.get("message_id") or "") if isinstance(provider_result, dict) else ""
        # Finalize the claim (state -> sent) …
        claim.detail = {
            **(claim.detail if isinstance(claim.detail, dict) else {}),
            "state": CLAIM_STATE_SENT,
            "message_id": message_id or None,
            "status_flipped_to": flipped_to,
        }
        # … and write the delivered-ledger row byte-compatibly with the V9 pins.
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
    errors: list[dict[str, Any]] = []

    async with session_factory() as db:
        open_ids = await fetch_scalars(
            db,
            select(Invoice.id)
            .where(Invoice.status.in_((InvoiceStatus.SENT, InvoiceStatus.OVERDUE)))
            .order_by(Invoice.id),
            what="open invoices",
        )
        # F5 (V11/W2): claims still `pending` at this tick's start are the signature of a crash
        # window (or a live concurrent tick — the residual V9 records).  They CONSUME their step;
        # naming them here keeps the window visible instead of silently eating a reminder.
        claims = await _claim_rows(db)
        # F16 (V12/W-C): ONE ledger read for the whole tick — it used to run per invoice
        # (O(open_invoices x ledger_rows)); the same map is passed into every `_dun_one`.
        consumed = await _sent_steps(db, claims=claims)
        unfinalized = [
            {
                "invoice_id": invoice_id,
                "number": (row.detail or {}).get("number"),
                "step": step,
                "row_id": int(row.id),
            }
            for invoice_id, by_step in claims.items()
            for step, row in by_step.items()
            if isinstance(row.detail, dict) and row.detail.get("state") == CLAIM_STATE_PENDING
        ]
    for invoice_id in open_ids:
        try:
            await _dun_one(
                session_factory,
                int(invoice_id),
                today,
                sent=sent,
                refused=refused,
                consumed=consumed,
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001 — L3-07: never abort the remaining invoices
            # The send-step guard lives in `_dun_one`; this catches a failure in the re-read or
            # the claim write, which would otherwise end the tick before the later invoices ran.
            logger.exception("dunning: invoice %s failed before its step could be dispatched", invoice_id)
            errors.append(
                {
                    "invoice_id": int(invoice_id),
                    "number": None,
                    "step": None,
                    "code": DUNNING_STEP_FAILED_CODE,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    result: dict[str, Any] = {
        "status": "completed" if (sent or refused or errors) else "no_due",
        "date": today.isoformat(),
        "sent": sent,
        "refused": refused,
        "errors": errors,
        "unfinalized_claims": unfinalized,
        "steps": [step.key for step in DUNNING_STEPS],
    }
    logger.info("dunning ladder: %s", result)
    return result


__all__ = [
    "DUNNING_ACTION",
    "DUNNING_ERROR_ACTION",
    "DUNNING_LOCAL_HOUR",
    "DUNNING_REFUSAL_ACTION",
    "DUNNING_STEPS",
    "DUNNING_STEP_FAILED_CODE",
    "PAST_DUE_FLIP_AT_DAYS",
    "DunningStep",
    "compose_dunning_email",
    "run_dunning_ladder",
    "step_date",
]
