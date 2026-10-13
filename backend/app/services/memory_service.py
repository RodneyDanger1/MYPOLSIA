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
  removes — and if that fallback read also fails, the caller gets `E_DB`;
* a collection whose client rejects `where` (TypeError) is retried without the
  category filter — the drop is recorded (`code=where_filter_dropped`, naming the
  query and the lost category) and every hit that retry returns is tagged
  `fallback: "sql_unfiltered"`, so an unfiltered read cannot pass as filtered;
* `delete_memory` removes the ChromaDB document FIRST and the row SECOND (the
  mirror of the dual write, and the sweep's own order).  A chroma failure refuses
  with `E_STORAGE` and keeps the row: both halves still exist, so the state stays
  consistent and the delete can be retried.  The inverse order would strand a
  searchable ghost whose row — and with it the `chroma_id` needed to find the
  document — is already gone.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chroma_client import get_collection  # noqa: F401  (re-exported for patching)
from app.core.errors import E_DB, E_NOT_FOUND, E_STORAGE, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.memory import MemoryEntry
from app.services.query_helpers import fetch_scalars

MAX_FALLBACK_LIMIT = 200

logger = logging.getLogger(__name__)


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

    where_dropped = False
    try:
        collection = get_collection()
        where = _build_where(category, None)
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": n_results}
        if where:
            kwargs["where"] = where
        try:
            raw = collection.query(**kwargs)
        except TypeError:
            # A collection whose client rejects `where` still answers the plain query — but the
            # category filter is LOST, so the drop is recorded and every hit from this retry is
            # tagged `fallback: "sql_unfiltered"`: an unfiltered read must not pass as filtered.
            where_dropped = True
            logger.warning(
                "code=where_filter_dropped: collection rejected `where`; retrying without it "
                "(query=%r, category=%r, where=%r)",
                query,
                category,
                where,
            )
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
        hit: dict[str, Any] = {
            "id": ids[index] if index < len(ids) else None,
            "content": document,
            "category": meta.get("category"),
            "title": meta.get("title"),
            "source": meta.get("source"),
            "memory_id": meta.get("memory_id"),
            "distance": distances[index] if index < len(distances) else None,
        }
        if where_dropped:
            hit["fallback"] = "sql_unfiltered"
        results.append(hit)
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


async def delete_memory(db: AsyncSession, memory_id: int) -> MemoryEntry:
    """Remove a memory from BOTH stores — the sqlite row and its ChromaDB document.

    Order is load-bearing (mirror of `store_memory`'s dual write; same order as
    `e2e/_sweep-fixtures.py`):

      1. read the row — a missing row is a named `E_NOT_FOUND` refusal, never a
         silent success for a delete of something that is not there;
      2. remove the ChromaDB document FIRST.  If that fails, the row is left
         alone and the refusal is `E_STORAGE` (the same classification
         `store_memory` uses for the vector store): both halves still exist, so
         the state stays consistent and the delete can be re-run.  Deleting the
         row first would make a chroma failure strand a searchable ghost — and
         with the row gone, the `chroma_id` needed to remove the document would
         be gone too;
      3. delete the row and flush, so a 200 means BOTH halves were removed before
         the response was built.

    A row without a `chroma_id` (legacy data) has no document to strand, so step 2
    is skipped.  `collection.delete(ids=[...])` is idempotent for an id that is
    already absent, so a retry after a partially-applied earlier attempt is safe.

    The returned entry is the representation of what was removed; its attributes
    are still readable after the flush (a deleted instance is not expired).
    """
    entry = await get_memory(db, memory_id)
    if entry is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"memory {memory_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"memory_id": memory_id},
        )

    chroma_id = entry.chroma_id
    if chroma_id:
        try:
            get_collection().delete(ids=[chroma_id])
        except Exception as exc:  # noqa: BLE001 - the vector store is an external dependency
            raise PolsiaError(
                E_STORAGE,
                f"memory {memory_id} was NOT deleted: its ChromaDB document {chroma_id!r} "
                f"could not be removed ({type(exc).__name__}: {exc}) — the row is kept so the "
                "two stores stay consistent",
                how_to_fix="check CHROMA_PERSIST_DIR / the Chroma service, then retry the delete",
                context={"memory_id": memory_id, "chroma_id": chroma_id},
            ) from exc

    await db.delete(entry)
    try:
        await db.flush()
    except SQLAlchemyError as exc:
        # The document is already gone; the row is not.  Reporting success would be a lie
        # and staying quiet would hide the half-state — so the refusal says exactly what
        # happened, and names the re-run that finishes the job (a chroma delete of an
        # already-removed id is a no-op, so re-running this call is safe).
        removed = (
            f"its ChromaDB document {chroma_id!r} was already removed but the row could not "
            "be deleted"
            if chroma_id
            else "the row could not be deleted"
        )
        raise PolsiaError(
            E_DB,
            f"memory {memory_id}: {removed} ({type(exc).__name__}: {exc}) — re-run the delete "
            "to finish",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"memory_id": memory_id, "chroma_id": chroma_id},
        ) from exc
    return entry


__all__ = ["delete_memory", "get_memory", "list_memories", "search_memory", "store_memory"]
