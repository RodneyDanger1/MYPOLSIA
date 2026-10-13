"""R19 — `memory_service.delete_memory`: ONE request flow removes BOTH halves of a memory.

A memory lives twice — a sqlite row plus a ChromaDB document (`store_memory`) — so an
honest delete has to remove both.  The defect class this pins is the GHOST DOCUMENT: a
row deleted while its chroma document survives keeps answering semantic searches (the
exact half-state `e2e/memory.spec.ts` and the suite-end sweep exist to prevent), and
once the row is gone the `chroma_id` needed to remove the document is gone too.

The shipped `tests/conftest.py::mock_chroma` fixture is a bare MagicMock: its `query`
returns a canned hit and its `delete` forgets nothing, so it cannot prove "the document
is gone".  `StatefulChromaDouble` below is a small in-test stand-in with real store
semantics (add/delete/query/get over one dict), patched into the SAME two namespaces
conftest patches (`app.core.chroma_client.get_collection` and
`app.services.memory_service.get_collection` — CLAUDE.md: patch where the name is used).
It is a test double and is used only inside tests.

RED legs (mutations of `app/services/memory_service.py::delete_memory`):
  (a) drop the `get_collection().delete(...)` block -> `test_search_after_delete_...`
      fails: the ghost document still answers the semantic search;
  (b) `return entry` right after the E_NOT_FOUND check -> the both-halves test fails:
      success is reported while both stores still hold the memory.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import E_DB, E_NOT_FOUND, E_STORAGE, ERROR_HINTS, PolsiaError
from app.models.memory import MemoryEntry
from app.services.memory_service import (
    delete_memory,
    get_memory,
    list_memories,
    search_memory,
    store_memory,
)


class StatefulChromaDouble:
    """A chroma collection stand-in with enough store semantics to prove deletions.

    `add` remembers every document keyed by its id; `delete(ids=...)` removes them and is
    a no-op for ids already absent (chroma's own semantics); `query`/`get` read the
    surviving documents, so a search AFTER a delete can be asserted to return nothing.
    `delete_raises` lets a test simulate the store being down at exactly the delete step.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.add_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.delete_raises: BaseException | None = None

    def add(self, documents, metadatas, ids) -> None:
        self.add_calls.append({"documents": list(documents), "ids": list(ids)})
        for document, metadata, doc_id in zip(documents, metadatas, ids, strict=True):
            self.docs[doc_id] = {"document": document, "metadata": metadata}

    def delete(self, ids) -> None:
        self.delete_calls.append({"ids": list(ids)})
        if self.delete_raises is not None:
            raise self.delete_raises
        for doc_id in ids:
            self.docs.pop(doc_id, None)

    def query(self, query_texts=None, n_results=None, where=None) -> dict[str, list[list[Any]]]:
        hits = list(self.docs.items())[: (n_results if n_results is not None else len(self.docs))]
        return {
            "documents": [[doc["document"] for _doc_id, doc in hits]],
            "metadatas": [[doc["metadata"] for _doc_id, doc in hits]],
            "ids": [[doc_id for doc_id, _doc in hits]],
            "distances": [[0.0 for _doc in hits]],
        }

    def get(self, ids=None, where=None) -> dict[str, list[str]]:
        return {"ids": [doc_id for doc_id in (ids or []) if doc_id in self.docs]}


@pytest.fixture
def stateful_chroma(mocker):
    """Patch the stateful double into both namespaces the way conftest's mock_chroma does."""
    collection = StatefulChromaDouble()
    mocker.patch("app.core.chroma_client.get_collection", return_value=collection)
    mocker.patch("app.services.memory_service.get_collection", return_value=collection)
    return collection


@pytest.mark.asyncio
async def test_delete_removes_row_and_chroma_document(async_db_session, stateful_chroma):
    entry = await store_memory(
        async_db_session,
        category="strategy",
        title="Doomed memory",
        content="Referrals convert 3x better than paid",
    )
    await async_db_session.commit()
    chroma_id = entry.chroma_id
    assert chroma_id and chroma_id in stateful_chroma.docs  # the dual write indexed it

    removed = await delete_memory(async_db_session, entry.id)
    await async_db_session.commit()

    assert removed.id == entry.id
    assert stateful_chroma.delete_calls == [{"ids": [chroma_id]}]  # the document, by ITS id
    assert chroma_id not in stateful_chroma.docs  # chroma half is gone …
    assert await get_memory(async_db_session, entry.id) is None  # … and so is the row
    assert await list_memories(async_db_session) == []


@pytest.mark.asyncio
async def test_delete_missing_memory_is_a_named_e_not_found(async_db_session, stateful_chroma):
    with pytest.raises(PolsiaError) as excinfo:
        await delete_memory(async_db_session, 4242)

    assert excinfo.value.code == E_NOT_FOUND
    assert "4242" in excinfo.value.message
    assert excinfo.value.how_to_fix == ERROR_HINTS[E_NOT_FOUND]
    assert excinfo.value.context == {"memory_id": 4242}
    assert stateful_chroma.delete_calls == []  # nothing was touched in the vector store


@pytest.mark.asyncio
async def test_delete_twice_second_call_refuses(async_db_session, stateful_chroma):
    entry = await store_memory(
        async_db_session, category="competitor", title="Once", content="pricing notes"
    )
    await async_db_session.commit()

    await delete_memory(async_db_session, entry.id)
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await delete_memory(async_db_session, entry.id)
    assert excinfo.value.code == E_NOT_FOUND


@pytest.mark.asyncio
async def test_search_after_delete_returns_no_ghost_hits(async_db_session, stateful_chroma):
    entry = await store_memory(
        async_db_session,
        category="strategy",
        title="Ghost bait",
        content="unique ghost token zqxjwv",
    )
    await async_db_session.commit()

    before = await search_memory(async_db_session, "unique ghost token zqxjwv")
    assert [hit["id"] for hit in before] == [entry.chroma_id]  # sanity: the semantic path finds it

    await delete_memory(async_db_session, entry.id)
    await async_db_session.commit()

    after = await search_memory(async_db_session, "unique ghost token zqxjwv")
    assert after == []  # a surviving document would answer here — the ghost hit, caught


@pytest.mark.asyncio
async def test_chroma_delete_failure_refuses_and_keeps_both_halves(async_db_session, stateful_chroma):
    entry = await store_memory(
        async_db_session, category="strategy", title="Keep me", content="still searchable"
    )
    await async_db_session.commit()
    chroma_id = entry.chroma_id

    stateful_chroma.delete_raises = RuntimeError("the vector store is down")
    with pytest.raises(PolsiaError) as excinfo:
        await delete_memory(async_db_session, entry.id)

    assert excinfo.value.code == E_STORAGE
    assert "NOT deleted" in excinfo.value.message
    assert excinfo.value.context == {"memory_id": entry.id, "chroma_id": chroma_id}
    assert excinfo.value.how_to_fix and "CHROMA_PERSIST_DIR" in excinfo.value.how_to_fix
    # NOTHING may be half-done: the row is still here …
    assert await get_memory(async_db_session, entry.id) is not None
    # … and so is its document.
    assert stateful_chroma.get(ids=[chroma_id])["ids"] == [chroma_id]

    # … and the refusal is retryable: after the store recovers, the same call finishes the job.
    stateful_chroma.delete_raises = None
    removed = await delete_memory(async_db_session, entry.id)
    await async_db_session.commit()
    assert removed.id == entry.id
    assert await get_memory(async_db_session, entry.id) is None
    assert stateful_chroma.docs == {}


@pytest.mark.asyncio
async def test_row_without_a_chroma_id_is_deleted_without_touching_chroma(
    async_db_session, stateful_chroma
):
    # Legacy/pre-index row: no document exists, so there is nothing that could be stranded.
    legacy = MemoryEntry(
        category="legacy", title="Pre-index row", content="no document", chroma_id=None
    )
    async_db_session.add(legacy)
    await async_db_session.commit()

    await delete_memory(async_db_session, legacy.id)
    await async_db_session.commit()

    assert await get_memory(async_db_session, legacy.id) is None
    assert stateful_chroma.delete_calls == []  # no invented call against the vector store


@pytest.mark.asyncio
async def test_row_delete_failure_is_named_e_db_and_says_what_was_removed(
    async_db_session, stateful_chroma, mocker
):
    entry = await store_memory(
        async_db_session, category="strategy", title="Half state", content="body"
    )
    await async_db_session.commit()
    chroma_id = entry.chroma_id

    async def _broken_flush(*args, **kwargs):
        raise SQLAlchemyError("disk I/O error")

    mocker.patch.object(async_db_session, "flush", side_effect=_broken_flush)
    with pytest.raises(PolsiaError) as excinfo:
        await delete_memory(async_db_session, entry.id)

    assert excinfo.value.code == E_DB
    # Honest wording: the document WAS already removed, the row was NOT — a refusal that
    # names the half-state out loud instead of hiding it behind a success or a bare 500.
    assert f"its ChromaDB document {chroma_id!r} was already removed" in excinfo.value.message
    assert "the row could not be deleted" in excinfo.value.message
    assert "re-run the delete to finish" in excinfo.value.message
    assert excinfo.value.context == {"memory_id": entry.id, "chroma_id": chroma_id}
    # The row really is still there.  `no_autoflush` because the pending delete would
    # otherwise re-run the broken flush while reading.
    with async_db_session.no_autoflush:
        assert await get_memory(async_db_session, entry.id) is not None
