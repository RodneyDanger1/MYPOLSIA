"""V11/W2 — truth-fix (capability claims) + macro-context injection.

TRUTH-FIX (brief items 6): `base_agent` rendered "Tools available to you: WebFetch, WebSearch"
for competitor_research even when the run dispatches to the DeepSeek runtime, which cannot CALL
tools (tool-calling is V12).  The rendered claim must match the runtime that will run: tools
listed under the CLI runtime; an explicit no-tool-calls statement under DeepSeek.  README's web
-search bullet gets the same caveat (worded, not wired — wiring is V12).

MACRO CONTEXT (brief item 7): `company_service.get_full_context` carries one `macro` key fed
from the EXISTING free-data sources (World Bank / FRED / frankfurter via `economics_service`),
cache-only (no network on the hot context path), with sourced facts or an EXPLICIT
unavailable/degraded answer — never fabricated values.  Covered: present, unavailable, shape.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.services import economics_service

README = Path(__file__).resolve().parents[3] / "README.md"


def _prompt_text(layers: dict) -> str:
    return "\n".join(getattr(layer, "text", str(layer)) for layer in layers.values())


# ── truth-fix: the tools claim follows the runtime ───────────────────────────────────────────

def test_tools_claim_is_honest_under_the_deepseek_runtime(monkeypatch):
    """V12/W-B update (this test pinned the pre-tool-loop truth): the DeepSeek runtime CAN call
    tools now (the W-A tool loop + the W-B registry).  For an agent whose REGISTRY allowlist is
    non-empty the doctrine names the real tools (`web_fetch`); the CLI-tool names (WebFetch /
    WebSearch) are still NOT the DeepSeek claim.

    V24/L2 RE-PIN: `web_search` DOES ship now (DeepSeek Search API) and competitor_research
    holds it — so the doctrine must name it too.  The pre-V24 assertion here ("no search
    provider ships — the name is absent") was true then and is false now; that reversal is the
    V24 plan's frozen interface, not a drift."""
    from app.agents import deepseek_runtime
    from app.agents.competitor_research.agent import CompetitorResearchAgent

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    agent = CompetitorResearchAgent()

    doctrine = agent.layers_for({"title": "research"}, {"company": {"name": "Co"}})[
        "tool_doctrine"
    ].text

    assert "WebFetch" not in doctrine and "WebSearch" not in doctrine, (
        "the CLI tool names are not the DeepSeek runtime's claim"
    )
    assert "web_fetch" in doctrine, "the registry tool the loop can really call is named"
    assert "web_search" in doctrine, "V24/L2: the search provider ships and this agent holds it"


def test_tools_claim_for_a_tool_less_agent_under_deepseek_stays_honest(monkeypatch):
    """An agent with an EMPTY registry allowlist gets no tool claim (the runtime requests no
    tools for it — the unchanged path)."""
    from app.agents import deepseek_runtime
    from app.agents.social_media.agent import SocialMediaAgent

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    doctrine = SocialMediaAgent().layers_for({"title": "post"}, {"company": {"name": "Co"}})[
        "tool_doctrine"
    ].text

    assert "web_fetch" not in doctrine and "none" in doctrine


def test_tools_claim_states_the_tools_under_the_cli_runtime(monkeypatch):
    from app.agents import deepseek_runtime
    from app.agents.competitor_research.agent import CompetitorResearchAgent

    monkeypatch.setattr(deepseek_runtime, "available", lambda: False)
    agent = CompetitorResearchAgent()

    doctrine = agent.layers_for({"title": "research"}, {"company": {"name": "Co"}})[
        "tool_doctrine"
    ].text

    assert "WebFetch" in doctrine, "the CLI runtime CAN call the tools — the claim stays there"


def test_readme_websearch_bullet_cavesats_the_runtime():
    readme = README.read_text(encoding="utf-8")
    bullet_start = readme.index("**Web search for competitor research**")
    bullet = readme[bullet_start : readme.index("\n- **", bullet_start + 10)]
    assert "CLI runtime" in bullet, "the bullet must scope the tool path to the CLI runtime"
    assert "DeepSeek" in bullet, "and state what the DeepSeek runtime does instead"


# ── macro context: present / unavailable / shape ─────────────────────────────────────────────

def _seed_company(db):
    from app.models.company import CompanyConfig

    db.add(CompanyConfig(name="Macro Co"))
    return db.commit()


def _seed_cache(monkeypatch) -> dict:
    fetched_at = "2026-10-14T22:00:00+00:00"
    cache = {
        "inflation": (
            datetime.now(UTC),
            {
                "year": 2024,
                "value": 2.9,
                "source": economics_service.WORLD_BANK_INFLATION_URL,
                "fetched_at": fetched_at,
            },
        ),
        "cpi": (
            datetime.now(UTC),
            {
                "date": "2026-08-01",
                "value": 320.1,
                "source": economics_service.FRED_CPI_CSV_URL,
                "fetched_at": fetched_at,
            },
        ),
        "fx": (
            datetime.now(UTC),
            {
                "usd_base": True,
                "date": "2026-10-14",
                "rates": {"EUR": 0.92},
                "reason": None,
                "source": economics_service.FRANKFURTER_LATEST_URL,
                "fetched_at": fetched_at,
            },
        ),
    }
    monkeypatch.setattr(economics_service, "_CACHE", cache)
    return cache


async def test_full_context_carries_the_macro_snapshot_when_fetched(async_db_session, monkeypatch):
    from app.services.company_service import get_full_context

    await _seed_company(async_db_session)
    _seed_cache(monkeypatch)

    context = await get_full_context(async_db_session, use_cache=False)

    macro = context["macro"]
    assert macro["status"] == "ok"
    assert macro["as_of"] == "2026-10-14T22:00:00+00:00"
    assert macro["cpi"]["value"] == 320.1  # sourced fact, carried verbatim
    assert all(macro[f"{name}_reason"] is None for name in ("inflation", "cpi", "fx"))


async def test_macro_is_explicitly_unavailable_without_a_snapshot(async_db_session, monkeypatch):
    from app.services.company_service import get_full_context

    await _seed_company(async_db_session)
    monkeypatch.setattr(economics_service, "_CACHE", {})

    context = await get_full_context(async_db_session, use_cache=False)

    macro = context["macro"]
    assert macro["status"] == "unavailable"
    assert macro["as_of"] is None
    for name in ("inflation", "cpi", "fx"):
        assert macro[name] is None, "no fabricated values"
        assert macro[f"{name}_reason"], "an unavailable section is NAMED, never silent"


async def test_macro_snapshot_shape_matches_the_economics_contract(async_db_session, monkeypatch):
    from app.services.company_service import get_full_context

    await _seed_company(async_db_session)
    _seed_cache(monkeypatch)

    context = await get_full_context(async_db_session, use_cache=False)

    assert set(context["macro"]) == {
        "status",
        "as_of",
        "inflation",
        "cpi",
        "fx",
        "inflation_reason",
        "cpi_reason",
        "fx_reason",
    }
