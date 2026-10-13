"""social_posts + social_engagements."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import json_type


class SocialPost(Base):
    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str | None] = mapped_column(
        String(50), server_default="twitter", default="twitter"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(
        String(50), server_default="draft", default="draft"
    )
    tweet_id: Mapped[str | None] = mapped_column(String(100))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engagement: Mapped[dict[str, Any] | None] = mapped_column(json_type())
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # migration 0005 (L2-08): the natural key `record_published_posts` dedupes on —
        # `tweet_id` alone is not identity (it is the provider's id, scoped by platform).
        # Partial, so legacy rows without a provider id stay legal (NULLs are distinct in
        # both dialects).  Mapped here so `create_all` matches the chain's schema.
        Index(
            "uq_social_posts_platform_tweet_id",
            "platform",
            "tweet_id",
            unique=True,
            postgresql_where=text("tweet_id IS NOT NULL"),
            sqlite_where=text("tweet_id IS NOT NULL"),
        ),
    )


class SocialEngagement(Base):
    __tablename__ = "social_engagements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mention_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    author_handle: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str | None] = mapped_column(Text)
    our_reply: Mapped[str | None] = mapped_column(Text)
    reply_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
