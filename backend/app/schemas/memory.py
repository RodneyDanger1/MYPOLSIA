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
    """Union shape for BOTH the plain list and the semantic-search list.

    `fallback` is the service's DEGRADATION MARKER, and it must survive to the wire: the service
    tags a degraded read on purpose (`memory_service._sql_fallback` → `"sql_like"` when the vector
    store is down and the read became an ILIKE scan; `"sql_unfiltered"` when the collection rejected
    `where` and the category filter was dropped).  A response model is a filter — without this field
    pydantic dropped the marker, so a keyword scan was served indistinguishably from a semantic hit
    (L1-02).  `None` means the hit came from the real similarity search; `distance: None` cannot
    substitute for the marker (a real hit may carry it too).
    """

    id: int | str | None = None
    content: str
    category: str | None = None
    title: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    chroma_id: str | None = None
    distance: float | None = None
    fallback: str | None = None  # "sql_like" | "sql_unfiltered" | None (semantic)
    created_at: datetime | None = None
