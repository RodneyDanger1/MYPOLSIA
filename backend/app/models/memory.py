"""memory_entries."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, Text, func

from app.core.database import Base
from app.models.types import string_list_type


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    id = Column(Integer, primary_key=True)
    category = Column(String(100), nullable=False)
    title = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    source = Column(String(100))
    tags = Column(string_list_type())
    chroma_id = Column(String(255), unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
