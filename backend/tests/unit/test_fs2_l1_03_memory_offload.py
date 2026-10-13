"""PHASE-3 fixer-2 / L1-03 — the chroma calls run OFF the event loop.

Before the fix every chroma call (`get_collection`, `add`, `query`, `delete`) was a plain
blocking call inside an async function, i.e. on the uvicorn event loop: the whole API froze for
the duration (upstream e2e measured a >30 s first-call cold load that surfaced as a proxy 502
while the row still landed).  The fix offloads each call with `asyncio.to_thread`.

The pin: while the collection BLOCKS, a concurrent asyncio heartbeat must keep ticking — it
cannot if the call runs on the loop.  The patch target (`app.services.memory_service.
get_collection`) is unchanged, so the shipped tests' MagicMocks still see every call.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services import memory_service


class _BlockingCollection:
    """A chroma collection whose every call blocks for `delay` seconds (off-loop proof)."""

    def __init__(self, delay: float = 0.15) -> None:
        self.delay = delay
        self.calls: dict[str, dict[str, object]] = {}

    def add(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls["add"] = kwargs
        time.sleep(self.delay)

    def query(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls["query"] = kwargs
        time.sleep(self.delay)
        return {
            "documents": [["hit-content"]],
            "metadatas": [[{"category": "strategy", "title": "T"}]],
            "ids": [["chroma-1"]],
            "distances": [[0.1]],
        }

    def delete(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls["delete"] = kwargs
        time.sleep(self.delay)


async def _ticks_during(coro, *, min_ticks: int = 5):
    """Run `coro`, counting how many times a 10 ms asyncio heartbeat got the loop."""
    ticks = 0
    done = False

    async def heartbeat() -> None:
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)  # let the heartbeat start before the blocking call begins
    before = ticks
    result = await coro
    done = True
    await task
    return result, ticks - before, ticks


@pytest.mark.asyncio
async def test_search_memory_query_does_not_block_the_loop(async_db_session, monkeypatch):
    collection = _BlockingCollection()
    monkeypatch.setattr(memory_service, "get_collection", lambda: collection)

    results, ticks, _ = await _ticks_during(
        memory_service.search_memory(async_db_session, "q", n_results=1)
    )

    assert results[0]["content"] == "hit-content"
    assert collection.calls["query"]["query_texts"] == ["q"]
    assert ticks >= 5, (
        f"the loop only got {ticks} tick(s) while the 150 ms chroma query ran — the call is "
        "still on the event loop"
    )


@pytest.mark.asyncio
async def test_store_and_delete_offload_and_keep_call_shapes(async_db_session, monkeypatch):
    collection = _BlockingCollection()
    monkeypatch.setattr(memory_service, "get_collection", lambda: collection)

    entry, store_ticks, ticks = await _ticks_during(
        memory_service.store_memory(async_db_session, category="c", title="t", content="body")
    )
    assert store_ticks >= 5, f"store_memory blocked the loop ({store_ticks} ticks)"
    assert collection.calls["add"]["documents"] == ["body"]
    assert collection.calls["add"]["ids"] == [entry.chroma_id]

    _, delete_ticks, _ = await _ticks_during(
        memory_service.delete_memory(async_db_session, entry.id)
    )
    assert delete_ticks >= 5, f"delete_memory blocked the loop ({delete_ticks} ticks)"
    assert collection.calls["delete"]["ids"] == [entry.chroma_id]
