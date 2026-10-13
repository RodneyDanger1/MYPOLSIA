"""support_inbox_items — the customer-support inbox ingest (R17).

The producer side of the one chain RD12 left open.  RD12 shipped the CONSUMER: a
customer_support task carrying `metadata.inbox_item` gets its body quarantined into the
prompt (`app/agents/customer_support/agent.py:64-149`), but nothing in the tree produced
an inbox item — the RD12 receipt §6 records the remaining gap verbatim ("Populating them
requires the ingest below").  This table is that ingest's store, `support_inbox_service`
is its only writer, and the 3h `run_email_sweep` attaches pending rows to CS tasks.

State machine (deliberately SMALL — see the receipt's SCHEMA decision):

* `pending`    — stored, not yet attached to any task;
* `dispatched` — attached to the CS task in `task_id`; the mark is written in the SAME
  transaction as the task row (`support_inbox_service.attach_pending`), so "dispatched
  with no task id" cannot exist.

A proposed `served` state is NOT created: nothing in this tree could ever honestly set it
(CS replies land in `email_logs`; nothing reads them back to an inbox row), and a state no
code path can write is exactly the dead surface RD15 hunts for.  `status` is enforced by
the service (the only writer), not by a DB CHECK: a PostgreSQL-only CHECK would make the
two dialects disagree, and the frozen schema pin
(`tests/unit/test_models_schema_frozen.py`) is generated from the ORM.

`provider_message_id` is the provider's own message id (unique): it is BOTH the ingest
idempotency key (at-least-once delivery must not duplicate an item) AND the id the whole
consumer contract is keyed on — the quarantine block's `content_id` and the reply's
`message_ref` source (`INBOX_ITEM_KEY` contract).  `received_at` is when the message
arrived, operator-supplied: NULL means "the submitter did not state it" and is NEVER
invented (the sweep orders by `coalesce(received_at, created_at)`); a timezone-less value
is refused because assuming a zone would silently reorder the attach.  `task_id` is the CS
task the item was attached to, set together with `status='dispatched'`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SupportInboxItem(Base):
    __tablename__ = "support_inbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The provider's own message id — ingest idempotency key AND the `content_id`/`message_ref`
    #: the consumer contract is built from (see the module docstring).  NOT NULL: an item
    #: without a provider id could be stored but never acted on (the agent refuses it by name).
    provider_message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The customer's message, byte-exact (validation checks non-blank but stores as submitted —
    #: stripping a customer's own message would alter evidence).
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), server_default="pending", default="pending", nullable=False
    )
    #: When the message arrived; NULL = "not stated" (never a fabricated arrival instant).
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The CS task this item was attached to (set with `status='dispatched'`, one transaction).
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    #: the sweep's FILTER column; the task_id index mirrors 0002's FK-index pass (agent_runs).
    __table_args__ = (
        Index("ix_support_inbox_items_status", "status"),
        Index("ix_support_inbox_items_task_id", "task_id"),
    )
