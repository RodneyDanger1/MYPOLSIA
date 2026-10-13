"""Prospects / email campaigns.

`prospects.email` is UNIQUE in the schema, and the original `create_prospect` let
the `IntegrityError` escape as a 500.  A duplicate import is a *validation* answer
(`E_VALIDATION` with the address in the context), not an internal error — the
caller can act on it.  Reads are newest-first on `(created_at DESC, id DESC)`.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utcnow
from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import dialect_name
from app.models.outreach import EmailCampaign, EmailLog, Prospect
from app.services.query_helpers import db_refusal, fetch_scalar, fetch_scalars

logger = logging.getLogger(__name__)


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
    """Insert a prospect; a duplicate address is a named refusal, not a 500."""
    address = (email or "").strip()
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


async def list_campaigns(db: AsyncSession, limit: int = 50) -> list[EmailCampaign]:
    """Newest-first `email_campaigns` read.

    ZERO PRODUCTION CALLERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the only callers are
    `tests/unit/test_r27_social_outreach_sweep.py`.  NOTE the wired namesake: what
    `GET /api/v1/ads` serves is `ads_service.list_campaigns` (ad campaigns) — this function
    reads `email_campaigns` and no code path calls it.  Intended purpose: an outreach-campaign
    list read for a campaigns page.  Removal vs adoption is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §4).
    """
    return await fetch_scalars(
        db,
        select(EmailCampaign).order_by(EmailCampaign.id.desc()).limit(limit),
        what="email campaigns",
    )


async def list_email_logs(db: AsyncSession, limit: int = 100) -> list[EmailLog]:
    """Newest-first send-log read.

    ZERO PRODUCTION CALLERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — the only callers are
    `tests/unit/test_r27_social_outreach_sweep.py`; the shipped email path is the agent ->
    worker -> `record_sent_emails` chain and no route serves the send log today.  Intended
    purpose: the outreach send-log read.  Removal vs adoption is an owner decision
    (polsia-work/build/full-sweep/fix-5/REQUESTS.md §4).
    """
    return await fetch_scalars(
        db,
        select(EmailLog).order_by(EmailLog.id.desc()).limit(limit),
        what="email logs",
    )


async def _find_or_create_prospect(db: AsyncSession, address: str) -> tuple[Prospect, bool]:
    """`(prospect, created)` for an email address — SELECT first, then create.

    The create attempt runs inside `db.begin_nested()` ON PURPOSE: a duplicate trips
    `create_prospect`'s flush, and without a savepoint that failed flush rolls back the whole
    session transaction and poisons it — the very re-select this function needs then dies with
    `PendingRollbackError` (measured, see the S2 seat's raw/probe_dup_prospect.py). The savepoint
    scopes the rollback to the failed insert, the caller's pending work survives, and the
    duplicate refusal is re-resolved by re-selecting. Anything other than an `already exists`
    refusal is re-raised — never swallowed. `flush` only; the worker owns the commit.
    """
    existing = await fetch_scalar(
        db, select(Prospect).where(Prospect.email == address), what=f"prospect {address!r}"
    )
    if existing is not None:
        return existing, False
    try:
        async with db.begin_nested():
            return await create_prospect(db, address), True
    except PolsiaError as exc:
        if exc.code == E_VALIDATION and "already exists" in exc.message:
            again = await fetch_scalar(
                db, select(Prospect).where(Prospect.email == address), what=f"prospect {address!r}"
            )
            if again is not None:
                return again, False
        raise


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
            created_id = (
                await db.execute(
                    insert_fn(EmailLog)
                    .values(
                        prospect_id=prospect.id,
                        subject=str(item.get("subject") or ""),
                        body=str(item.get("body") or ""),
                        sendgrid_id=sendgrid_id,
                        status="sent",
                        sent_at=utcnow(),
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


__all__ = [
    "create_prospect",
    "list_campaigns",
    "list_email_logs",
    "list_prospects",
    "record_sent_emails",
]
