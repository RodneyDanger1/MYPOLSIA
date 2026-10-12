from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class MemoryCreate(BaseModel):
    category: str
    title: str
    content: str
    source: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class MemoryEntryResponse(ORMModel):
    id: int
    category: str
    title: str
    content: str
    source: str | None = None
    tags: list[str] | None = None
    chroma_id: str | None = None
    created_at: datetime | None = None


class MemoryItem(BaseModel):
    """Union shape for BOTH the plain list and the semantic-search list."""

    id: int | str | None = None
    content: str
    category: str | None = None
    title: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    chroma_id: str | None = None
    distance: float | None = None
    created_at: datetime | None = None
