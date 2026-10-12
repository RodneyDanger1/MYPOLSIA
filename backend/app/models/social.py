"""social_posts + social_engagements."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from app.core.database import Base
from app.models.types import json_type


class SocialPost(Base):
    __tablename__ = "social_posts"

    id = Column(Integer, primary_key=True)
    platform = Column(String(50), server_default="twitter", default="twitter")
    content = Column(Text, nullable=False)
    status = Column(String(50), server_default="draft", default="draft")
    tweet_id = Column(String(100))
    scheduled_for = Column(DateTime(timezone=True))
    published_at = Column(DateTime(timezone=True))
    engagement = Column(json_type())
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialEngagement(Base):
    __tablename__ = "social_engagements"

    id = Column(Integer, primary_key=True)
    mention_id = Column(String(100), unique=True)
    author_handle = Column(String(100))
    content = Column(Text)
    our_reply = Column(Text)
    reply_id = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
