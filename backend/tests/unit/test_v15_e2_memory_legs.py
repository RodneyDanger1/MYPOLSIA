"""4-05 (a)(b) (MEMORY family, S5-02, #6 LOCKED) — the agent memory read/write legs — RED-first.

BUG-DIGEST §C8 measured: the read leg was PLUMBED but unused (`base_agent.layers_for` renders a
real `memories` layer with `rank_memories`; ALL 10 `collect_structured(...)` sites passed none),
and the write leg was DEAD (`store_memory`'s only production caller was the owner API route).
Pinned here:

  * the base seam: `context_memories(context)` reads the worker-resolved list off
    `context["memories"]` (junk filtered, never a crash), and `assemble_prompt`/
    `collect_structured` default to it — EVERY one of the 10 agents' assembled prompts carries
    the seeded memory (asserted per agent, not by grep);
  * an explicit `memories=` argument still wins (no hidden override);
  * the resolver `memory_service.resolve_agent_memories` maps search hits into the exact
    prompt-view shape `layers_for` consumes (similarity from distance, tags/age enriched from
    the row), and its output really lands in a real agent's assembled prompt (the read-leg seam);
  * the write helper `memory_service.remember_run` persists exactly ONE honest, run-derived
    memory (the run's own summary — never an invented fact), keeps the dual-write's vector half,
    and SKIPS BY NAME (E_VALIDATION) when there is no summary to remember.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from sqlalchemy import select

from app.agents import base_agent
from app.models.memory import MemoryEntry
from app.services import memory_service

TEN_AGENTS = (
    ("app.agents.ads_management.agent", "AdsManagementAgent"),
    ("app.agents.business_planning.agent", "BusinessPlanningAgent"),
    ("app.agents.code_generation.agent", "CodeGenerationAgent"),
    ("app.agents.company_planner.agent", "CompanyPlannerAgent"),
    ("app.agents.competitor_research.agent", "CompetitorResearchAgent"),
    ("app.agents.customer_support.agent", "CustomerSupportAgent"),
    ("app.agents.email_outreach.agent", "EmailOutreachAgent"),
    ("app.agents.finance.agent", "FinanceAgent"),
    ("app.agents.orchestrator.agent", "OrchestratorAgent"),
    ("app.agents.social_media.agent", "SocialMediaAgent"),
)

MEMORY_CONTENT = "v15-memory-leg-unique-content-xyz"


def _seeded_memories() -> list[dict[str, Any]]:
    return [{
        "memory_id": 7,
        "title": "Referral loop",
        "content": MEMORY_CONTENT,
        "similarity": 0.9,
        "age_days": 1.5,
        "tags": ["growth"],
        "pinned": False,
    }]


# ── 1. the base seam: context memories reach every agent's prompt ──────────────────────────────


def test_context_memories_reads_the_worker_resolved_list_and_filters_junk() -> None:
    assert base_agent.context_memories({"memories": [{"title": "a"}, "junk", 3]}) == [
        {"title": "a"}
    ]
    assert base_agent.context_memories({}) == []
    assert base_agent.context_memories(None) == []
    assert base_agent.context_memories({"memories": "nope"}) == []


def test_every_agents_assembled_prompt_carries_the_seeded_memories() -> None:
    """All 10 agents, through the BASE seam — the worker-resolved context reaches the layer."""
    for module_path, class_name in TEN_AGENTS:
        agent = getattr(import_module(module_path), class_name)()
        assembled = agent.assemble_prompt({"title": "plan the morning"},
                                          {"memories": _seeded_memories()})
        assert MEMORY_CONTENT in assembled.volatile_suffix, class_name


def test_an_explicit_memories_argument_wins_over_the_context() -> None:
    agent = getattr(import_module(TEN_AGENTS[0][0]), TEN_AGENTS[0][1])()
    assembled = agent.assemble_prompt({"title": "t"}, {"memories": _seeded_memories()},
                                      memories=[])
    assert MEMORY_CONTENT not in assembled.volatile_suffix


# ── 2. the resolver (read leg) ─────────────────────────────────────────────────────────────────


async def test_resolver_maps_search_hits_into_rankable_prompt_views(
    async_db_session, mock_chroma, monkeypatch,
) -> None:
    entry = await memory_service.store_memory(
        async_db_session, category="strategy", title="Referral loop",
        content="work the referral loop weekly", tags=["growth"])
    entry_id = entry.id

    async def fake_search(db: Any, query: str, n_results: int = 5,
                          category: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "hit-1", "memory_id": entry_id, "title": "Referral loop",
                 "content": "work the referral loop weekly", "category": "strategy",
                 "source": None, "distance": 0.25}]

    monkeypatch.setattr(memory_service, "search_memory", fake_search)

    views = await memory_service.resolve_agent_memories(
        async_db_session, agent_type="social_media", query="referrals", limit=5)

    assert len(views) == 1
    view = views[0]
    assert view["memory_id"] == entry_id
    assert view["title"] == "Referral loop"
    assert view["content"] == "work the referral loop weekly"
    assert view["similarity"] == 0.75          # 1 - distance, the ranker's own input
    assert view["tags"] == ["growth"]          # enriched from the row
    assert view["age_days"] >= 0.0
    assert view["pinned"] is False


async def test_resolver_output_lands_in_a_real_agents_assembled_prompt(
    async_db_session, mock_chroma, monkeypatch,
) -> None:
    entry = await memory_service.store_memory(
        async_db_session, category="company", title="Pricing",
        content="resolver-prompt-seam-content-abc")
    entry_id = entry.id

    async def fake_search(db: Any, query: str, n_results: int = 5,
                          category: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "h", "memory_id": entry_id, "title": "Pricing",
                 "content": "resolver-prompt-seam-content-abc", "category": "company",
                 "source": None, "distance": 0.1}]

    monkeypatch.setattr(memory_service, "search_memory", fake_search)

    views = await memory_service.resolve_agent_memories(
        async_db_session, agent_type="finance", query="revenue", limit=3)

    from app.agents.finance.agent import FinanceAgent

    assembled = FinanceAgent().assemble_prompt({"title": "revenue"}, {"memories": views})
    assert "resolver-prompt-seam-content-abc" in assembled.volatile_suffix


# ── 3. the write helper (write leg) ────────────────────────────────────────────────────────────


async def test_remember_run_persists_one_honest_run_memory(async_db_session, mock_chroma) -> None:
    outcome = await memory_service.remember_run(
        async_db_session,
        agent_type="finance",
        task={"title": "Daily revenue snapshot"},
        result={"summary": "Revenue reconciled: net 6000 cents across 2 charges"},
    )

    assert outcome["ok"] is True
    entry = (await async_db_session.execute(
        select(MemoryEntry).where(MemoryEntry.id == outcome["memory_id"]))).scalars().one()
    assert entry.category == "agent_run"
    assert entry.source == "finance"
    assert "Daily revenue snapshot" in entry.title
    assert "6000 cents" in entry.content
    assert entry.chroma_id, "the dual write keeps the vector half"


async def test_remember_run_skips_by_name_when_there_is_no_summary(
    async_db_session, mock_chroma,
) -> None:
    """No summary = nothing honest to remember: a NAMED skip, never an invented entry."""
    outcome = await memory_service.remember_run(
        async_db_session, agent_type="finance", task=None, result={"status": "ok"})

    assert outcome["ok"] is False
    assert outcome["error"]["code"] == "E_VALIDATION"
    assert "summary" in outcome["error"]["message"]
    assert outcome["error"]["how_to_fix"]


async def test_remember_run_never_raises_on_a_dead_vector_store(
    async_db_session, mock_chroma, monkeypatch,
) -> None:
    """A chroma outage is a RECORDED failure (`{"ok": False, "error": {...}}`), not a killed run —
    the worker's artifact step reports, the run itself stays completed."""
    from app.core.errors import E_STORAGE, PolsiaError

    async def broken_store(*args: Any, **kwargs: Any) -> Any:
        raise PolsiaError(E_STORAGE, "vector store down", how_to_fix="restart chroma")

    monkeypatch.setattr(memory_service, "store_memory", broken_store)

    outcome = await memory_service.remember_run(
        async_db_session, agent_type="finance", task={"title": "t"},
        result={"summary": "something real happened"})

    assert outcome["ok"] is False
    assert outcome["error"]["code"] == E_STORAGE
