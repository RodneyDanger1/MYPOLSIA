"""GET/POST/DELETE /api/v1/memory.

`?q=` is semantic search (ChromaDB) with a keyword fallback inside the service;
`?category=&limit=` is the plain list.  Every refusal from `memory_service`
(`E_VALIDATION` on a supplied-but-empty query, `E_STORAGE` when a write cannot be indexed or a
delete cannot remove the document, `E_NOT_FOUND` for an unknown id, `E_DB` when
nothing can be read) reaches the client as that named refusal.

`DELETE /{memory_id}` removes the sqlite row AND its ChromaDB document in the same
request flow (order + refusal semantics: `memory_service.delete_memory`).  It
answers with the removed entry's representation rather than a bare 204, so the
caller can verify which document was removed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.models.memory import MemoryEntry
from app.schemas.memory import MemoryCreate, MemoryEntryResponse, MemoryItem
from app.services.memory_service import delete_memory, list_memories, search_memory, store_memory

router = APIRouter(prefix="/memory", tags=["memory"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=list[MemoryItem])
async def read_memory(
    response: Response,
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryItem]:
    # L3-06 (V18): the docstring's `E_VALIDATION` on an empty query is reachable only for a
    # SUPPLIED `q`.  The old falsy `if q:` sent `?q=` down the unfiltered-list branch, so the
    # documented refusal never fired; `q is not None` makes the contract true by name (an empty
    # or whitespace query is refused inside `search_memory`, an ABSENT `q` is the plain list).
    if q is not None:
        with refusal_guard():
            hits = await search_memory(db, q, n_results=limit, category=category)
        # No total header on a semantic search: the hit list is the index's answer set, not a
        # table page, and a "match count" would have to come from a similarity threshold the
        # product does not define (an invented number is worse than no declaration).
        return [MemoryItem.model_validate(hit) for hit in hits]
    with refusal_guard():
        entries = await list_memories(db, category=category, limit=limit)
        # V20/L1 (card L2-07): the FULL filtered count of the LIST read (same WHERE, no LIMIT),
        # so a full page is distinguishable from a truncated one.  Body stays a bare array.
        total = await declared_total(
            db,
            MemoryEntry,
            [] if category is None else [MemoryEntry.category == category],
            what="memory total",
        )
    response.headers[TOTAL_HEADER] = str(total)
    return [MemoryItem.model_validate(entry, from_attributes=True) for entry in entries]


@router.post("", response_model=MemoryEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(payload: MemoryCreate, db: AsyncSession = Depends(get_db)) -> MemoryEntryResponse:
    """Store one memory entry (sqlite row + ChromaDB document).

    The Memory page has offered an "Add memory" control since V14 (`frontend/src/app/memory/page.tsx`
    → `POST /memory`), so this route is product-reachable and its named `E_STORAGE` refusal renders
    inline. (A stale V13/V14 note here once called it API-only; corrected in the V22 honesty sweep,
    W2/D7.)
    """
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


@router.delete("/{memory_id}", response_model=MemoryEntryResponse)
async def remove_memory(memory_id: int, db: AsyncSession = Depends(get_db)) -> MemoryEntryResponse:
    """Remove a memory from both stores (sqlite row + ChromaDB document).

    Named `remove_memory`, not `delete_memory`, because the service function of
    that name is imported into this namespace (same reason `tasks.py` suffixes its
    handlers `_endpoint`).  The 200 body is the removed entry's representation:
    it carries the `chroma_id` that was deleted, so the caller can check the exact
    document rather than trusting an empty response.

    INTENDED STATUS (tracker S3-03, wave v13 wiring sweep; corrected in the V22 honesty sweep,
    W2/D7): the Memory page has offered a per-row "Forget" control since V14
    (`frontend/src/app/memory/page.tsx` → `DELETE /memory/{id}`), so the named `E_NOT_FOUND`
    refusal is product-reachable. The earlier "API-only at this HEAD" note was stale.
    """
    with refusal_guard():
        entry = await delete_memory(db, memory_id)
    return MemoryEntryResponse.model_validate(entry)
