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
* `ingest_sendgrid_inbound` — WAVE 9 (SV-W7-01 / SV-W8-04): the TWILIO SENDGRID INBOUND PARSE
  adapter.  The W5 route above is a JSON webhook, and Inbound Parse is not one: the vendor
  delivers `multipart/form-data`, carries **no** `provider_message_id` field, and authenticates
  with an ECDSA signature over the raw bytes (or an OAuth bearer token) — never the shared
  secret.  This section parses that real delivery (`parse_multipart_form`), verifies the
  credential (`verify_sendgrid_signature` / `verify_sendgrid_oauth_token`), derives the item's
  provider id from the message's OWN identity (`derive_sendgrid_provider_message_id`) and then
  writes the row through the ONE writer (`ingest_inbound_item` -> `submit_item`).  Every failure
  is named: the provider's retry loop treats a 5xx as "try again for days", so an unverifiable or
  malformed delivery is a 4xx with the offending field named.
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

import base64
import binascii
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy as email_policy
from email.message import Message
from email.parser import BytesParser, Parser
from email.utils import parseaddr
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_AUTH, E_VALIDATION, ERROR_HINTS, MeridianError
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
        raise MeridianError(
            E_VALIDATION,
            f"support inbox item requires a non-empty {field}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": field},
        )
    if max_len is not None and len(text) > max_len:
        raise MeridianError(
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
        raise MeridianError(
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
        raise MeridianError(
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
    delivery_auth: str | None = None,
    body_source: str | None = None,
    provider_id_source: str | None = None,
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

    WAVE 9 — `delivery_auth` / `body_source` / `provider_id_source` are OPTIONAL provenance flags
    the SendGrid Inbound Parse adapter adds to the ONE audit row: HOW the delivery authenticated
    (`signature` / `oauth` / `shared_secret`), WHICH field supplied the stored body (`text` /
    `html` / `email`) and WHICH identity rule produced the provider id (`message-id` / `digest`).
    Flags, never content: the customer body and every credential stay out of the ledger, and the
    shipped JSON path passes none of them, so its audit row is byte-identical to before.
    """
    provider_tag = (provider or "").strip() or None
    if provider_tag is not None and len(provider_tag) > MAX_INBOUND_PROVIDER_CHARS:
        raise MeridianError(
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
        raise MeridianError(
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
            # WAVE 9 provenance — present only for the SendGrid Inbound Parse adapter.
            **({"delivery_auth": delivery_auth} if delivery_auth else {}),
            **({"body_source": body_source} if body_source else {}),
            **({"provider_id_source": provider_id_source} if provider_id_source else {}),
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
        raise MeridianError(
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
                    raise MeridianError(
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


# ── Twilio SendGrid Inbound Parse (WAVE 9 · SV-W7-01 / SV-W8-04) ─────────────────────────────
# Twilio's own pages (fetched 2026-10-29; `dateModified` 2026-10-08 "Configure the inbound parse
# webhook" and 2026-10-07 "Securing your Inbound Parse Webhooks") are the contract implemented
# here:
#
#   * the delivery is `Content-Type: multipart/form-data` whose named fields are `headers`, `dkim`,
#     `to`, `from`, `subject`, `text`, `html`, `sender_ip`, `envelope`, `attachments`, `charsets`,
#     `SPF`, `spam_score`, `spam_report`, `content-ids`, `attachment-info`, `attachment{N}` — or,
#     with "POST the raw, full MIME message" enabled, a single `email` field holding the message;
#   * there is **no** `message_id` field: a `Message-ID:` header exists only inside `headers` (or
#     the `email` blob), which is why the id below is DERIVED from the message's own identity;
#   * the credential is either the ECDSA signature of `timestamp || raw body` in
#     `X-Twilio-Email-Event-Webhook-Signature` (+ `-Timestamp`), verified with the security
#     policy's public key, or an OAuth `Authorization: Bearer` token — or both.  "Do not parse or
#     modify the request body before validating the signature … use the raw request body exactly
#     as it was received", which is why the route hands this module the RAW bytes and every field
#     below is decoded AFTER the signature is checked.

#: Stable provider tag for the audit row / the `provider` answer (≤ MAX_INBOUND_PROVIDER_CHARS).
SENDGRID_INBOUND_PROVIDER = "sendgrid-inbound-parse"

#: The vendor's signed-webhook headers.  The signature is base64 of a DER/ASN.1 ECDSA signature
#: over `sha256(timestamp_bytes + raw_body_bytes)`; the vendored Go sample in the docs un-marshals
#: the ASN.1 (r,s) pair first, and `cryptography`'s `ec.ECDSA` performs exactly that hash+verify.
SENDGRID_SIGNATURE_HEADER = "X-Twilio-Email-Event-Webhook-Signature"
SENDGRID_TIMESTAMP_HEADER = "X-Twilio-Email-Event-Webhook-Timestamp"

#: The vendor documents no replay tolerance for the signed header (its Go/PHP/Node samples verify
#: the signature only).  A window is a product hardening on top of the vendor contract: the
#: Message-ID dedupe already makes a replay a no-op, this refuses the replay EARLIER.  A
#: non-positive config falls back to this default instead of meaning "off" (the Stripe rule,
#: SV-W7-05).
SENDGRID_DEFAULT_SIGNATURE_TOLERANCE_SECONDS = 300

#: The multipart envelope cap.  The vendor's own limit is 30 MB for message+attachments; nginx
#: already refuses >10 MiB on this location (`nginx/nginx.conf:171`), so 10 MiB is the bound the
#: backend can actually honour (and it is the same number an operator sees in the proxy).  The
#: cap exists because the attachment bytes are DISCARDED: only the message text is evidence here.
MAX_INBOUND_MULTIPART_BYTES = 10 * 1024 * 1024

#: Prefix of a DIGEST-derived provider id (no `Message-ID` header on the message).
SENDGRID_DIGEST_PREFIX = "sendgrid-parse-sha256:"

#: The form fields the DIGEST may depend on, in a FIXED order and length-prefixed (so
#: `("text","html")` cannot collide with `("tex","thtml")`).  `attachment*`, `charsets`,
#: `spam_score`, `spam_report`, `SPF`, `sender_ip` and `attachments` are deliberately EXCLUDED:
#: they are provider metadata that a retry may recompute (a fresh spam score must not turn one
#: message into two rows), while these fields are the message's own content and headers.
_SENDGRID_DIGEST_FIELDS: tuple[str, ...] = (
    "from", "to", "subject", "dkim", "headers", "text", "html", "email", "envelope",
)

#: The multipart parser is the one SendGrid documents and Python ships: `email` (RFC 2046/MIME).
#: No new dependency is introduced — `python-multipart` is a declared requirement but the local
#: venv resolves the legacy `multipart` shim while the image resolves `python_multipart`, and this
#: path must behave identically in both.
_FORM_DATA_POLICY = email_policy.default


@dataclass(frozen=True, slots=True)
class SendGridInboundItem:
    """One parsed Inbound Parse delivery, before it is written.

    `body_source` and `provider_id_source` are the provenance the audit row records: WHICH field
    supplied the stored body and WHICH identity rule produced `provider_message_id`.  `received_at`
    is always None: the vendor sends no arrival instant (the `Date:` header is the SENDER's clock,
    and inventing an arrival time would reorder the attach sweep).
    """

    provider_message_id: str
    from_address: str
    subject: str
    body: str
    body_source: str
    provider_id_source: str
    provider: str = SENDGRID_INBOUND_PROVIDER
    received_at: datetime | None = None


def media_type_of(content_type: str | None) -> str:
    """`"multipart/form-data; boundary=…"` -> `"multipart/form-data"` (lower-cased, trimmed)."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_multipart_form_data(content_type: str | None) -> bool:
    """Is this delivery SendGrid's (and RFC 7578's) multipart form encoding?"""
    return media_type_of(content_type) == "multipart/form-data"


def _multipart_boundary(content_type: str) -> str:
    """The declared boundary, or a NAMED refusal (a form without one cannot be read at all)."""
    message = Message()
    message["Content-Type"] = content_type
    boundary = message.get_param("boundary")
    if not isinstance(boundary, str) or not boundary.strip():
        raise MeridianError(
            E_VALIDATION,
            "the multipart/form-data delivery declares no boundary parameter in its "
            "Content-Type, so its form fields cannot be separated",
            how_to_fix=(
                "send the delivery exactly as the provider does: Content-Type: "
                'multipart/form-data; boundary="<the boundary used in the body>"'
            ),
            context={"field": "payload", "content_type": media_type_of(content_type)},
        )
    return boundary.strip()


def parse_multipart_form(raw_body: bytes, content_type: str) -> dict[str, bytes]:
    """Every named, non-file form field of a `multipart/form-data` delivery — as RAW BYTES.

    Bytes, not `str`: the same fields are used for the provider id digest (`bytes` is what a retry
    reproduces exactly) and for the item text (decoded afterwards with the field's own charset).
    File parts are dropped BY NAME (`Content-Disposition: …; filename=…`): the support inbox stores
    the customer's message, and an attachment is not part of `support_inbox_items`' schema.

    Every failure is a named `E_VALIDATION` carrying `context.field = "payload"` — an unreadable
    delivery must never surface as a 5xx (that is the provider's retry loop) and must never be
    silently stored as an empty message.
    """
    if len(raw_body) > MAX_INBOUND_MULTIPART_BYTES:
        raise MeridianError(
            E_VALIDATION,
            f"the multipart/form-data delivery is {len(raw_body)} bytes; the maximum is "
            f"{MAX_INBOUND_MULTIPART_BYTES}.  The message is refused rather than truncated: a "
            "clipped customer email would be altered evidence.",
            how_to_fix=(
                "send a smaller message (the proxy already refuses >10 MiB on this location); "
                "attachments are not stored, so the message TEXT is what matters"
            ),
            context={"field": "payload", "max_bytes": MAX_INBOUND_MULTIPART_BYTES,
                     "length": len(raw_body)},
        )
    boundary = _multipart_boundary(content_type)
    if boundary.encode("latin-1", "replace") not in raw_body:
        raise MeridianError(
            E_VALIDATION,
            "the multipart/form-data body contains no part for its declared boundary, so it is "
            "not a multipart delivery (a body altered before the signature was checked looks "
            "exactly like this)",
            how_to_fix="verify the signature over the raw bytes first, then parse; do not rewrite "
            "the body in between",
            context={"field": "payload", "reason": "boundary_not_found"},
        )
    try:
        parsed = BytesParser(policy=_FORM_DATA_POLICY).parsebytes(
            b"Content-Type: "
            + content_type.encode("latin-1", "replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw_body
        )
    except Exception as exc:  # noqa: BLE001 — a hostile body must become a refusal, not a 500
        raise MeridianError(
            E_VALIDATION,
            f"the multipart/form-data delivery could not be parsed ({type(exc).__name__})",
            how_to_fix="send the delivery exactly as the provider does (raw bytes, unmodified)",
            context={"field": "payload", "reason": "unparsable"},
        ) from exc
    if not parsed.is_multipart():
        raise MeridianError(
            E_VALIDATION,
            "the multipart/form-data delivery produced no MIME parts",
            how_to_fix="send the delivery exactly as the provider does (raw bytes, unmodified)",
            context={"field": "payload", "reason": "no_parts"},
        )

    fields: dict[str, bytes] = {}
    for part in parsed.walk():
        if part.is_multipart():
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name.strip():
            continue  # an unnamed part is not a documented field — nothing to attribute
        if part.get_filename() is not None:
            continue  # an attachment: kept out of the item (the body is the evidence)
        try:
            fields[name] = _part_payload_bytes(part)
        except Exception as exc:  # noqa: BLE001 — see above: refuse by name, never a 500
            raise MeridianError(
                E_VALIDATION,
                f"the multipart/form-data field {name!r} could not be decoded "
                f"({type(exc).__name__})",
                how_to_fix="send the field as text (SendGrid sends text fields unencoded)",
                context={"field": "payload", "part": name},
            ) from exc
    if not fields:
        raise MeridianError(
            E_VALIDATION,
            "the multipart/form-data delivery carried no named form fields",
            how_to_fix="send the fields SendGrid documents (headers/from/subject/text …)",
            context={"field": "payload", "reason": "no_named_fields"},
        )
    return fields


def _sendgrid_charsets(fields: dict[str, bytes]) -> dict[str, str]:
    """The provider's own `charsets` map: `{"text": "UTF-8", …}`; anything unusable is ignored.

    This map is METADATA about how the strings were encoded, not evidence: an unreadable value
    falls back to UTF-8 rather than refusing a real customer message.  The strict decode happens
    per field below, so a wrong charset still refuses BY NAME (`context.field`) instead of
    silently replacing bytes.
    """
    raw = fields.get("charsets")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(name): value
        for name, value in parsed.items()
        if isinstance(value, str) and value.strip()
    }


def _decode_multipart_text(
    fields: dict[str, bytes],
    name: str,
    charsets: dict[str, str],
    *,
    field: str,
) -> str:
    """Decode one form field with its declared charset (strict); `""` when the field is absent."""
    raw = fields.get(name)
    if raw is None:
        return ""
    charset = (charsets.get(name) or "utf-8").strip()
    try:
        return raw.decode(charset, "strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise MeridianError(
            E_VALIDATION,
            f"the multipart/form-data field {name!r} could not be decoded as {charset!r}; the "
            "message is refused rather than stored with replaced characters",
            how_to_fix="set the field's charset correctly in the provider's `charsets` map "
            "(or send it as UTF-8)",
            context={"field": field, "part": name, "charset": charset},
        ) from exc


def _sendgrid_headers_message(fields: dict[str, bytes], charsets: dict[str, str]) -> Message | None:
    """The delivery's header block as a parsed `Message` (Default format: the `headers` field)."""
    raw = fields.get("headers")
    if not raw:
        return None
    text = _decode_multipart_text(fields, "headers", charsets, field="headers")
    return Parser(policy=_FORM_DATA_POLICY).parsestr(text, headersonly=True)


def _sendgrid_mime_message(fields: dict[str, bytes]) -> Message | None:
    """The delivery's full MIME message (Raw format: the `email` field), parsed from BYTES."""
    raw = fields.get("email")
    if not raw:
        return None
    try:
        return BytesParser(policy=_FORM_DATA_POLICY).parsebytes(raw)
    except Exception as exc:  # noqa: BLE001 — a hostile MIME blob becomes a refusal, not a 500
        raise MeridianError(
            E_VALIDATION,
            f"the raw-format `email` field could not be parsed as a MIME message "
            f"({type(exc).__name__})",
            how_to_fix="send the raw format only with SendGrid's 'POST the raw, full MIME "
            "message' option, which delivers the message unmodified",
            context={"field": "payload", "reason": "mime_unparsable"},
        ) from exc


def _message_header(message: Message | None, name: str) -> str:
    """One header value, unfolded; `""` when absent (never a guess)."""
    if message is None:
        return ""
    value = message.get(name)
    return str(value).strip() if value is not None else ""


def _message_id_candidate(fields: dict[str, bytes], charsets: dict[str, str]) -> str:
    """`Message-ID` from the `headers` block, else from the raw `email` message; `""` if neither."""
    candidate = _message_header(
        _sendgrid_headers_message(fields, charsets), "Message-ID"
    ) or _message_header(_sendgrid_mime_message(fields), "Message-ID")
    # A Message-ID is an opaque ASCII token: RFC 5322 forbids whitespace inside it, so folded
    # headers (invalid but real) are normalised to the token rather than stored with the folds.
    return "".join(candidate.split()).strip("<>").strip()


def canonical_sendgrid_digest(fields: dict[str, bytes]) -> str:
    """The boundary-independent identity digest used when the message carries no `Message-ID`.

    Deterministic in the message's own content and headers, and INDEPENDENT of everything a
    provider retry may regenerate: the MIME boundary (not part of `fields` at all), the attachment
    bytes/names, the spam score, the charsets map.  Length-prefixed per field so two adjacent
    fields cannot be re-split into the same input.  Bounded below `MAX_PROVIDER_MESSAGE_ID_LEN`.
    """
    hasher = hashlib.sha256()
    for name in _SENDGRID_DIGEST_FIELDS:
        value = fields.get(name, b"")
        hasher.update(name.encode("ascii") + b"\x00")
        hasher.update(str(len(value)).encode("ascii") + b"\x00")
        hasher.update(value)
    return SENDGRID_DIGEST_PREFIX + hasher.hexdigest()


def derive_sendgrid_provider_message_id(
    fields: dict[str, bytes], charsets: dict[str, str] | None = None
) -> tuple[str, str]:
    """The item's provider id + the rule that produced it (`"message-id"` / `"digest"`).

    WHY a derivation at all: Inbound Parse has no `message_id` field (measured on the vendor's own
    payload table), so the id the whole consumer contract is keyed on has to come from the message.
    Order, each anchored on something the provider sends:

    1. the message's own `Message-ID` header — an RFC 5322 globally-unique id, reproduced by every
       retry of the same message; used verbatim (minus the angle brackets) so an operator can grep
       the mail logs for it.  A `Message-ID` longer than `MAX_PROVIDER_MESSAGE_ID_LEN` falls
       through to the digest (the column bound wins, and the digest still contains it);
    2. otherwise `canonical_sendgrid_digest` — a sha256 over the message's identifying fields.
    """
    candidate = _message_id_candidate(fields, charsets or {})
    if candidate and len(candidate) <= MAX_PROVIDER_MESSAGE_ID_LEN:
        return candidate, "message-id"
    return canonical_sendgrid_digest(fields), "digest"


def _sendgrid_address(value: str) -> str:
    """The bare addr-spec of a `From:` value (`"Name <a@b.c>"` -> `"a@b.c"`), or `""`.

    The prospect-reply stamping compares `prospects.email` against this value
    (`stamp_prospect_replied`), so a display name would silently match nothing.  A value without
    an `@` is not an address at all — it is DROPPED here so the envelope's return path can be
    tried, instead of storing a non-address as if it were one.
    """
    _display, address = parseaddr(value or "")
    address = (address or "").strip()
    return address if "@" in address else ""


def _sendgrid_envelope_from(fields: dict[str, bytes], charsets: dict[str, str]) -> str:
    """`envelope.from` — the SMTP return path; `""` when absent or unreadable (metadata)."""
    raw = fields.get("envelope")
    if not raw:
        return ""
    try:
        parsed = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    value = parsed.get("from")
    return str(value).strip() if isinstance(value, str) else ""


def _part_payload_bytes(part: Message) -> bytes:
    """One MIME leaf part's decoded payload as BYTES (`b""` when it carries no byte payload).

    `Message.get_payload(decode=True)` is typed `Message | bytes | Any`: for a leaf part it is the
    decoded bytes, for a container it is a list of sub-parts (never reached here — callers skip
    `is_multipart()`).  This is the ONE place that narrows it, so no caller has to `type: ignore`.
    """
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _sendgrid_mime_body(fields: dict[str, bytes]) -> str:
    """The first `text/plain` part of the raw `email` message, else its first `text/html`; `""`.

    Attachments are skipped by their `Content-Disposition`/filename, so a `.txt` attachment is
    never mistaken for the message body.
    """
    message = _sendgrid_mime_message(fields)
    if message is None:
        return ""
    plain = ""
    markup = ""
    for part in message.walk():
        if part.is_multipart() or part.get_filename() is not None:
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            text = _part_payload_bytes(part).decode(
                part.get_content_charset() or "utf-8", "strict"
            )
        except (LookupError, UnicodeDecodeError):
            continue  # one undecodable part must not mask a decodable sibling
        if content_type == "text/plain" and not plain:
            plain = text
        elif content_type == "text/html" and not markup:
            markup = text
    return plain or markup


def build_sendgrid_inbound_item(fields: dict[str, bytes]) -> SendGridInboundItem:
    """Turn parsed multipart fields into the item the ONE writer stores (`submit_item`).

    Field precedence is stated, not guessed:
      * body   — `text`, then `html` (an HTML-only customer mail is still the message), then the
                 raw MIME message's own text part (`send_raw` mode).  All three absent/blank is a
                 named `E_VALIDATION` on `body`: an empty inbox row would pretend to be a message;
      * from   — the `from` field's addr-spec, then every `From:` header's addr-spec (the
                 `headers` block, then the raw MIME message), then `envelope.from` — the SMTP
                 RETURN PATH, used only as a last resort because it is SendGrid's bounce address,
                 not the sender a customer sees.  Nothing usable is a named refusal on
                 `from_address` (the prospect-reply stamping compares a real address, never
                 display text);
      * subject— the `subject` field, then the `Subject:` header; still blank is a named refusal
                 on `subject` (the inbox is customer email: a subject-less delivery is named, not
                 invented into "(no subject)");
      * id     — `derive_sendgrid_provider_message_id` (Message-ID, else the digest);
      * arrival— never invented (the vendor sends no arrival instant).
    """
    charsets = _sendgrid_charsets(fields)
    headers = _sendgrid_headers_message(fields, charsets)
    mime = _sendgrid_mime_message(fields)

    subject = _decode_multipart_text(fields, "subject", charsets, field="subject")
    if not subject.strip():
        subject = _message_header(headers, "Subject") or _message_header(mime, "Subject")
    if not subject.strip():
        raise MeridianError(
            E_VALIDATION,
            "the inbound delivery carries no subject: neither the `subject` form field nor a "
            "`Subject:` header is present, so the item would have to invent one",
            how_to_fix="send the delivery exactly as the provider does; a message with no Subject "
            "header is refused by name rather than stored as a fabricated subject",
            context={"field": "subject"},
        )

    from_address = _sendgrid_address(
        _decode_multipart_text(fields, "from", charsets, field="from_address")
    )
    if not from_address:
        from_address = _sendgrid_address(_message_header(headers, "From"))
    if not from_address:
        from_address = _sendgrid_address(_message_header(mime, "From"))
    if not from_address:
        # LAST resort: `envelope.from` is the SMTP RETURN PATH (SendGrid's bounce address), not
        # the displayed sender — it is a real address, but only used when nothing names the From.
        from_address = _sendgrid_address(_sendgrid_envelope_from(fields, charsets))
    if not from_address:
        raise MeridianError(
            E_VALIDATION,
            "the inbound delivery carries no usable sender address: the `from` field, every "
            "`From:` header and `envelope.from` are absent or not an addr-spec",
            how_to_fix="send the delivery exactly as the provider does (the `from` field carries "
            "the message's From header)",
            context={"field": "from_address"},
        )

    body = _decode_multipart_text(fields, "text", charsets, field="body")
    body_source = "text"
    if not body.strip():
        body = _decode_multipart_text(fields, "html", charsets, field="body")
        body_source = "html"
    if not body.strip():
        body = _sendgrid_mime_body(fields)
        body_source = "email"
    if not body.strip():
        raise MeridianError(
            E_VALIDATION,
            "the inbound delivery carries no message body: `text`, `html` and the raw `email` "
            "message are all absent or blank",
            how_to_fix="send the delivery exactly as the provider does (`text` and/or `html`, or "
            "the raw MIME message)",
            context={"field": "body"},
        )

    provider_message_id, provider_id_source = derive_sendgrid_provider_message_id(fields, charsets)
    return SendGridInboundItem(
        provider_message_id=provider_message_id,
        from_address=from_address,
        subject=subject,
        body=body,
        body_source=body_source,
        provider_id_source=provider_id_source,
    )


def _load_sendgrid_public_key(material: str) -> ec.EllipticCurvePublicKey:
    """The configured public key: base64 DER SPKI (what SendGrid returns) or PEM — or a refusal."""
    try:
        if material.startswith("-----BEGIN"):
            loaded = serialization.load_pem_public_key(material.encode("ascii"))
        else:
            loaded = serialization.load_der_public_key(
                base64.b64decode(material.encode("ascii"), validate=True)
            )
    except (ValueError, TypeError, binascii.Error, UnicodeEncodeError) as exc:
        raise MeridianError(
            E_AUTH,
            "SENDGRID_INBOUND_PUBLIC_KEY is not a loadable public key: expected the security "
            "policy's base64-encoded DER SubjectPublicKeyInfo (the `MFkw…` value SendGrid "
            "returns) or a PEM public key",
            how_to_fix="copy the `signature.public_key` value from the Parse webhook security "
            "policy response into SENDGRID_INBOUND_PUBLIC_KEY (or convert it to PEM)",
            context={"setting": "SENDGRID_INBOUND_PUBLIC_KEY", "reason": "unloadable_key"},
        ) from exc
    if not isinstance(loaded, ec.EllipticCurvePublicKey):
        raise MeridianError(
            E_AUTH,
            "SENDGRID_INBOUND_PUBLIC_KEY is not an EC (ECDSA) public key; the signed Inbound "
            "Parse webhook is ECDSA",
            how_to_fix="use the public key from the webhook security policy — do not paste an "
            "RSA key here (it cannot verify an ECDSA signature)",
            context={"setting": "SENDGRID_INBOUND_PUBLIC_KEY", "reason": "not_ec_key"},
        )
    return loaded


def verify_sendgrid_signature(
    raw_body: bytes,
    *,
    signature: str | None,
    timestamp: str | None,
    public_key: str,
    tolerance_seconds: int | None = None,
    now: datetime | None = None,
) -> None:
    """Verify SendGrid's ECDSA signature over the RAW request bytes — or refuse by name.

    The vendor's own verification recipe (its docs, "Verify the signature"): base64-decode the
    signature (ASN.1 DER), take `sha256(timestamp_bytes + payload_bytes)`, and verify with the
    public key.  `cryptography`'s `ec.ECDSA(hashes.SHA256())` performs exactly that over
    `timestamp + raw_body`; the RAW body is required because re-serialising a parsed body changes
    bytes the signature covers (the docs warn about this explicitly).

    Every failure is `E_AUTH` 401 (never a 5xx): the credential is missing or wrong, and a 5xx here
    would make the provider retry a delivery that can never become valid.  Neither the signature,
    the timestamp nor any part of the body is echoed into the refusal.

    `tolerance_seconds` is the product's replay window (`SENDGRID_DEFAULT_SIGNATURE_TOLERANCE_
    SECONDS` when unset or non-positive); `now` is injectable for deterministic tests.
    """
    key_material = (public_key or "").strip()
    if not key_material:
        raise MeridianError(
            E_AUTH,
            "the SendGrid Inbound Parse signature cannot be verified: SENDGRID_INBOUND_PUBLIC_KEY "
            "is unset, so no delivery can be authenticated",
            how_to_fix="create a Parse webhook security policy with `signature: {enabled: true}` "
            "and copy its `signature.public_key` value into SENDGRID_INBOUND_PUBLIC_KEY, then "
            "restart the API",
            context={"setting": "SENDGRID_INBOUND_PUBLIC_KEY", "reason": "unconfigured"},
        )
    if not signature or not timestamp:
        raise MeridianError(
            E_AUTH,
            f"the signed webhook headers are incomplete: both {SENDGRID_SIGNATURE_HEADER} and "
            f"{SENDGRID_TIMESTAMP_HEADER} are required",
            how_to_fix="attach a security policy with signature verification enabled to the Parse "
            "webhook; SendGrid then sends both headers on every delivery",
            context={
                "headers": [SENDGRID_SIGNATURE_HEADER, SENDGRID_TIMESTAMP_HEADER],
                "reason": "headers_incomplete",
            },
        )
    stamp = timestamp.strip()
    try:
        stamp_epoch = int(stamp)
    except ValueError as exc:
        raise MeridianError(
            E_AUTH,
            f"{SENDGRID_TIMESTAMP_HEADER} is not an integer epoch timestamp",
            how_to_fix="send the delivery exactly as SendGrid does; do not rewrite the timestamp "
            "header (it is part of the signed bytes)",
            context={"header": SENDGRID_TIMESTAMP_HEADER, "reason": "bad_timestamp"},
        ) from exc
    window = (
        tolerance_seconds
        if isinstance(tolerance_seconds, int) and tolerance_seconds > 0
        else SENDGRID_DEFAULT_SIGNATURE_TOLERANCE_SECONDS
    )
    skew = abs(int((now or now_utc()).timestamp()) - stamp_epoch)
    if skew > window:
        raise MeridianError(
            E_AUTH,
            f"the signed timestamp is {skew}s away from this server's clock, outside the "
            f"{window}s replay window",
            how_to_fix="check the server clock (NTP) and SENDGRID_INBOUND_SIGNATURE_TOLERANCE_"
            "SECONDS; a captured request replayed later is refused here on purpose",
            context={"reason": "timestamp_outside_window", "skew_seconds": skew,
                     "tolerance_seconds": window},
        )
    try:
        signature_bytes = base64.b64decode((signature or "").strip().encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise MeridianError(
            E_AUTH,
            f"{SENDGRID_SIGNATURE_HEADER} is not base64-encoded DER, so it cannot be a SendGrid "
            "signature",
            how_to_fix="send the delivery exactly as SendGrid does; do not re-encode the signature"
            " header",
            context={"header": SENDGRID_SIGNATURE_HEADER, "reason": "bad_base64"},
        ) from exc
    public = _load_sendgrid_public_key(key_material)
    try:
        public.verify(
            signature_bytes,
            stamp.encode("ascii") + raw_body,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError) as exc:
        raise MeridianError(
            E_AUTH,
            "the SendGrid signature does not verify for this request: the raw body (or the "
            "timestamp) differs from what was signed, or the configured public key is another "
            "policy's",
            how_to_fix="verify the signature over the RAW bytes before any parsing; if the body is "
            "byte-identical, re-copy SENDGRID_INBOUND_PUBLIC_KEY from the same security policy "
            "the webhook uses",
            context={"reason": "signature_mismatch"},
        ) from exc


def verify_sendgrid_oauth_token(*, authorization: str | None, oauth_token: str) -> None:
    """Check SendGrid's OAuth bearer token — or refuse by name (RFC 6750 / the vendor's rules).

    SendGrid obtains the token from the OWNER's authorization server; this deployment holds the
    expected value (`SENDGRID_INBOUND_OAUTH_TOKEN`) and compares it in constant time.  That is
    token EQUALITY, not introspection — stated here because the difference matters: a deployment
    whose issuer rotates tokens must update this value with it.

    The refusal for a mismatch carries the RFC 6750 code `invalid_token` in its message, which is
    what the vendor documents as the trigger to fetch a fresh token and retry.  An unconfigured
    token is a named 401 (never an open inbox, never a 503: nothing here becomes true by retrying).
    """
    expected = (oauth_token or "").strip()
    if not expected:
        raise MeridianError(
            E_AUTH,
            "the Inbound Parse OAuth credential cannot be verified: SENDGRID_INBOUND_OAUTH_TOKEN "
            "is unset, so no bearer token can be authenticated",
            how_to_fix="put the token your authorization server issues into "
            "SENDGRID_INBOUND_OAUTH_TOKEN, or enable the security policy's signature instead "
            "(SENDGRID_INBOUND_PUBLIC_KEY), then restart the API",
            context={"setting": "SENDGRID_INBOUND_OAUTH_TOKEN", "reason": "unconfigured"},
        )
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.strip().lower() != "bearer" or not presented.strip():
        raise MeridianError(
            E_AUTH,
            "the Authorization header is not a Bearer token (RFC 6750 `Authorization: Bearer "
            "<token>`)",
            how_to_fix="attach an OAuth security policy to the Parse webhook; SendGrid then sends "
            "`Authorization: Bearer <token>`",
            context={"reason": "invalid_request"},
        )
    mismatch_refusal = MeridianError(
        E_AUTH,
        "the bearer token does not match SENDGRID_INBOUND_OAUTH_TOKEN — invalid_token",
        how_to_fix="align the authorization server's issued token with "
        "SENDGRID_INBOUND_OAUTH_TOKEN (SendGrid requests a fresh token when the refusal carries "
        "invalid_token)",
        context={"reason": "invalid_token", "setting": "SENDGRID_INBOUND_OAUTH_TOKEN"},
    )
    try:
        matched = secrets.compare_digest(
            presented.strip().encode("utf-8"), expected.encode("utf-8")
        )
    except UnicodeError:  # lone surrogates cannot round-trip UTF-8 — not a possible token
        matched = False
    if not matched:
        raise mismatch_refusal


async def ingest_sendgrid_inbound(
    db: AsyncSession,
    *,
    raw_body: bytes,
    content_type: str,
    delivery_auth: str,
) -> InboundIngestResult:
    """Ingest ONE SendGrid Inbound Parse delivery (the credential is verified by the caller).

    Order is the vendor's own: authenticate the RAW bytes (the route does that), then parse, then
    write through the ONE writer.  The parsed item is stored exactly like the JSON path's row —
    same `submit_item`, same idempotency, same prospect-reply stamping — so the 3h attach sweep
    cannot tell which provider produced it (except for the `provider` tag it records).
    """
    if not is_multipart_form_data(content_type):
        raise MeridianError(
            E_VALIDATION,
            f"the SendGrid Inbound Parse adapter only accepts multipart/form-data; received "
            f"{media_type_of(content_type) or 'no Content-Type'}",
            how_to_fix="this is an internal dispatch error: the route must call this adapter only "
            "for multipart deliveries",
            context={"field": "payload", "content_type": media_type_of(content_type)},
        )
    fields = parse_multipart_form(raw_body, content_type)
    parsed = build_sendgrid_inbound_item(fields)
    return await ingest_inbound_item(
        db,
        provider_message_id=parsed.provider_message_id,
        from_address=parsed.from_address,
        subject=parsed.subject,
        body=parsed.body,
        received_at=parsed.received_at,
        provider=parsed.provider,
        delivery_auth=delivery_auth,
        body_source=parsed.body_source,
        provider_id_source=parsed.provider_id_source,
    )


__all__ = [
    "ANSWER_WINDOW_DAYS",
    "DEDUPE_KEY_PREFIX",
    "INBOUND_AUDIT_ACTION",
    "INBOUND_AUDIT_AGENT",
    "MAX_INBOUND_BODY_CHARS",
    "MAX_INBOUND_MULTIPART_BYTES",
    "MAX_INBOUND_PROVIDER_CHARS",
    "MAX_PROVIDER_MESSAGE_ID_LEN",
    "OPT_OUT_AUDIT_ACTION",
    "SENDGRID_DEFAULT_SIGNATURE_TOLERANCE_SECONDS",
    "SENDGRID_DIGEST_PREFIX",
    "SENDGRID_INBOUND_PROVIDER",
    "SENDGRID_SIGNATURE_HEADER",
    "SENDGRID_TIMESTAMP_HEADER",
    "VALID_STATUSES",
    "InboundIngestResult",
    "SendGridInboundItem",
    "attach_pending",
    "build_sendgrid_inbound_item",
    "canonical_sendgrid_digest",
    "count_expired_pending",
    "derive_sendgrid_provider_message_id",
    "ingest_inbound_item",
    "ingest_sendgrid_inbound",
    "inbox_task_dedupe_key",
    "is_multipart_form_data",
    "list_items",
    "media_type_of",
    "parse_multipart_form",
    "stamp_prospect_replied",
    "submit_item",
    "verify_sendgrid_oauth_token",
    "verify_sendgrid_signature",
]

