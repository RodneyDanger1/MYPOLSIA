"""F-09 / R7 — a memory's identity is the provider's id or nothing; order is content, not position.

The defect (MASTER-PLAN F-09, plan §A-T16): `base_agent` built each item's id as
`str(m.get("id") or m.get("memory_id") or i)` — the enumerate index became an id when both keys
were absent (and a REAL id of `0` was skipped by the `or` chain).  `rank_memories` then tie-broke
on that positional string, so two queries over the same items could disagree on order.
"""
from __future__ import annotations

from app.agents.context_assembler import MemoryItem, memory_identity, rank_memories
from app.schemas.memory import MemoryItem as MemoryResponseItem


def _item(title: str, memory_id: str | None = None) -> MemoryItem:
    """Equal-score items: identical similarity/age/tags ⇒ identical rank score (deterministic)."""
    return MemoryItem(
        memory_id=memory_id,
        title=title,
        content=f"body of {title}",
        similarity=0.5,
        age_days=10.0,
        tags=(),
    )


def test_memory_identity_is_the_provider_id_or_none() -> None:
    assert memory_identity({"id": 7}) == "7"
    assert memory_identity({"memory_id": "abc"}) == "abc"
    assert memory_identity({"id": None, "memory_id": "abc"}) == "abc"
    assert memory_identity({"title": "no ids here"}) is None
    # `0` is a REAL id — the old `or` chain skipped it and fell through to the enumerate index
    assert memory_identity({"id": 0}) == "0"
    # the empty string is not an identity either; the next key (then None) is the truth
    assert memory_identity({"id": "", "memory_id": 3}) == "3"
    assert memory_identity({"id": "", "memory_id": ""}) is None


def test_memory_item_defaults_its_identity_to_none() -> None:
    item = MemoryItem(title="t", content="c", similarity=0.1, age_days=1.0)
    assert item.memory_id is None


def test_rank_order_is_determined_by_content_not_input_position() -> None:
    alpha, beta = _item("Alpha"), _item("Beta")
    once = [m.title for m in rank_memories([alpha, beta], query_terms=["x"], k=10)]
    twice = [m.title for m in rank_memories([alpha, beta], query_terms=["x"], k=10)]
    swapped = [m.title for m in rank_memories([beta, alpha], query_terms=["x"], k=10)]
    assert once == ["Alpha", "Beta"]      # stable across runs …
    assert twice == once
    assert swapped == once                # … and across the provider's order (the old defect)


def test_title_ties_fall_back_to_the_provider_id() -> None:
    a, b, c = _item("Same", "b"), _item("Same", "a"), _item("Same", None)
    ordered = [m.memory_id for m in rank_memories([a, b, c], query_terms=[], k=10)]
    assert ordered == [None, "a", "b"]    # content decides; "" sorts before "a"/"b"


def test_pinned_order_is_also_content_determined() -> None:
    alpha, beta = _item("Alpha"), _item("Beta")
    alpha.pinned = beta.pinned = True
    ordered = [m.title for m in rank_memories([beta, alpha], query_terms=[], k=10)]
    assert ordered == ["Alpha", "Beta"]


def test_an_id_less_hit_serialises_as_null() -> None:
    """The semantic path can return a hit with no id — the response shape must say `null`."""
    hit = {"id": None, "content": "no id here", "title": "t"}
    assert MemoryResponseItem.model_validate(hit).model_dump()["id"] is None
