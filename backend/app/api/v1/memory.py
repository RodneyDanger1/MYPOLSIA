"""GET/POST /api/v1/memory.

`?q=` is semantic search (ChromaDB) with a keyword fallback inside the service;
`?category=&limit=` is the plain list.  Every refusal from `memory_service`
(`E_VALIDATION` on an empty query, `E_STORAGE` when a write cannot be indexed,
`E_DB` when nothing can be read) reaches the client as that named refusal.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.memory import MemoryCreate, MemoryEntryResponse, MemoryItem
from app.services.memory_service import list_memories, search_memory, store_memory

router = APIRouter(prefix="/memory", tags=["memory"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=list[MemoryItem])
async def read_memory(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryItem]:
    if q:
        with refusal_guard():
            hits = await search_memory(db, q, n_results=limit, category=category)
        return [MemoryItem.model_validate(hit) for hit in hits]
    with refusal_guard():
        entries = await list_memories(db, category=category, limit=limit)
    return [MemoryItem.model_validate(entry, from_attributes=True) for entry in entries]


@router.post("", response_model=MemoryEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(payload: MemoryCreate, db: AsyncSession = Depends(get_db)) -> MemoryEntryResponse:
    with refusal_guard():
        entry = await store_memory(
            db,
            category=payload.category,
            title=payload.title,
            content=payload.content,
            source=payload.source,
            tags=payload.tags,
            metadata=payload.metadata,
        )
    return MemoryEntryResponse.model_validate(entry)
