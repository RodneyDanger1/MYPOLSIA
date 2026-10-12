"""prospects + email_campaigns + email_logs (module name INFERRED - unreferenced)."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func

from app.core.database import Base


class Prospect(Base):
    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    first_name = Column(String(100))
    last_name = Column(String(100))
    company = Column(String(255))
    title = Column(String(255))
    source = Column(String(100))
    status = Column(String(50), server_default="new", default="new")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EmailCampaign(Base):
    __tablename__ = "email_campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    goal = Column(Text)
    target_segment = Column(String(255))
    status = Column(String(50), server_default="active", default="active")
    total_sent = Column(Integer, server_default="0", default=0)
    total_opened = Column(Integer, server_default="0", default=0)
    total_replied = Column(Integer, server_default="0", default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EmailLog(Base):
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, ForeignKey("prospects.id"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("email_campaigns.id"))
    sequence_step = Column(Integer, server_default="1", default=1)
    subject = Column(String(512))
    body = Column(Text)
    sendgrid_id = Column(String(255))
    status = Column(String(50), server_default="sent", default="sent")
    sent_at = Column(DateTime(timezone=True))
    opened_at = Column(DateTime(timezone=True))
    replied_at = Column(DateTime(timezone=True))
    follow_up_due = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
