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

THREADING (L1-03): every chroma call is BLOCKING — `get_collection` opens the on-disk
store, `add`/`query` run the embedding model in-process, `delete` hits the index.  Called
directly from these async functions they run ON the event loop, freezing every other
request (upstream e2e measured a >30 s first-call cold load that produced a proxy 502
while the row still landed).  Each call is therefore offloaded with `asyncio.to_thread`
— the same discipline `daily_cycle` applies to the blocking CLI.  The calls keep their
shapes (keyword arguments, `get_collection` still referenced from THIS namespace), so the
shipped tests' patch targets are unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chroma_client import get_collection  # noqa: F401  (re-exported for patching)
from app.core.errors import E_DB, E_NOT_FOUND, E_STORAGE, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import now_utc
from app.models.memory import MemoryEntry
from app.services.query_helpers import fetch_scalar, fetch_scalars

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
        # L1-03: the store open and the index write are blocking — off the loop.
        collection = await asyncio.to_thread(get_collection)
        await asyncio.to_thread(
            collection.add,
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
        # L1-03: the store open and the query (embedding + HNSW) are blocking — off the loop.
        collection = await asyncio.to_thread(get_collection)
        where = _build_where(category, None)
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": n_results}
        if where:
            kwargs["where"] = where
        try:
            raw = await asyncio.to_thread(collection.query, **kwargs)
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
            raw = await asyncio.to_thread(
                collection.query, query_texts=[query], n_results=n_results
            )
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
    """One entry by id, or `None`; a failed read is a named `E_DB` (L2-06 — was a raw
    `db.execute`, so `DELETE /memory/{id}` answered a codeless 500 on an outage)."""
    return await fetch_scalar(
        db, select(MemoryEntry).where(MemoryEntry.id == memory_id), what=f"memory {memory_id}"
    )


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
            # L1-03: the store open and the document delete are blocking — off the loop.
            collection = await asyncio.to_thread(get_collection)
            await asyncio.to_thread(collection.delete, ids=[chroma_id])
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


# ---------------------------------------------------------------------------
# AGENT MEMORY LEGS (V15/4-05, S5-02 a+b) — the read resolver + the write helper.
#
# The worker owns the DB (agents never hold a session), so both legs live HERE and are called
# from the worker: `resolve_agent_memories` feeds `context["memories"]` (the base agent's
# `context_memories` layer input), and `remember_run` persists one honest, run-derived entry
# after a completed run.  Both reuse the shipped machinery (`search_memory`, `store_memory`)
# instead of growing a second memory path.
# ---------------------------------------------------------------------------


def _age_days(created_at: datetime | None) -> float:
    """How old a memory row is, in days — 0.0 when the row carries no measurable clock."""
    if created_at is None:
        return 0.0
    created = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    return max(0.0, round((now_utc() - created).total_seconds() / 86400.0, 3))


async def resolve_agent_memories(
    db: AsyncSession,
    *,
    agent_type: str,
    query: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """The run's agent-relevant memories, in the exact shape the prompt layer ranks (S5-02a).

    H4-frozen interface (V15/4-05a): the WORKER calls this and hands the result to the agent
    under `context["memories"]`; `base_agent.context_memories` picks it up and `layers_for`
    renders the volatile `memories` layer through `rank_memories`.  Relevance comes from the
    SHIPPED search machinery (`search_memory` — vector search with the SQL keyword fallback),
    queried with the caller's task terms; every hit is enriched with its row's `tags` and
    `age_days` so the ranker's inputs are real rather than defaults.

    A vector-store failure with no fallback match raises the named `E_DB` (see `search_memory`)
    — the worker's hook catches that and leaves the context key ABSENT; an empty list must
    never stand in for "could not read" (the same rule every `_with_*` worker hook follows).
    """
    text = (query or "").strip() or f"{agent_type} agent run"
    hits = await search_memory(db, text, n_results=max(1, min(int(limit or 8), 20)))
    ids = [hit.get("memory_id") for hit in hits if isinstance(hit.get("memory_id"), int)]
    rows: dict[int, MemoryEntry] = {}
    if ids:
        entries = await fetch_scalars(
            db, select(MemoryEntry).where(MemoryEntry.id.in_(ids)),
            what="memory prompt rows",
        )
        rows = {int(entry.id): entry for entry in entries}
    views: list[dict[str, Any]] = []
    for hit in hits:
        memory_id = hit.get("memory_id")
        row = rows.get(memory_id) if isinstance(memory_id, int) else None
        distance = hit.get("distance")
        similarity = (
            max(0.0, round(1.0 - float(distance), 4))
            if isinstance(distance, (int, float))
            else 0.0
        )
        views.append({
            "memory_id": memory_id if memory_id is not None else hit.get("id"),
            "title": str(hit.get("title") or (row.title if row is not None else "")),
            "content": str(hit.get("content") or ""),
            "category": hit.get("category") or (row.category if row is not None else None),
            "source": hit.get("source") or (row.source if row is not None else None),
            "similarity": similarity,
            "age_days": _age_days(row.created_at) if row is not None else 0.0,
            "tags": [str(tag) for tag in (row.tags or [])] if row is not None else [],
            "pinned": False,
        })
    return views


def _memory_skip(code: str, message: str, **context: Any) -> dict[str, Any]:
    """The `_persist_run_artifacts` report shape for a skip/failure — RECORDED, never raised."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "how_to_fix": ERROR_HINTS.get(code, ""),
            "context": context,
        },
    }


async def remember_run(
    db: AsyncSession,
    *,
    agent_type: str,
    task: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist ONE honest, run-derived memory for a completed run (S5-02b write leg).

    H4-frozen interface (V15/4-05b): the WORKER calls this from the run-artifact step (it owns
    the session and it commits — this function FLUSHES only).  The report follows the
    `_persist_run_artifacts` convention: `{"ok": True, "memory_id": ..., "chroma_id": ...}` or a
    RECORDED `{"ok": False, "error": {...}}` — never a raised exception (a memory-leg failure
    must not kill a run that really completed).

    What is remembered is the run's OWN summary (the one field every agent contract requires and
    the runner really produced): "honest, run-derived" means exactly that text — nothing is
    paraphrased upward, extrapolated or invented, and a run without a summary is SKIPPED BY NAME
    (`E_VALIDATION`) rather than given a fabricated one.  `store_memory`'s dual write persists
    both halves (row + vector document) under `category="agent_run"`, `source=agent_type`.
    """
    if not isinstance(result, dict):
        return _memory_skip(
            E_VALIDATION, f"{agent_type}: run result is not an object; nothing to remember"
        )
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return _memory_skip(
            E_VALIDATION,
            f"{agent_type}: the run carries no summary; nothing honest to remember (a memory "
            "is never invented)",
        )
    task_title = str((task or {}).get("title") or "").strip()
    title = f"{agent_type}: {task_title}" if task_title else f"{agent_type} run"
    try:
        entry = await store_memory(
            db,
            category="agent_run",
            title=title[:512],
            content=summary[:4000],
            source=agent_type,
            tags=[agent_type],
        )
    except PolsiaError as exc:
        return {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "how_to_fix": exc.how_to_fix,
                "context": exc.context,
            },
        }
    return {
        "ok": True,
        "memory_id": entry.id,
        "chroma_id": entry.chroma_id,
        "title": entry.title,
    }


__all__ = [
    "delete_memory",
    "get_memory",
    "list_memories",
    "remember_run",
    "resolve_agent_memories",
    "search_memory",
    "store_memory",
]
