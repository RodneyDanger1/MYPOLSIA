"""WAVE 6 — the read side of the memory-tag decision: stale-asserted rows are WITHHELD.

Measured chain (wave 5 → wave 6): the copy correction fixed the company profile and the generated
site, but 13 `memory_entries` rows still carried a banned/unverified brand claim.  Six of them assert
the previous brand's facts as OUR product facts (`claim:stale-asserted`), and ONE of them (row 225) is
CURRENT company scope — written AFTER the correction, because task 327's brief said *"include the
scent, hand-poured in Los Angeles, 9 oz"* and the model complied.  Wave 6 applied the reversible
review tags (`raw-w6/w6-memory-tag-apply.sql`, inverse proven 0-different on a snapshot first) and
this file pins the READ consequence:

  * `memory_claim_withheld` returns the withholding tag for exactly `claim:stale-asserted` and
    `claim:banned-current`; `claim:stale-premise` and `claim:mention-ok` are NOT withheld — those
    rows are the reasoning that rejected the claim, and dropping them would delete the correction's
    own evidence;
  * the resolver's view carries the decision (`claim_withheld`) so the read leg reports what it is
    about to withhold;
  * `layers_for` never renders a withheld row's text, and COUNTS what it withheld in the layer head
    (a silent drop was F-24's original sin; the head position survives the over-budget pass);
  * a CURRENT-scope row is withheld too — scope says whose history it is, not whether it is true;
  * a layer with no tagged rows is unchanged (the healthy case gains no noise, and an untagged row is
    never implied to be approved — only REVIEWED rows carry `claim:reviewed:<date>`).
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.agents.business_planning.agent import BusinessPlanningAgent
from app.models.company import CompanyConfig
from app.models.memory import MemoryEntry
from app.services import memory_service
from app.services.memory_service import (
    CLAIM_BANNED_CURRENT,
    CLAIM_STALE_ASSERTED,
    SCOPE_CURRENT,
    SCOPE_UNSCOPED,
    WITHHELD_CLAIM_TAGS,
    company_id_tag,
    memory_claim_withheld,
)

STALE_FACT = "Our coconut-soy wax is hand-poured in Los Angeles and burns for 55 hours."
SAFE_FACT = "Made-to-order soy candles; printed and shipped from Chatsworth, CA."
WITHHELD_HEAD = "stored memories were withheld from this brief"
FOREIGN_TEXT = "stored memories from a different company were withheld"


def _view(
    index: int,
    *,
    content: str = "",
    scope: str = SCOPE_UNSCOPED,
    tags: list[str] | None = None,
    claim_withheld: str | None = None,
) -> dict[str, Any]:
    """One resolver view in the exact shape `resolve_agent_memories` returns."""
    return {
        "memory_id": index,
        "title": f"memory {index}",
        "content": content or f"fact {index}: " + "candle " * 40,
        "category": "agent_run",
        "source": "social_media",
        "similarity": 1.0 - index / 100.0,
        "age_days": float(index),
        "tags": tags if tags is not None else ["social_media"],
        "pinned": False,
        "company_scope": scope,
        "company_name": "Cinder & Lye" if scope == SCOPE_CURRENT else "",
        "claim_withheld": claim_withheld,
    }


def _prompt(views: list[dict[str, Any]]) -> str:
    return BusinessPlanningAgent().assemble_prompt(
        {"title": "draft the launch copy", "description": "write a tweet"}, {"memories": views}
    ).volatile_suffix


# ── 1. the tag rule (one owner for the vocabulary) ────────────────────────────────────────────────


def test_only_the_two_reviewed_assertion_tags_withhold_a_row() -> None:
    """The exact tag set is the decision: assertions are withheld, reasoning/mentions are kept."""
    assert WITHHELD_CLAIM_TAGS == (CLAIM_STALE_ASSERTED, CLAIM_BANNED_CURRENT)
    assert memory_claim_withheld([CLAIM_STALE_ASSERTED]) == CLAIM_STALE_ASSERTED
    assert memory_claim_withheld(["social_media", CLAIM_BANNED_CURRENT]) == CLAIM_BANNED_CURRENT
    assert memory_claim_withheld([]) is None
    assert memory_claim_withheld(["social_media", "company_id:1"]) is None
    # The two classes that MUST survive into the prompt: a stale premise quoted while rejecting it,
    # and a term named only to refuse or verify it.
    assert memory_claim_withheld(["claim:stale-premise"]) is None
    assert memory_claim_withheld(["claim:mention-ok"]) is None
    assert memory_claim_withheld(["claim:reviewed:2026-10-29"]) is None


# ── 2. the resolver carries the decision on the view ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_cache():
    from app.services import company_service

    company_service.invalidate_company_context()
    yield
    company_service.invalidate_company_context()


async def _company(session: Any) -> CompanyConfig:
    company = CompanyConfig(name="Cinder & Lye", product_type="candle", industry="Home fragrance")
    session.add(company)
    await session.commit()
    return company


async def _memory(
    session: Any, *, title: str, content: str, tags: list[str]
) -> MemoryEntry:
    entry = MemoryEntry(category="agent_run", title=title, content=content,
                        source="social_media", tags=tags)
    session.add(entry)
    await session.commit()
    return entry


@pytest.mark.asyncio
async def test_the_resolver_marks_a_tagged_row_as_withheld(
    async_db_session, monkeypatch
) -> None:
    """The read leg reports WHAT it will withhold — the tag, read off the row, never its prose."""
    company = await _company(async_db_session)
    await _memory(async_db_session, title="social_media: old brand",
                  content=STALE_FACT, tags=["social_media", CLAIM_STALE_ASSERTED])
    kept = await _memory(
        async_db_session, title="social_media: current brand", content=SAFE_FACT,
        tags=["social_media", company_id_tag(int(company.id))],
    )

    rows = (await async_db_session.execute(select(MemoryEntry))).scalars().all()

    async def fake_search(db: Any, text: str, n_results: int = 5,
                          category: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": f"hit-{row.id}", "memory_id": row.id, "title": row.title,
             "content": row.content, "category": row.category, "source": row.source,
             "distance": 0.2}
            for row in rows
        ]

    monkeypatch.setattr(memory_service, "search_memory", fake_search)
    views = {
        view["memory_id"]: view
        for view in await memory_service.resolve_agent_memories(
            async_db_session, agent_type="social_media", query="launch copy", limit=10
        )
    }

    assert views[kept.id]["claim_withheld"] is None
    withheld_views = [v for v in views.values() if v["claim_withheld"] is not None]
    assert len(withheld_views) == 1, views
    assert withheld_views[0]["claim_withheld"] == CLAIM_STALE_ASSERTED
    assert withheld_views[0]["company_scope"] == SCOPE_UNSCOPED


# ── 3. the prompt layer withholds + counts ────────────────────────────────────────────────────────


def test_a_stale_asserted_row_never_reaches_the_prompt_and_is_counted() -> None:
    """RED at wave-5 HEAD: the tag existed (or could be applied) and the read path ignored it."""
    prompt = _prompt([
        _view(1, content=STALE_FACT, claim_withheld=CLAIM_STALE_ASSERTED,
              tags=["social_media", CLAIM_STALE_ASSERTED]),
        _view(2, content=SAFE_FACT),
    ])

    assert "coconut-soy" not in prompt, "a withheld claim's prose must not reach the model"
    assert SAFE_FACT in prompt
    assert WITHHELD_HEAD in prompt
    assert "1 stored memories were withheld from this brief" in prompt
    # The scope key still rides first — ahead of the withholding count (head lines, in order).
    assert prompt.index("SCOPE:") < prompt.index(WITHHELD_HEAD), prompt[:400]


def test_a_current_scope_row_is_withheld_too() -> None:
    """Scope answers WHOSE history it is; the claim review answers whether it may be read as fact.

    Row 225 is exactly this case: `company_id:1` AND `claim:banned-current` — the row the current
    prompt would otherwise treat as this company's own fact (it was written from task 327's brief).
    """
    prompt = _prompt([
        _view(225, content=STALE_FACT, scope=SCOPE_CURRENT,
              tags=["social_media", company_id_tag(1), CLAIM_BANNED_CURRENT],
              claim_withheld=CLAIM_BANNED_CURRENT),
        _view(2, content=SAFE_FACT, scope=SCOPE_CURRENT),
    ])

    assert STALE_FACT not in prompt
    assert "coconut-soy" not in prompt
    assert SAFE_FACT in prompt
    assert "1 stored memories were withheld from this brief" in prompt


def test_premise_and_mention_rows_are_kept() -> None:
    """The two other classes are evidence, not claims: withholding them would delete the correction."""
    premise = "The 55-hour claim in our product story is stale; it must not be repeated."
    mention = "No ASTM or CFR wording may be printed on the label."
    prompt = _prompt([
        _view(214, content=premise, tags=["claim:stale-premise"]),
        _view(206, content=mention, tags=["claim:mention-ok"]),
    ])

    assert premise in prompt
    assert mention in prompt
    assert WITHHELD_HEAD not in prompt


def test_the_withholding_count_survives_an_over_budget_memories_layer() -> None:
    """The count is a HEAD line: the assembler drops trailing entries, never the head (W4-F24-1)."""
    views = [_view(i, content=f"fact {i}: " + "candle " * 700) for i in range(19)]
    views.append(
        _view(999, content=STALE_FACT + " " + "candle " * 700,
              tags=[CLAIM_STALE_ASSERTED], claim_withheld=CLAIM_STALE_ASSERTED)
    )

    assembled = BusinessPlanningAgent().assemble_prompt(
        {"title": "candle launch copy", "description": "write it"}, {"memories": views}
    )

    dropped = [w for w in assembled.warnings if "entries dropped for budget" in w]
    assert dropped, assembled.warnings
    assert WITHHELD_HEAD in assembled.volatile_suffix, (
        "the over-budget pass dropped the withholding count — the model cannot know it happened"
    )


def test_an_untagged_layer_keeps_its_old_shape() -> None:
    """The healthy case gains no noise; an untagged row is not implied to be reviewed."""
    prompt = _prompt([
        _view(1, content=SAFE_FACT, scope=SCOPE_CURRENT, tags=["social_media", company_id_tag(1)]),
    ])

    assert SAFE_FACT in prompt
    assert WITHHELD_HEAD not in prompt
    assert "claim:" not in prompt, "the tag vocabulary never leaks into a clean prompt"
