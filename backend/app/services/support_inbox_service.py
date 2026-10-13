"""support_inbox_service — the producer side of the customer-support inbox (R17).

RD12 shipped the consumer: a `customer_support` task carrying `metadata.inbox_item` gets the
body quarantined into its prompt (`app/agents/customer_support/agent.py:64-149` — id/from/
subject/body, reference-only refuses by name, malformed refuses by name).  Nothing produced
such an item.  This service is the ingest that populates it:

* `submit_item`    — the owner (via `POST /api/v1/support/inbox`) or a future IMAP poller
  stores one message.  `provider_message_id` is the unique ingest key: a re-submitted id
  returns the STORED row unchanged (at-least-once delivery must not duplicate, and must
  never overwrite a stored customer message with whatever arrived second).
* `attach_pending` — the 3h sweep's helper (`celery_app/tasks/agent_tasks.py`,
  `_email_inbox_sweep`): oldest-first pending items become one deduped CS task each,
  carrying `metadata.inbox_item` in the RD12 shape; the item is then marked `dispatched`
  with that task id in the SAME transaction as the task row.
* `list_items`     — the owner's read.

Transaction discipline: every function FLUSHES only; the caller commits (the same split the
rest of `app/services` uses — `task_service.create_task` flushes, the sweeps commit).  A
rollback therefore takes back a whole attach, item marks included.

Idempotency, both layers: the unique `provider_message_id` stops a duplicate ITEM;
`support-inbox-<provider_message_id>` on `tasks.dedupe_key` (unique, migration 0002) stops a
duplicate TASK, so a re-run of the sweep cannot attach the same message twice.  A dedupe hit
still marks the item `dispatched` with the EXISTING task id — otherwise an item whose task
row already exists would stay pending forever (every tick would re-hit the same dedupe key).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, dialect_name
from app.models.support import SupportInboxItem
from app.models.task import Task
from app.services.query_helpers import fetch_scalars

logger = logging.getLogger(__name__)

#: The states this table's sweep actually needs (see the model docstring: `served` was
#: rejected — nothing in this tree could ever honestly set it).  The one tuple both the
#: service and the list filter validate against.
VALID_STATUSES: tuple[str, ...] = ("pending", "dispatched")

#: `tasks.dedupe_key` is String(255) and the attach key is `support-inbox-` (14 chars) + the
#: provider id: capping the id at 240 keeps every derived key ≤ 254, so a key can never be
#: truncated into a collision on PostgreSQL.
MAX_PROVIDER_MESSAGE_ID_LEN = 240

#: The columns of `tasks.dedupe_key` rows this service creates — greppable so a reader can
#: find every producer of the shape.
DEDUPE_KEY_PREFIX = "support-inbox-"


def inbox_task_dedupe_key(provider_message_id: str) -> str:
    """`support-inbox-<provider message id>` — the per-ITEM dedupe key (RD17 brief's shape)."""
    return f"{DEDUPE_KEY_PREFIX}{provider_message_id}"


def _require_text(field: str, value: object, *, max_len: int | None = None) -> str:
    """A required non-blank string, named in every refusal.

    WHY stripped for the two IDENTIFIER fields (provider id, from-address) and byte-exact for
    subject/body: whitespace around a pasted id or address is paste noise around an opaque
    token, but a customer's own subject/body is evidence — validation checks `strip()` is
    non-empty, the stored value is what was submitted.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        raise PolsiaError(
            E_VALIDATION,
            f"support inbox item requires a non-empty {field}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": field},
        )
    if max_len is not None and len(text) > max_len:
        raise PolsiaError(
            E_VALIDATION,
            f"support inbox item {field} is {len(text)} characters; the maximum is {max_len}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": field, "length": len(text), "max_len": max_len},
        )
    return text


async def submit_item(
    db: AsyncSession,
    *,
    provider_message_id: str,
    from_address: str,
    subject: str,
    body: str,
    received_at: datetime | None = None,
) -> SupportInboxItem:
    """Store one customer message, pending — or return the EXISTING row for a known id.

    The insert is `ON CONFLICT (provider_message_id) DO NOTHING` (the same idiom
    `task_service.create_task` uses for `dedupe_key`): two racing submitters produce ONE row
    and the loser re-selects the winner's row instead of raising.  The stored body is never
    overwritten — a re-submission of a known provider id is a duplicate DELIVERY, not an edit.

    `received_at` is the submitter's statement of when the message arrived; NULL is "not
    stated" (never invented — the sweep then orders by `created_at`).  A timezone-less value
    is refused: assuming a zone would silently shift the attach order, and refusing is the
    honest answer.
    """
    pid = _require_text(
        "provider_message_id", provider_message_id, max_len=MAX_PROVIDER_MESSAGE_ID_LEN
    )
    from_addr = _require_text("from_address", from_address, max_len=255)
    subj = _require_text("subject", subject, max_len=512)
    text = _require_text("body", body)
    if received_at is not None and received_at.tzinfo is None:
        raise PolsiaError(
            E_VALIDATION,
            "support inbox item received_at must carry a timezone offset (a timezone-less "
            "value would be read as UTC and silently reorder the attach)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "received_at", "received_at": received_at.isoformat()},
        )

    values: dict[str, Any] = {
        "provider_message_id": pid,
        "from_address": from_addr,
        "subject": subj,
        "body": text,
        "status": "pending",
        # dialect-correct bind (the claim path's rule): SQLite stores naive UTC text,
        # PostgreSQL an aware instant.
        "received_at": as_bound_instant(received_at, db) if received_at is not None else None,
    }
    insert_fn = pg_insert if dialect_name(db) == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(SupportInboxItem)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["provider_message_id"])
        .returning(SupportInboxItem)
    )
    created = (await db.execute(stmt)).scalars().one_or_none()
    if created is not None:
        return created

    existing = (
        await db.execute(
            select(SupportInboxItem).where(SupportInboxItem.provider_message_id == pid)
        )
    ).scalar_one_or_none()
    if existing is None:
        # Only reachable if the conflicting row was deleted in between; either way we must
        # not report a fake success (same guard as create_task's dedupe path).
        raise PolsiaError(
            E_VALIDATION,
            f"provider_message_id {pid!r} conflicted but no row could be re-read",
            how_to_fix="retry submit_item; if it persists, check the "
            "support_inbox_items.provider_message_id unique constraint",
            context={"provider_message_id": pid},
        )
    return existing


async def list_items(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SupportInboxItem]:
    """Newest first, index-friendly: `(created_at DESC, id DESC)` + explicit LIMIT/OFFSET.

    An unknown `status` is a named refusal, never an empty list standing in for a typo (the
    same rule `list_tasks`-style reads carry in this codebase).
    """
    if status is not None and status not in VALID_STATUSES:
        raise PolsiaError(
            E_VALIDATION,
            f"unknown support inbox status {status!r}; valid: {', '.join(VALID_STATUSES)}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"status": status, "valid": list(VALID_STATUSES)},
        )
    stmt = select(SupportInboxItem)
    if status is not None:
        stmt = stmt.where(SupportInboxItem.status == status)
    stmt = (
        stmt.order_by(SupportInboxItem.created_at.desc(), SupportInboxItem.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return await fetch_scalars(db, stmt, what="support inbox items")


async def attach_pending(db: AsyncSession) -> list[dict[str, Any]]:
    """Attach every pending item to its own deduped CS task; mark it dispatched. Flush-only.

    Oldest first (`coalesce(received_at, created_at), id` — deterministic on both dialects).
    For each item, ONE `customer_support` task is created under the per-item dedupe key,
    carrying exactly the RD12 payload shape:

        metadata = {"inbox_item": {"id": <provider id>, "from": ..., "subject": ...,
                                   "body": ...}}

    Whether the task was created now or already existed (a dedupe hit — e.g. a concurrent
    sweep won the insert), the item is marked `dispatched` with that task's id via a
    conditional UPDATE (`WHERE status='pending'`): a lost race can therefore never overwrite
    another attach's mark, and a mark only happens for a task that really exists.

    No validation is duplicated here for the item's fields: `submit_item` is the only writer
    and the CONSUMER's gates are the single authority for what a runnable item looks like —
    a row written around this service fails loudly in the agent (by name), never silently.
    """
    # lazy: app.* must not import the worker package at module scope (the tree's convention —
    # see app/api/v1/agents.py:77); _common imports nothing from celery itself.
    from celery_app.tasks._common import create_task_deduped

    pending = list(
        (
            await db.execute(
                select(SupportInboxItem)
                .where(SupportInboxItem.status == "pending")
                .order_by(
                    func.coalesce(SupportInboxItem.received_at, SupportInboxItem.created_at).asc(),
                    SupportInboxItem.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    attached: list[dict[str, Any]] = []
    for item in pending:
        key = inbox_task_dedupe_key(item.provider_message_id)
        task = await create_task_deduped(
            db,
            title=f"Reply to support email from {item.from_address}",
            agent_type="customer_support",
            dedupe_key=key,
            priority=3,
            source="scheduler",
            metadata={
                "inbox_item": {
                    "id": item.provider_message_id,
                    "from": item.from_address,
                    "subject": item.subject,
                    "body": item.body,
                }
            },
        )
        if task is not None:
            task_id, task_created = int(task.id), True
        else:
            # Dedupe hit: the task for this item already exists (None is "already created",
            # never a failure).  Resolve its id so the item can terminate pending-ness with a
            # real task reference instead of being re-attempted forever.
            existing_id = await db.scalar(select(Task.id).where(Task.dedupe_key == key).limit(1))
            if existing_id is None:
                raise PolsiaError(
                    E_VALIDATION,
                    f"dedupe key {key!r} conflicted but no task row could be re-read",
                    how_to_fix="retry attach_pending; if it persists, check ix_tasks_dedupe_key",
                    context={"dedupe_key": key},
                )
            task_id, task_created = int(existing_id), False

        marked = (
            await db.execute(
                update(SupportInboxItem)
                .where(SupportInboxItem.id == item.id, SupportInboxItem.status == "pending")
                .values(status="dispatched", task_id=task_id)
                .returning(SupportInboxItem.id)
                .execution_options(synchronize_session=False)
            )
        ).scalars().all()
        if not marked:
            # A concurrent attach marked this item between our read and this write; the task
            # row is (by the dedupe key) the same one, and reporting it as attached-here
            # would claim work this call did not do.
            logger.info("support inbox item %s was attached concurrently; nothing to mark", item.id)
            continue
        # Mirror the marked transition on the in-session object: the UPDATE runs with
        # synchronize_session=False (the task_service convention), so without this a caller
        # reading `item.status` in this same session would still see the stale 'pending'.
        item.status = "dispatched"
        item.task_id = task_id
        attached.append(
            {
                "item_id": int(item.id),
                "provider_message_id": item.provider_message_id,
                "task_id": task_id,
                "task_created": task_created,
            }
        )
    return attached


__all__ = [
    "DEDUPE_KEY_PREFIX",
    "MAX_PROVIDER_MESSAGE_ID_LEN",
    "VALID_STATUSES",
    "attach_pending",
    "inbox_task_dedupe_key",
    "list_items",
    "submit_item",
]
