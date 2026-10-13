"""prospects + email_campaigns + email_logs — consumed by app.services.outreach_service,
app.services.channel_service (send caps) and the worker persist path; DDL-pinned by
tests/unit/test_models_schema_frozen.py (M3: the old "(module name INFERRED - unreferenced)"
header was stale — only models/competitor.py is genuinely unreferenced by services)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
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
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="sent", default="sent"
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
    )
