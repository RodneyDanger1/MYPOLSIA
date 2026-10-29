"""prospects + email_campaigns + email_logs — consumed by app.services.outreach_service,
app.services.channel_service (send caps) and the worker persist path; DDL-pinned by
tests/unit/test_models_schema_frozen.py (M3: the old "(module name INFERRED - unreferenced)"
header was stale — only models/competitor.py is genuinely unreferenced by services)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="new", default="new"
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmailCampaign(Base):
    __tablename__ = "email_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    target_segment: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="active", default="active"
    )
    total_sent: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    total_opened: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    total_replied: Mapped[int | None] = mapped_column(Integer, server_default="0", default=0)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EmailLog(Base):
    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("prospects.id"), nullable=False
    )
    campaign_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("email_campaigns.id"))
    sequence_step: Mapped[int | None] = mapped_column(Integer, server_default="1", default=1)
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    sendgrid_id: Mapped[str | None] = mapped_column(String(255))
    #: WAVE 7: the state vocabulary is `draft | queued | sent | failed` (`services.email_lifecycle`),
    #: and the DEFAULT is `draft` — the state that claims the least.  It shipped as `"sent"` at
    #: both the ORM and the server, so any writer that forgot to state a status (a data import, a
    #: raw INSERT, a future producer) fabricated a PERFORMED SEND with no provider id, which the
    #: email cap would then count.  Migration 0019 changes the server default and adds the
    #: provider-acceptance check; `record_sent_emails` is the only writer that ever sets `sent`,
    #: and it requires the provider's own message id.
    #:
    #: WAVE 8: 0019's check was `status <> 'sent' OR sendgrid_id IS NOT NULL` — and an EMPTY
    #: STRING is not NULL, so a raw INSERT with `sendgrid_id=''` still fabricated a send, and a
    #: `sent` row could carry no `sent_at` at all (measured live: both probes inserted).  The
    #: constraint below is the strengthened rule migration 0020 installs on PostgreSQL, and
    #: `create_all` renders it on SQLite too (`trim`/`length` are portable; `btrim` is not), so
    #: the unit-test engine enforces the same shape the live database does.  A `sent` row must
    #: carry a NON-BLANK provider id AND the instant it was recorded — the two pieces of evidence
    #: the database itself can check; whether the id came from a real provider is the writer's
    #: answer (see `services.email_lifecycle`).
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="draft", default="draft"
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # migration 0005 (L2-08): the natural key `record_sent_emails` dedupes on — one
        # provider message id = one real send.  Partial, so legacy rows without a
        # `sendgrid_id` stay legal.  Mapped here so `create_all` matches the chain's schema.
        Index(
            "uq_email_logs_sendgrid_id",
            "sendgrid_id",
            unique=True,
            postgresql_where=text("sendgrid_id IS NOT NULL"),
            sqlite_where=text("sendgrid_id IS NOT NULL"),
        ),
        # WAVE 8 / migration 0020 (strengthens 0019's `sendgrid_id IS NOT NULL`): a `sent` row
        # must carry a non-blank provider message id AND the instant it was recorded.  0019's
        # predicate let `sendgrid_id=''` through (an empty string is not NULL) and required no
        # `sent_at`, so a direct SQL/ORM write could still fabricate a send or a send with no
        # time; both probes inserted on the live DB before this revision.  `trim()` is used on
        # purpose — `btrim` is PostgreSQL-only, and `create_all` must render the same rule on the
        # SQLite test engine.  NULL never satisfies a CHECK on either engine, so the explicit
        # `IS NOT NULL` is load-bearing: without it `trim(NULL) <> ''` is NULL and the row passes.
        CheckConstraint(
            "status <> 'sent' OR ("
            "sendgrid_id IS NOT NULL AND trim(sendgrid_id) <> '' AND sent_at IS NOT NULL)",
            name="ck_email_logs_sent_requires_provider_id",
        ),
    )
