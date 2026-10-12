"""Dual-write memory: PostgreSQL row + ChromaDB vector.

`get_collection` is imported into THIS namespace because the shipped conftest
patches `app.services.memory_service.get_collection` (CLAUDE.md: patch where the
name is used, not where it is defined).  Neither `chromadb` nor `asyncpg` may be
imported at module level (CLAUDE.md:116).

Failure policy — no silent partial success:

* a ChromaDB write failure is `E_STORAGE` (the row would be unsearchable, so
  reporting 201 would be a lie the operator only discovers later);
* a ChromaDB *read* failure degrades to a SQL `ILIKE` search over the same table
  (each hit is tagged `fallback: "sql_like"`), because an empty list standing in
  for "the vector store is down" is exactly the deceptive shape this rebuild
  removes — and if that fallback read also fails, the caller gets `E_DB`.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chroma_client import get_collection  # noqa: F401  (re-exported for patching)
from app.core.errors import E_DB, E_STORAGE, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.memory import MemoryEntry
from app.services.query_helpers import fetch_scalars

MAX_FALLBACK_LIMIT = 200


def _build_where(category: str | None, source: str | None) -> dict[str, Any] | None:
    clauses = []
    if category:
        clauses.append({"category": category})
    if source:
        clauses.append({"source": source})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


async def store_memory(
    db: AsyncSession,
    category: str,
    title: str,
    content: str,
    source: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> MemoryEntry:
    """Write the row, then index the content in ChromaDB.

    `collection.add` must be called with KEYWORD arguments — the shipped test reads
    `call_args[1]["documents"]`.  `chroma_id` is derived from the flushed row id so
    a retry after a Chroma success cannot duplicate the document (`chroma_id` is
    `unique=True` in the schema).
    """
    entry = MemoryEntry(
        category=category,
        title=title,
        content=content,
        source=source,
        tags=list(tags or []),
    )
    db.add(entry)
    await db.flush()

    chroma_id = f"memory-{entry.id}-{uuid.uuid4().hex[:8]}" if entry.id else uuid.uuid4().hex
    entry.chroma_id = chroma_id
    await db.flush()

    # Chroma metadata accepts scalars only; anything else is dropped rather than
    # silently stringified (a list rendered as "[...]" is not a real metadata value).
    chroma_metadata: dict[str, Any] = {
        "category": category,
        "title": title,
        "source": source or "unknown",
        "memory_id": entry.id,
    }
    if metadata:
        chroma_metadata.update(
            {k: v for k, v in metadata.items() if isinstance(v, str | int | float | bool)}
        )
    try:
        collection = get_collection()
        collection.add(
            documents=[content],
            metadatas=[chroma_metadata],
            ids=[chroma_id],
        )
    except Exception as exc:  # noqa: BLE001 - the vector store is an external dependency
        raise PolsiaError(
            E_STORAGE,
            f"memory {entry.id} was written but could not be indexed in ChromaDB: "
            f"{type(exc).__name__}: {exc}",
            how_to_fix="check CHROMA_PERSIST_DIR is writable and the collection is reachable, "
            "then re-post the memory (chroma_id is unique, so a retry cannot duplicate it)",
            context={"memory_id": entry.id, "chroma_id": chroma_id},
        ) from exc
    return entry


def _sql_like_escape(term: str) -> str:
    """Escape LIKE wildcards so a user's `%`/`_` is a literal character."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _sql_fallback(
    db: AsyncSession, query: str, n_results: int, category: str | None
) -> list[dict[str, Any]]:
    """Keyword search used only when the vector store is unavailable."""
    pattern = f"%{_sql_like_escape(query)}%"
    stmt = select(MemoryEntry).where(
        or_(
            MemoryEntry.title.ilike(pattern, escape="\\"),
            MemoryEntry.content.ilike(pattern, escape="\\"),
        )
    )
    if category:
        stmt = stmt.where(MemoryEntry.category == category)
    stmt = (
        stmt.order_by(MemoryEntry.created_at.desc(), MemoryEntry.id.desc())
        .limit(min(max(n_results, 1), MAX_FALLBACK_LIMIT))
    )
    # `fetch_scalars` raises E_DB if even this read fails: the caller must never see
    # an empty list that means "we could not read anything".
    entries = await fetch_scalars(db, stmt, what="memory keyword fallback")
    return [
        {
            "id": entry.id,
            "content": entry.content,
            "category": entry.category,
            "title": entry.title,
            "source": entry.source,
            "memory_id": entry.id,
            "distance": None,
            "fallback": "sql_like",
        }
        for entry in entries
    ]


async def search_memory(
    db: AsyncSession,
    query: str,
    n_results: int = 5,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search; every result dict has a "content" key."""
    if not query or not query.strip():
        raise PolsiaError(
            E_VALIDATION,
            "search_memory requires a non-empty query",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
        )

    try:
        collection = get_collection()
        where = _build_where(category, None)
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": n_results}
        if where:
            kwargs["where"] = where
        try:
            raw = collection.query(**kwargs)
        except TypeError:
            # A collection whose client rejects `where` still answers the plain query.
            raw = collection.query(query_texts=[query], n_results=n_results)
    except Exception as exc:  # noqa: BLE001 - the vector store is an external dependency
        hits = await _sql_fallback(db, query, n_results, category)
        if hits:
            return hits
        raise PolsiaError(
            E_DB,
            f"semantic search unavailable ({type(exc).__name__}: {exc}) and the keyword "
            "fallback matched nothing",
            how_to_fix="check CHROMA_PERSIST_DIR / the Chroma service, or search by keyword",
            context={"query": query},
        ) from exc

    documents = (raw.get("documents") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    ids = (raw.get("ids") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    results: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        meta = metadatas[index] if index < len(metadatas) and metadatas[index] else {}
        results.append(
            {
                "id": ids[index] if index < len(ids) else None,
                "content": document,
                "category": meta.get("category"),
                "title": meta.get("title"),
                "source": meta.get("source"),
                "memory_id": meta.get("memory_id"),
                "distance": distances[index] if index < len(distances) else None,
            }
        )
    return results


async def list_memories(
    db: AsyncSession,
    category: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MemoryEntry]:
    stmt = select(MemoryEntry)
    if category is not None:
        stmt = stmt.where(MemoryEntry.category == category)
    stmt = (
        stmt.order_by(MemoryEntry.created_at.desc(), MemoryEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return await fetch_scalars(db, stmt, what="memory entries")


async def get_memory(db: AsyncSession, memory_id: int) -> MemoryEntry | None:
    result = await db.execute(select(MemoryEntry).where(MemoryEntry.id == memory_id))
    return result.scalar_one_or_none()


__all__ = ["get_memory", "list_memories", "search_memory", "store_memory"]
