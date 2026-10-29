"""Prospects / email campaigns.

`prospects.email` is UNIQUE in the schema, and the original `create_prospect` let
the `IntegrityError` escape as a 500.  A duplicate import is a *validation* answer
(`E_VALIDATION` with the address in the context), not an internal error — the
caller can act on it.  Reads are newest-first on `(created_at DESC, id DESC)`.

T13/D20 removals: `list_campaigns` and `list_email_logs` were labelled zero-callers (fix-5 §4).
`list_campaigns` read `email_campaigns`, which NO surface serves — the wired namesake is
`ads_service.list_campaigns` behind `GET /api/v1/ads`, and keeping a same-named second reader
for a different table is exactly the confusion the removal ends.  `list_email_logs` was a
send-log read with no route/UI; the shipped email write path (agent -> worker ->
`record_sent_emails`) stays, and the send log remains readable through the rows it writes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, dialect_name
from app.models.outreach import EmailLog, Prospect
from app.models.support import SupportInboxItem
from app.services.email_lifecycle import (
    PROVIDER_ACCEPTED_MAX,
    PROVIDER_ACCEPTED_MIN,
    coerce_status_code,
)
from app.services.query_helpers import db_refusal, fetch_scalar, fetch_scalars

logger = logging.getLogger(__name__)


def normalize_address(address: object) -> str:
    """The ONE stored form of an address: `strip().lower()` (V11/W1's opt-out rule, widened).

    WHY every writer normalises: `prospects.email`'s unique index is CASE-SENSITIVE, so
    `Ada@Example.COM` and `ada@example.com` used to become two rows for ONE mailbox — one
    subscribed, one unsubscribed.  That split the dedupe (a re-send created a second prospect)
    and the suppression list (`send_compliant_email` reads it with `lower(email) = …` and took
    the FIRST match, so a send to the other case could miss the opt-out).  The opt-out producer
    already stored `strip().lower()`; the send path did not.  One mailbox is one consent.
    """
    return str(address or "").strip().lower()


async def list_prospects(
    db: AsyncSession,
    status: str | None = None,
    limit: int = 100,
) -> list[Prospect]:
    stmt = select(Prospect)
    if status is not None:
        stmt = stmt.where(Prospect.status == status)
    stmt = stmt.order_by(Prospect.created_at.desc(), Prospect.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="prospects")


async def create_prospect(db: AsyncSession, email: str, **values: Any) -> Prospect:
    """Insert a prospect; a duplicate address is a named refusal, not a 500.

    The address is STORED normalised (`normalize_address`): one mailbox, one row — otherwise the
    case-sensitive unique index lets a second casing in and the suppression list stops covering
    the person (see `normalize_address`).
    """
    address = normalize_address(email)
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise PolsiaError(
            E_VALIDATION,
            f"prospect email {email!r} is not a usable address",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"email": email},
        )
    prospect = Prospect(email=address, **values)
    db.add(prospect)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise PolsiaError(
            E_VALIDATION,
            f"prospect {address} already exists",
            how_to_fix="re-use the existing prospect row (prospects.email is unique) "
            "or update its status instead of re-inserting",
            context={"email": address},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - translated below
        raise db_refusal("prospect insert", exc) from exc
    return prospect


async def _find_or_create_prospect(db: AsyncSession, address: str) -> tuple[Prospect, bool]:
    """`(prospect, created)` for an email address — SELECT first, then create.

    The create attempt runs inside `db.begin_nested()` ON PURPOSE: a duplicate trips
    `create_prospect`'s flush, and without a savepoint that failed flush rolls back the whole
    session transaction and poisons it — the very re-select this function needs then dies with
    `PendingRollbackError` (measured, see the S2 seat's raw/probe_dup_prospect.py). The savepoint
    scopes the rollback to the failed insert, the caller's pending work survives, and the
    duplicate refusal is re-resolved by re-selecting. Anything other than an `already exists`
    refusal is re-raised — never swallowed. `flush` only; the worker owns the commit.

    WAVE 7: the lookup is CASE-INSENSITIVE and ordered by id, and the row is created stored
    normalised — so a legacy mixed-case row is FOUND rather than duplicated, and the
    suppression check `send_compliant_email` runs has one row to answer from.
    """
    normalized = normalize_address(address)
    lookup = (
        select(Prospect)
        .where(func.lower(Prospect.email) == normalized)
        .order_by(Prospect.id.asc())
        .limit(1)
    )
    existing = await fetch_scalar(db, lookup, what=f"prospect {normalized!r}")
    if existing is not None:
        return existing, False
    try:
        async with db.begin_nested():
            return await create_prospect(db, normalized), True
    except PolsiaError as exc:
        if exc.code == E_VALIDATION and "already exists" in exc.message:
            again = await fetch_scalar(db, lookup, what=f"prospect {normalized!r}")
            if again is not None:
                return again, False
        raise


def _follow_up_due_at(
    result: dict[str, Any], item: dict[str, Any], sent_at: datetime, db: AsyncSession
) -> datetime | None:
    """`sent_at` + the earliest due_in_days this send's prospect states — or None.

    V9/B3: the output's `follow_ups[]` (`schemas.FollowUpDraft`) is the chain — one entry per
    planned touch, each with `due_in_days` measured AFTER THE SEND.  The earliest entry for
    THIS prospect is the touch the every-3h sweep must fire first, so it becomes
    `email_logs.follow_up_due`.  A prospect with no entry — or an entry whose `due_in_days`
    will not parse as a positive int — states no chain here: NULL.  The column is only ever
    written from a stated number, and a malformed one is a named warning, not a guess.
    """
    address = str(item.get("prospect_email") or "").strip().lower()
    if not address:
        return None
    due_days: list[int] = []
    for entry in (result or {}).get("follow_ups") or []:
        if isinstance(entry, dict):
            entry_address = str(entry.get("prospect_email") or "").strip().lower()
            raw = entry.get("due_in_days")
        else:  # a Pydantic `FollowUpDraft` handed in by a direct caller
            entry_address = str(getattr(entry, "prospect_email", "") or "").strip().lower()
            raw = getattr(entry, "due_in_days", None)
        if entry_address != address:
            continue
        if raw is None:
            # absent due_in_days is not a stated cadence (the FollowUpDraft schema requires
            # it; a raw dict can still omit it) — nothing to bind, nothing to warn about
            continue
        try:
            days = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "code=follow_up_due_unparseable: prospect=%s due_in_days=%r (chain ignored)",
                address, raw,
            )
            continue
        if days > 0:
            due_days.append(days)
    if not due_days:
        return None
    # Dialect-correct bind (the claim path's rule): SQLite compares naive UTC text, PostgreSQL
    # an aware instant — the sweep reads the column with the same convention (`as_bound_instant`).
    return as_bound_instant(sent_at + timedelta(days=min(due_days)), db)


async def record_sent_emails(db: AsyncSession, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the emails an email_outreach run REALLY sent — the worker calls this after `run()`.

    WHY this lives here: agents have no DB session, and only the worker can write the
    `email_logs` rows that `channel_service` counts the email caps against (A2 F-04). Only items
    with `status == "sent"` AND a non-empty `sendgrid_id` are recorded — anything else was
    refused, drafted or failed, and a log row for it would fake a send. `prospect_id` is a
    required FK, so the prospect is found (or created) first. Deduped by `sendgrid_id` so a
    retried run cannot double-count — and since migration 0005 that key is a REAL partial unique
    index, so two writers that both miss the SELECT cannot both insert: the write is one
    `INSERT ... ON CONFLICT DO NOTHING RETURNING id` (the `record_stripe_event` shape) whose
    empty RETURNING means the raced row already exists and is counted as `duplicates_skipped`.
    `flush` only — the worker owns the commit. Never raises: any failure comes back in the
    report's `error` field.

    RD30 (M5): a performed send whose `prospect_email` cannot become a prospect row (no usable
    address) is ITEM-SCOPED — it is skipped with a named warning and a `skipped_malformed` entry
    in the report (present only when there is something to list) and the batch CONTINUES, so the
    performed sends after it are still counted. The refusal fires before any flush, so the
    session is provably healthy. Every other item failure (E_DB, a refused read, the
    vanished-duplicate race) stays batch-fatal: a session that may be poisoned must not be
    driven on. When at least one item was skipped the report is `ok=False` with the first
    skip's named error — visible in the run output and the worker's existing warning path.

    V9/B3 (follow-up chain): a row this send writes also carries `follow_up_due` = `sent_at` +
    the EARLIEST `due_in_days` the output's `follow_ups[]` states for its prospect — the number
    the every-3h follow-up sweep (`celery_app/tasks/followups.py`) reads.  No stated chain ->
    NULL, never a guessed date.

    WAVE 7 (two additions, both additive to the report):

    * the PROVIDER-ACCEPTANCE gate — an item that claims `status="sent"` while stating a
      `provider_status` outside 2xx is skipped BY NAME (`rejected_provider_status`) and never
      becomes a row, so the ledger cannot hold a send the provider rejected;
    * the FOUND-PROSPECTS leg — when the output states `prospects[]` (it always does for a real
      email_outreach run), the people the run found are persisted too, idempotently.  Before this,
      an `action="find"` run wrote NOTHING (only a sent email created a prospect as a side
      effect), so the agent's first documented duty was a lost write.  Counts ride
      `found_prospects_written` / `found_prospects_existing`, and an unusable address is a named
      `skipped_found_prospects` entry (never a silent drop, never a poisoned session).
    """
    report: dict[str, Any] = {
        "ok": True,
        "emails_written": 0,
        "prospects_created": 0,
        "duplicates_skipped": 0,
        "error": None,
    }
    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    try:
        for index, item in enumerate((result or {}).get("emails") or []):
            if not isinstance(item, dict):
                continue
            sendgrid_id = str(item.get("sendgrid_id") or "").strip()
            if str(item.get("status") or "") != "sent" or not sendgrid_id:
                # Not a performed send (draft / failed / no provider id): nothing to record.
                continue
            # WAVE 7 — the provider-acceptance gate.  A "sent" claim that CONTRADICTS the
            # provider's own stated answer (a 4xx/5xx) is not a send this writer may count: the
            # row is skipped BY NAME instead of being written, so a pre-fix (or replayed) output
            # that said "sent" over a rejected call cannot fabricate a counted send.  The
            # agent already refuses to claim "sent" in that case; this is the second lock, on
            # the ledger's own writer.
            stated_status = coerce_status_code(item.get("provider_status"))
            if stated_status is not None and not (
                PROVIDER_ACCEPTED_MIN <= stated_status <= PROVIDER_ACCEPTED_MAX
            ):
                logger.warning(
                    "code=email_log_skipped_provider_rejected: item %d (sendgrid_id=%s) claims "
                    "'sent' but the provider answered HTTP %s — no row written",
                    index, sendgrid_id, stated_status,
                )
                report.setdefault("rejected_provider_status", []).append({
                    "index": index,
                    "prospect_email": normalize_address(item.get("prospect_email")),
                    "sendgrid_id": sendgrid_id,
                    "provider_status": stated_status,
                })
                if report["ok"]:                   # the first skip names the report's error
                    report["ok"] = False
                    report["error"] = {
                        "code": E_VALIDATION,
                        "message": (
                            f"send {sendgrid_id!r} claims status 'sent' but the provider "
                            f"answered HTTP {stated_status}; the send is not counted"
                        ),
                    }
                continue
            existing = await fetch_scalar(
                db,
                select(EmailLog.id).where(EmailLog.sendgrid_id == sendgrid_id),
                what=f"email log {sendgrid_id}",
            )
            if existing is not None:
                report["duplicates_skipped"] += 1
                continue
            address = str(item.get("prospect_email") or "").strip()
            try:
                prospect, created = await _find_or_create_prospect(db, address)
            except PolsiaError as exc:
                if exc.code != E_VALIDATION or "is not a usable address" not in exc.message:
                    # E_DB / a refused read / the vanished-duplicate race: batch-fatal, as ever —
                    # a session that may be poisoned must not be driven on.
                    raise
                # RD30 (M5): item-scoped continuation. `email_logs.prospect_id` is a required FK
                # and this address cannot become a prospect, so there is no honest row for the
                # item — but the refusal fires BEFORE any flush (the session is provably
                # healthy), and aborting here would permanently undercount every performed send
                # after it. Skip BY NAME; the later valid rows still count (one commit, as ever).
                logger.warning(
                    "code=email_log_skipped_malformed_address: item %d (sendgrid_id=%s) cannot "
                    "be linked to a prospect: %s",
                    index, sendgrid_id, exc.message,
                )
                report.setdefault("skipped_malformed", []).append({
                    "index": index,
                    "prospect_email": address,
                    "sendgrid_id": sendgrid_id,
                    "code": exc.code,
                    "message": exc.message,
                })
                if report["ok"]:                   # the first skip names the report's error
                    report["ok"] = False
                    report["error"] = {"code": exc.code, "message": exc.message}
                continue
            if created:
                report["prospects_created"] += 1
            # ONE statement (the `record_stripe_event` shape): the natural key is a REAL
            # partial unique index (migration 0005), so a concurrent writer that missed the
            # SELECT above cannot double-insert — the conflict resolves atomically in the
            # database and `RETURNING id` comes back empty.  No savepoint: a savepoint released
            # as the OUTERMOST transaction commits on SQLite (pysqlite legacy control), which
            # would break the "flush only — the worker owns the commit" contract.
            sent_at = utcnow()
            # R2 (wave 7): a fast reply/auto-responder can be ingested BEFORE this send is
            # recorded. The log is then born with replied_at NULL and a follow_up_due, and the 3h
            # sweep mints a follow-up for someone who already answered. Consult the inbox for this
            # address at WRITE time: an already-arrived reply stamps the row and suppresses the chain.
            inbound = await fetch_scalar(
                db,
                select(SupportInboxItem)
                .where(func.lower(SupportInboxItem.from_address) == normalize_address(address))
                .order_by(SupportInboxItem.id.desc())
                .limit(1),
                what=f"inbound reply for {address!r}",
            )
            if inbound is not None:
                replied_at = inbound.received_at or inbound.created_at or sent_at
                follow_up_due = None
            else:
                replied_at = None
                follow_up_due = _follow_up_due_at(result, item, sent_at, db)
            created_id = (
                await db.execute(
                    insert_fn(EmailLog)
                    .values(
                        prospect_id=prospect.id,
                        subject=str(item.get("subject") or ""),
                        body=str(item.get("body") or ""),
                        sendgrid_id=sendgrid_id,
                        status="sent",
                        sent_at=sent_at,
                        replied_at=replied_at,
                        follow_up_due=follow_up_due,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["sendgrid_id"],
                        index_where=text("sendgrid_id IS NOT NULL"),
                    )
                    .returning(EmailLog.id)
                )
            ).scalars().one_or_none()
            if created_id is None:
                # The raced row is the natural key's row: it IS this send, recorded once.
                report["duplicates_skipped"] += 1
                continue
            report["emails_written"] += 1

        found = (result or {}).get("prospects")
        if isinstance(found, list):
            # WAVE 7 — the LOST WRITE.  A find run's prospects were never persisted: only the
            # side effect of a SENT email created a prospect row, so an `action="find"` run (the
            # outreach agent's first documented duty) wrote NOTHING and the outreach page's "the
            # outreach agent finds prospects when it runs" copy was false.  Persist what the run
            # really found, idempotently: the lookup is case-insensitive and the row is stored
            # normalised, so a re-persisted run writes no duplicate; an existing row is never
            # overwritten (the owner may have corrected it).
            written = 0
            existing_found = 0
            skipped_found: list[dict[str, Any]] = []
            for found_index, found_item in enumerate(found):
                if not isinstance(found_item, dict):
                    continue
                raw_address = str(found_item.get("email") or "")
                try:
                    prospect, created = await _find_or_create_prospect(db, raw_address)
                except PolsiaError as exc:
                    if exc.code != E_VALIDATION or "is not a usable address" not in exc.message:
                        raise
                    logger.warning(
                        "code=found_prospect_skipped_malformed_address: item %d cannot become a "
                        "prospect row: %s",
                        found_index, exc.message,
                    )
                    skipped_found.append({
                        "index": found_index,
                        "email": raw_address,
                        "code": exc.code,
                        "message": exc.message,
                    })
                    continue
                if not created:
                    existing_found += 1
                    continue
                for field_name in ("first_name", "last_name", "company", "title", "source"):
                    value = found_item.get(field_name)
                    if isinstance(value, str) and value.strip():
                        setattr(prospect, field_name, value.strip())
                written += 1
            report["found_prospects_written"] = written
            report["found_prospects_existing"] = existing_found
            if skipped_found:
                report["skipped_found_prospects"] = skipped_found
                if report["ok"]:                   # the first skip names the report's error
                    report["ok"] = False
                    report["error"] = {
                        "code": E_VALIDATION,
                        "message": skipped_found[0]["message"],
                    }
    except PolsiaError as exc:
        report["ok"] = False
        report["error"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 — the worker contract is a report, never a raise
        refusal = PolsiaError(
            E_DB,
            f"could not persist sent emails: {type(exc).__name__}: {exc}",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"agent": "email_outreach", "emails_written": report["emails_written"]},
        )
        report["ok"] = False
        report["error"] = {"code": refusal.code, "message": refusal.message}
    return report


async def compliance_preflight(db: AsyncSession) -> dict[str, Any]:
    """The compliance preflight an `email_outreach` run must carry (V11/W1 — the `channel_sends` shape).

    WHY here: the agent holds no DB session, so the sender identity (the 0008 trio) and the
    suppression state can only be resolved by the layer that owns the session.  The worker calls
    this and puts the result on the run context under `compliance_send.PREFLIGHT_KEY`; the agent
    then sends in the session-free mode of `send_compliant_email`.

    Contains no secret: the identity trio is public mail metadata, and the suppression list is a set
    of addresses the company must NOT mail.  `footer_for_company` runs first so the E_CONFIG refusal
    (naming the missing fields) fires HERE, on the worker's session, with its own how_to_fix —
    before any email is composed.
    """
    from app.services.company_service import get_company
    from app.services.email_compliance import footer_for_company

    company = await get_company(db)
    footer_for_company(company)          # the identity gate, by name (E_CONFIG + missing fields)
    suppressed = await fetch_scalars(
        db,
        select(Prospect.email).where(func.lower(Prospect.status) == "unsubscribed"),
        what="suppressed prospect addresses",
    )
    return {
        # R1 (wave 7): this preflight is a SNAPSHOT resolved once per run. The session-free send
        # refuses unless the producer asserts a send-time suppression verification for THIS send;
        # the worker adds that assertion at dispatch (agent_tasks._with_compliance_preflight). A
        # bare preflight therefore fails closed; a per-SEND refresh is the wave-8 carry.
        "send_time_verified": False,
        "company": {
            "sender_legal_name": str(getattr(company, "sender_legal_name", "") or ""),
            "sender_postal_address": str(getattr(company, "sender_postal_address", "") or ""),
            "website_url": str(getattr(company, "website_url", "") or ""),
        },
        "suppressed": sorted({str(address).strip().lower() for address in suppressed if address}),
    }


async def unsubscribe_prospect(db: AsyncSession, address: str) -> dict[str, Any]:
    """Record an opt-out: `prospects.status = "unsubscribed"`, durably (V11/W1 — audit B10).

    The ONE producer of the state `email_compliance.send_compliant_email` refuses to send to.  It
    works for an address whose row does not exist yet — `record_sent_emails` creates prospects as a
    side effect of sends, so an opt-out can arrive while a send is still in flight — and refuses for
    an address that cannot be one at all (E_VALIDATION, never a silent no-op).  `flush` only: the
    caller commits, like every other writer in this module.

    The address is stored `strip().lower()` — the SAME normalisation `record_sent_emails` and the
    owner's `prospect_service.normalize_email` apply.  Storing the recipient's typed case made
    `Ada@Example.COM` a second `prospects` row beside `ada@example.com` (the unique index is
    case-sensitive), i.e. one person listed twice with one row subscribed and one not.
    """
    normalized = (address or "").strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise PolsiaError(
            E_VALIDATION,
            f"cannot record an opt-out for {address!r}: it is not a usable address",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"address": address},
        )
    prospect = await fetch_scalar(
        db,
        select(Prospect).where(func.lower(Prospect.email) == normalized.lower()),
        what=f"prospect {normalized!r}",
    )
    created = prospect is None
    was = None if prospect is None else prospect.status
    if prospect is None:
        prospect = Prospect(email=normalized, status="unsubscribed", source="unsubscribe-link")
        db.add(prospect)
    else:
        prospect.status = "unsubscribed"
    await db.flush()
    return {
        "address": prospect.email,
        "status": "unsubscribed",
        "created": created,
        "was": was,
        "already": was == "unsubscribed",
    }


__all__ = [
    "compliance_preflight",
    "create_prospect",
    "list_prospects",
    "normalize_address",
    "record_sent_emails",
    "unsubscribe_prospect",
]
