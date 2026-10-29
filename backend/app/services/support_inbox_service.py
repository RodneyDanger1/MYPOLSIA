"""support_inbox_service — the producer side of the customer-support inbox (R17).

RD12 shipped the consumer: a `customer_support` task carrying `metadata.inbox_item` gets the
body quarantined into its prompt (`app/agents/customer_support/agent.py:64-149` — id/from/
subject/body, reference-only refuses by name, malformed refuses by name).  Nothing produced
such an item.  This service is the ingest that populates it:

* `submit_item`    — the owner (via `POST /api/v1/support/inbox`) stores one message.
  `provider_message_id` is the unique ingest key: a re-submitted id returns the STORED row
  unchanged (at-least-once delivery must not duplicate, and must never overwrite a stored
  customer message with whatever arrived second).  R4 OUTREACH-3 (V9/B3): the ingest ALSO
  stamps `replied_at` on the matching prospect's unstamped `email_logs` rows
  (`stamp_prospect_replied` below) — one reply stops the follow-up chain, because the every-3h
  sweep (`celery_app/tasks/followups.py`) filters on that column.
* `ingest_inbound_item` — W5: the INBOUND-EMAIL PROVIDER's path (`POST
  /api/v1/support/inbox/inbound`).  It is `submit_item` plus the three things a webhook needs and
  an owner POST does not: the provider's body/size cap (an unauthenticated-by-the-owner payload is
  caller input), an HONEST `created` flag (a re-delivery answers `created=False` without writing a
  row) and ONE audit row per delivery under the stable action `support_inbox_inbound`.  The item
  it writes is byte-for-byte the owner path's row, so the shipped attach sweep consumes both
  identically.
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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, dialect_name, now_utc
from app.models.outreach import EmailLog, Prospect
from app.models.support import SupportInboxItem
from app.models.task import Task
from app.services.activity_service import log_activity
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

#: W5 — the inbound-email webhook's INPUT CAPS.  Every field the webhook accepts is bounded and
#: refused BY NAME (`E_VALIDATION` + `context.field`), never truncated — a truncated customer
#: message is altered evidence.  `provider_message_id`/`from_address`/`subject` reuse
#: `submit_item`'s bounds; `body` deliberately had NO bound on the owner path (an authenticated
#: owner may paste what they like), so the provider path carries the explicit cap below.
MAX_INBOUND_BODY_CHARS = 65_536
MAX_INBOUND_PROVIDER_CHARS = 64

#: The ONE stable `activity_log.action` every inbound delivery writes (greppable by an operator,
#: pinned by `tests/unit/test_w5_inbound.py`).  The agent_type says who wrote it: the provider's
#: webhook is SYSTEM bookkeeping, not an agent's action.
INBOUND_AUDIT_ACTION = "support_inbox_inbound"
INBOUND_AUDIT_AGENT = "system"

#: WAVE 7 — the stable action a reply-driven opt-out writes (the footer's reply route honored:
#: see `email_compliance.is_opt_out_reply`).  IDs and flags only, never the customer's text.
OPT_OUT_AUDIT_ACTION = "support_inbox_opt_out"


@dataclass(frozen=True, slots=True)
class InboundIngestResult:
    """What one inbound delivery produced: the stored row, whether it is NEW, the provider tag.

    `created` is exact for every sequential delivery (including the shipped at-least-once retry
    case).  The concurrency guarantee is the DATABASE's, not this flag's: two racing FIRST
    deliveries of one id produce exactly ONE row (the unique key + `ON CONFLICT DO NOTHING` in
    `submit_item`), and both callers return that row's id; the loser may still report
    `created=True` because no deployed dialect here offers a portable "did MY insert win" answer
    for a no-op insert.  A delivery of an id that already exists always reports `created=False`.
    """

    item: SupportInboxItem
    created: bool
    provider: str | None

#: The ANSWER window (V8/C5, code-gap §10.2): the support function only ANSWERS mail received
#: in the last 14 days (dossier §11 line 128, verbatim).  The bound is enforced at the ATTACH
#: path (`attach_pending`) — NOT at submit — because a submit-time refusal cannot bound a
#: later dispatch (a stored, in-window item may sit pending through budget denials and only
#: be attached days later).  Expired items are never dropped: they stay `pending`, the owner
#: read keeps showing them, and the 3h sweep reports the named skip with this bound stated.
ANSWER_WINDOW_DAYS = 14


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


async def stamp_prospect_replied(
    db: AsyncSession, *, from_address: str, now: datetime | None = None
) -> dict[str, Any]:
    """R4 OUTREACH-3 (V9/B3): an ingested message FROM a stored prospect is a REPLY — stamp it.

    One answer stops the whole chain: EVERY `email_logs` row of the matched prospect that has
    not been stamped yet gets `replied_at` = this instant — the exact column the every-3h
    follow-up sweep filters on (`replied_at IS NULL` in
    `celery_app/tasks/followups.py`), so no future follow-up for that prospect can fire.
    Rows already stamped keep their original instant: a second reply is not a new stop, and
    re-stamping would rewrite WHEN the chain actually stopped.

    Matching is a case-insensitive compare on the full address (`prospects.email` is stored
    as given; mailboxes differ only by case in practice).  No match is an ANSWER, not an
    error — an inbound message from a non-prospect is simply not a chain event, and the
    report says so: `{"matched": False, "prospect_id": None, "stamped": 0}`.  Flush only —
    the caller commits (this service's transaction discipline).

    WAVE 7 — LEGACY CASE-VARIANTS.  The old lookup took `.limit(1)` of the case-insensitive
    match, so two rows for one mailbox (`Ada@Example.COM` beside `ada@example.com`, which the
    case-sensitive unique index allowed before `outreach_service.normalize_address` landed) left
    one of them unstamped: the reply stopped half a chain.  Every matching prospect is stamped
    now, and the report names them — `prospect_ids` is present only when there was more than one,
    so the long-standing three-key shape of a normal reply is unchanged.
    """
    address = (from_address or "").strip().lower()
    if not address:
        return {"matched": False, "prospect_id": None, "stamped": 0}
    prospect_ids = (
        await db.execute(
            select(Prospect.id)
            .where(func.lower(Prospect.email) == address)
            .order_by(Prospect.id.asc())
        )
    ).scalars().all()
    if not prospect_ids:
        return {"matched": False, "prospect_id": None, "stamped": 0}
    stamped_ids = (
        await db.execute(
            update(EmailLog)
            .where(EmailLog.prospect_id.in_(list(prospect_ids)), EmailLog.replied_at.is_(None))
            .values(replied_at=as_bound_instant(now or now_utc(), db))
            .returning(EmailLog.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars().all()
    logger.info(
        "reply from prospect %s: %d email log(s) stamped replied_at — the follow-up chain stops",
        address, len(stamped_ids),
    )
    report: dict[str, Any] = {
        "matched": True,
        "prospect_id": int(prospect_ids[0]),
        "stamped": len(stamped_ids),
    }
    if len(prospect_ids) > 1:
        report["prospect_ids"] = [int(pid) for pid in prospect_ids]
    return report


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
        # V9/B3 (R4 OUTREACH-3): a message FROM a stored prospect is a REPLY — stamp its chain
        # in the SAME transaction as the ingest, so `replied_at` can never lag the reply it
        # reports (the every-3h follow-up sweep filters on that column).  A duplicate delivery
        # (the `existing` branch below) never re-stamps: it is not a new reply.
        reply = await stamp_prospect_replied(db, from_address=from_addr)
        if reply.get("matched"):
            # WAVE 7 — the footer's reply-to-opt-out route (`email_compliance.OPT_OUT_LINE`)
            # was a PROMISE with no executable half: the reply stopped this chain but stored no
            # suppression, so the next campaign mailed the same person again.  A reply that
            # exercises the route now records the durable opt-out through the ONE producer
            # (`outreach_service.unsubscribe_prospect`), in this same transaction, and the
            # event is audited with IDs and flags only — never the customer's text.
            # Lazy imports: this module loads in the worker/beat path and must not drag the
            # compliance module (and through it `app.agents`) into that import graph.
            from app.services.email_compliance import is_opt_out_reply

            if is_opt_out_reply(subj, text):
                from app.services.outreach_service import unsubscribe_prospect

                opt_out = await unsubscribe_prospect(db, from_addr)
                await log_activity(
                    db,
                    INBOUND_AUDIT_AGENT,
                    OPT_OUT_AUDIT_ACTION,
                    summary=(
                        f"support inbox: reply from prospect {reply.get('prospect_id')} "
                        "requested opt-out; suppression recorded"
                    ),
                    detail={
                        "prospect_id": reply.get("prospect_id"),
                        "item_id": int(created.id),
                        "stamped_logs": reply.get("stamped", 0),
                        "was": opt_out.get("was"),
                        "already": bool(opt_out.get("already")),
                    },
                )
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


async def ingest_inbound_item(
    db: AsyncSession,
    *,
    provider_message_id: str,
    from_address: str,
    subject: str,
    body: str,
    received_at: datetime | None = None,
    provider: str | None = None,
) -> InboundIngestResult:
    """Ingest ONE inbound-email-provider delivery (W5) — `submit_item` + cap + created flag + audit.

    This is the webhook's service path, NOT a second writer: the row is written by `submit_item`
    exactly as the owner's POST writes it (`status='pending'`, same fields, same
    `ON CONFLICT (provider_message_id) DO NOTHING` idempotency, same prospect-reply stamping), so
    the shipped attach sweep (`attach_pending`) consumes provider mail and owner mail identically.

    Differences from the owner path, each for a stated reason:

    * the provider payload is CALLER input the owner did not type, so `body` and `provider` are
      bounded and refused BY NAME (`E_VALIDATION` + `context.field`) before any write;
    * a re-delivery must answer `created=False` and write NOTHING new — the unique row is the
      guarantee (see `InboundIngestResult`), and the pre-read below is what makes the flag honest
      for every non-concurrent delivery;
    * ONE `activity_log` row per delivery under `INBOUND_AUDIT_ACTION`, written through
      `log_activity` in the SAME transaction as the item: a rolled-back item can never leave an
      audit row claiming it landed, and a landed item can never be un-audited.  The row carries
      IDs and flags only — never the customer body and never the shared secret.

    Flush-only, like every other function here: the caller (the route's `get_db`) commits.
    """
    provider_tag = (provider or "").strip() or None
    if provider_tag is not None and len(provider_tag) > MAX_INBOUND_PROVIDER_CHARS:
        raise PolsiaError(
            E_VALIDATION,
            f"inbound support payload provider is {len(provider_tag)} characters; the maximum "
            f"is {MAX_INBOUND_PROVIDER_CHARS}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={
                "field": "provider",
                "length": len(provider_tag),
                "max_len": MAX_INBOUND_PROVIDER_CHARS,
            },
        )
    if isinstance(body, str) and len(body) > MAX_INBOUND_BODY_CHARS:
        raise PolsiaError(
            E_VALIDATION,
            f"inbound support payload body is {len(body)} characters; the maximum is "
            f"{MAX_INBOUND_BODY_CHARS}.  The message is refused rather than truncated: a clipped "
            "customer email would be altered evidence.",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "body", "length": len(body), "max_len": MAX_INBOUND_BODY_CHARS},
        )

    # Pre-read for the honest `created` flag.  `submit_item` stays the single writer: it re-reads
    # the conflicting row itself, so a delivery of a known id below still answers that stored row
    # and never overwrites it.
    known_id = (provider_message_id or "").strip()
    existing_id: int | None = None
    if known_id:
        existing_id = await db.scalar(
            select(SupportInboxItem.id)
            .where(SupportInboxItem.provider_message_id == known_id)
            .limit(1)
        )

    item = await submit_item(
        db,
        provider_message_id=provider_message_id,
        from_address=from_address,
        subject=subject,
        body=body,
        received_at=received_at,
    )
    created = existing_id is None

    await log_activity(
        db,
        INBOUND_AUDIT_AGENT,
        INBOUND_AUDIT_ACTION,
        summary=(
            f"support inbox: inbound message stored as item {item.id}"
            if created
            else f"support inbox: duplicate inbound delivery for item {item.id} (no new row)"
        ),
        detail={
            "item_id": int(item.id),
            "provider_message_id": item.provider_message_id,
            "provider": provider_tag,
            "created": created,
            "status": item.status,
        },
    )
    return InboundIngestResult(item=item, created=created, provider=provider_tag)


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


async def attach_pending(db: AsyncSession, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Attach every pending item to its own deduped CS task; mark it dispatched. Flush-only.

    Oldest first (`coalesce(received_at, created_at), id` — deterministic on both dialects).
    V8/C5 (code-gap §10.2): only items INSIDE the `ANSWER_WINDOW_DAYS` window are attached —
    the documented rule is "it only answers mail received in the last 14 days", enforced at
    the ATTACH (answer) path, because a submit-time refusal could not bound a later dispatch.
    Expired items are never attached, never marked, never dropped: they stay `pending`, the
    owner read still shows them, and the sweep reports the named skip via
    `count_expired_pending` (`older_than_14_days`, bound stated).  `now` is injectable for
    deterministic tests; production passes nothing (the wall clock).
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

    arrived = func.coalesce(SupportInboxItem.received_at, SupportInboxItem.created_at)
    cutoff = as_bound_instant((now or now_utc()) - timedelta(days=ANSWER_WINDOW_DAYS), db)
    pending = list(
        (
            await db.execute(
                select(SupportInboxItem)
                .where(SupportInboxItem.status == "pending", arrived >= cutoff)
                .order_by(
                    arrived.asc(),
                    SupportInboxItem.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    # V24/L6 — ONE dedupe prefetch for the whole batch.  The old loop called
    # `create_task_deduped` per item and, on every dedupe hit, a second SELECT for the existing
    # id: a sweep over N already-attached items issued 2N SELECTs (measured: 9 SELECTs for 4
    # items, `pre/red_run_1.txt`).  The prefetch answers the same question in one statement;
    # `create_task_deduped` is still called for genuinely-new items, so its savepoint still
    # protects a concurrent insert race, and the post-race re-read below stays as the last
    # resort for a key that appeared between the prefetch and the insert.
    keys = {item.provider_message_id: inbox_task_dedupe_key(item.provider_message_id) for item in pending}
    existing_ids: dict[str, int] = {}
    if keys:
        rows = await db.execute(
            select(Task.dedupe_key, Task.id).where(Task.dedupe_key.in_(list(keys.values())))
        )
        existing_ids = {str(key): int(task_id) for key, task_id in rows.all() if key is not None}

    attached: list[dict[str, Any]] = []
    for item in pending:
        key = keys[item.provider_message_id]
        if key in existing_ids:
            task_id, task_created = existing_ids[key], False
            logger.info("dedupe hit: %s already exists (task %s)", key, task_id)
        else:
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
                existing_ids[key] = task_id   # a later duplicate key in this batch re-uses it
            else:
                # Lost a race with a concurrent sweep between the prefetch and this insert:
                # resolve the winner's id so the item can terminate pending-ness with a real
                # task reference instead of being re-attempted forever.
                existing_id = await db.scalar(select(Task.id).where(Task.dedupe_key == key).limit(1))
                if existing_id is None:
                    raise PolsiaError(
                        E_VALIDATION,
                        f"dedupe key {key!r} conflicted but no task row could be re-read",
                        how_to_fix="retry attach_pending; if it persists, check ix_tasks_dedupe_key",
                        context={"dedupe_key": key},
                    )
                task_id, task_created = int(existing_id), False
                existing_ids[key] = task_id

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


async def count_expired_pending(db: AsyncSession, *, now: datetime | None = None) -> int:
    """How many PENDING items are older than the answer window (V8/C5, code-gap §10.2).

    The named-skip half of the bound: `attach_pending` refuses to attach these, and the 3h
    sweep (`celery_app/tasks/agent_tasks._email_inbox_sweep`) reports this count in its
    result as `expired = {"count": n, "bound_days": 14, "reason": "older_than_14_days"}` —
    the skip is named, bounded and countable, and nothing is lost (items stay pending).
    """
    arrived = func.coalesce(SupportInboxItem.received_at, SupportInboxItem.created_at)
    cutoff = as_bound_instant((now or now_utc()) - timedelta(days=ANSWER_WINDOW_DAYS), db)
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(SupportInboxItem)
                .where(SupportInboxItem.status == "pending", arrived < cutoff)
            )
        ).scalar()
        or 0
    )


__all__ = [
    "ANSWER_WINDOW_DAYS",
    "DEDUPE_KEY_PREFIX",
    "INBOUND_AUDIT_ACTION",
    "INBOUND_AUDIT_AGENT",
    "MAX_INBOUND_BODY_CHARS",
    "MAX_INBOUND_PROVIDER_CHARS",
    "MAX_PROVIDER_MESSAGE_ID_LEN",
    "OPT_OUT_AUDIT_ACTION",
    "VALID_STATUSES",
    "InboundIngestResult",
    "attach_pending",
    "count_expired_pending",
    "ingest_inbound_item",
    "inbox_task_dedupe_key",
    "list_items",
    "stamp_prospect_replied",
    "submit_item",
]
