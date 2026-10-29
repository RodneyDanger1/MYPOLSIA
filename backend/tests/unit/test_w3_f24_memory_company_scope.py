"""F-24 (wave 3) — memory is company-blind; a previous company's facts come back as "ours".

Measured defect (VISUALS-2 F-24, live): the memory layer still carried wave-1's Copper & Pine run
summaries ("coconut-soy wax, wood wick, 8 oz at $28, 55-hour burn") after the owner had replaced the
company with a candle/soap brand.  Run 312 — a run FOR the new company — then wrote *"its published
35-hour burn also sits below the 55-hour claim in our current product story"*: a claim the current
company had never made.  Nothing on `/memory` or `/settings` showed the provenance.

`remember_run` wrote the summary with the agent type as its only tag, and `resolve_agent_memories`
returned every row for every company.  Pinned here:

  * a new run memory is TAGGED with the company it was written for;
  * a row tagged with ANOTHER company is `foreign` and is WITHHELD from the prompt (and the
    withholding is COUNTED in the layer, never silent);
  * a row with no company tag (everything written before this fix) is `unscoped`, rendered with
    `[UNSCOPED]` and a one-line key that forbids presenting it as a fact about this company;
  * a current-company row is rendered `[CURRENT COMPANY]` with no note — the healthy case is
    unchanged in substance;
  * the classification is read off the rows' own tags, never inferred from their text.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.agents.business_planning.agent import BusinessPlanningAgent
from app.models.company import CompanyConfig
from app.models.memory import MemoryEntry
from app.services import company_service, memory_service
from app.services.memory_service import (
    SCOPE_CURRENT,
    SCOPE_FOREIGN,
    SCOPE_UNSCOPED,
    company_id_tag,
    memory_company_scope,
)

COPPER_AND_PINE = "coconut-soy wax, wood wick, hand-poured in the US, 8 oz at $28, 55-hour burn"
HEARTH_FACT = "Hearth & Lather gift box: 6 oz soy candle and a 4 oz soap bar"


@pytest.fixture(autouse=True)
def _clear_cache():
    company_service.invalidate_company_context()
    yield
    company_service.invalidate_company_context()


async def _company(session, name: str = "Hearth & Lather", product: str = "candle") -> CompanyConfig:
    company = CompanyConfig(name=name, product_type=product, industry="Home fragrance")
    session.add(company)
    await session.commit()
    return company


async def _memory(session, *, title: str, content: str, tags: list[str]) -> MemoryEntry:
    entry = MemoryEntry(category="agent_run", title=title, content=content,
                        source="social_media", tags=tags)
    session.add(entry)
    await session.commit()
    return entry


async def _views(session, monkeypatch: Any, *, query: str = "product story") -> list[dict]:
    """The resolver's own output — the read leg, with the vector search replaced by the rows."""
    rows = (await session.execute(select(MemoryEntry))).scalars().all()

    async def fake_search(db: Any, text: str, n_results: int = 5,
                          category: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": f"hit-{row.id}", "memory_id": row.id, "title": row.title,
             "content": row.content, "category": row.category, "source": row.source,
             "distance": 0.2}
            for row in rows
        ]

    monkeypatch.setattr(memory_service, "search_memory", fake_search)
    return await memory_service.resolve_agent_memories(session, agent_type="business_planning",
                                                       query=query, limit=10)


def test_the_scope_classifier_reads_tags_and_never_text() -> None:
    assert memory_company_scope([], 7) == SCOPE_UNSCOPED
    assert memory_company_scope(["social_media"], 7) == SCOPE_UNSCOPED
    assert memory_company_scope(["social_media", company_id_tag(7)], 7) == SCOPE_CURRENT
    assert memory_company_scope([company_id_tag(3)], 7) == SCOPE_FOREIGN
    assert memory_company_scope([company_id_tag(3)], None) == SCOPE_FOREIGN, (
        "with no company row, a tagged memory cannot belong to 'us'"
    )
    # The text is irrelevant: a row that TALKS about this company but is tagged to another one is
    # still foreign (provenance is the tag, not the prose).
    assert memory_company_scope([company_id_tag(3)], 7) == SCOPE_FOREIGN


@pytest.mark.asyncio
async def test_a_new_run_memory_is_tagged_with_the_company_it_was_written_for(
    async_db_session, mock_chroma,
) -> None:
    company = await _company(async_db_session)

    outcome = await memory_service.remember_run(
        async_db_session,
        agent_type="social_media",
        task={"title": "Provisional brand copy"},
        result={"summary": HEARTH_FACT},
    )

    entry = (await async_db_session.execute(
        select(MemoryEntry).where(MemoryEntry.id == outcome["memory_id"]))).scalars().one()
    assert company_id_tag(int(company.id)) in entry.tags, entry.tags
    assert "Hearth & Lather" in " ".join(entry.tags), entry.tags
    assert memory_company_scope(list(entry.tags), int(company.id)) == SCOPE_CURRENT


@pytest.mark.asyncio
async def test_the_resolver_labels_each_row_current_foreign_or_unscoped(
    async_db_session, monkeypatch,
) -> None:
    """RED-1: before F-24 every view was the same shape and no company identity existed."""
    company = await _company(async_db_session)
    await _memory(async_db_session, title="social_media: Copper & Pine launch",
                  content=COPPER_AND_PINE, tags=["social_media"])
    mine = await _memory(async_db_session, title="social_media: Hearth & Lather box",
                         content=HEARTH_FACT,
                         tags=["social_media", company_id_tag(int(company.id))])

    views = {view["memory_id"]: view for view in await _views(async_db_session, monkeypatch)}

    scopes = {view["memory_id"]: view["company_scope"] for view in views.values()}
    assert scopes[mine.id] == SCOPE_CURRENT, scopes
    assert scopes[[k for k in scopes if k != mine.id][0]] == SCOPE_UNSCOPED, scopes


@pytest.mark.asyncio
async def test_another_companys_memory_is_withheld_from_the_prompt_and_counted(
    async_db_session, monkeypatch,
) -> None:
    """The live harm: Copper & Pine facts reaching the new company's brief as 'our current story'."""
    company = await _company(async_db_session)
    # A row explicitly tagged to a DIFFERENT company id (e.g. written under the previous profile).
    await _memory(async_db_session, title="social_media: Copper & Pine launch",
                  content=COPPER_AND_PINE,
                  tags=["social_media", company_id_tag(int(company.id) + 5)])
    await _memory(async_db_session, title="social_media: Hearth & Lather box", content=HEARTH_FACT,
                  tags=["social_media", company_id_tag(int(company.id))])

    views = await _views(async_db_session, monkeypatch)
    assert any(view["company_scope"] == SCOPE_FOREIGN for view in views), (
        "the resolver must CLASSIFY the foreign row, not hide it"
    )

    assembled = BusinessPlanningAgent().assemble_prompt(
        {"title": "compare candle vs soap"}, {"memories": views}
    )

    assert COPPER_AND_PINE not in assembled.volatile_suffix, (
        "another company's fact must not reach this company's prompt"
    )
    assert HEARTH_FACT in assembled.volatile_suffix
    assert "1 stored memories from a different company were withheld" in assembled.volatile_suffix


@pytest.mark.asyncio
async def test_an_unscoped_memory_is_labelled_and_cannot_read_as_a_current_fact(
    async_db_session, monkeypatch,
) -> None:
    """Every row written before this fix is unscoped — the label is what stops the poisoning."""
    await _company(async_db_session)
    await _memory(async_db_session, title="social_media: Copper & Pine launch",
                  content=COPPER_AND_PINE, tags=["social_media"])

    views = await _views(async_db_session, monkeypatch)
    assembled = BusinessPlanningAgent().assemble_prompt(
        {"title": "compare candle vs soap"}, {"memories": views}
    )

    assert "[UNSCOPED]" in assembled.volatile_suffix
    assert "never present one as a fact about this company" in assembled.volatile_suffix
    # No ENTRY is tagged as ours (the key line above mentions the tag; an entry line would lead
    # with it after "- ").
    assert "- [CURRENT COMPANY]" not in assembled.volatile_suffix


@pytest.mark.asyncio
async def test_a_current_company_memory_is_tagged_ours_and_carries_no_scope_note(
    async_db_session, monkeypatch,
) -> None:
    """The healthy case is unchanged in substance: our own memory, no warning noise."""
    company = await _company(async_db_session)
    await _memory(async_db_session, title="social_media: Hearth & Lather box", content=HEARTH_FACT,
                  tags=["social_media", company_id_tag(int(company.id))])

    views = await _views(async_db_session, monkeypatch)
    assembled = BusinessPlanningAgent().assemble_prompt(
        {"title": "compare candle vs soap"}, {"memories": views}
    )

    assert "[CURRENT COMPANY]" in assembled.volatile_suffix
    assert "SCOPE:" not in assembled.volatile_suffix, "no key needed when everything is ours"


def test_a_caller_with_no_scope_information_gets_the_unscoped_label() -> None:
    """A bare `context["memories"]` list (a test, a new producer) cannot imply 'ours'."""
    assembled = BusinessPlanningAgent().assemble_prompt(
        {"title": "t"}, {"memories": [{"title": "Referral loop", "content": "some fact"}]}
    )
    assert "[UNSCOPED]" in assembled.volatile_suffix
    assert "Referral loop" in assembled.volatile_suffix
