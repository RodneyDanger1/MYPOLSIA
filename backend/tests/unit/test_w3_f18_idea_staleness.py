"""F-18 (wave 3) — a stale "confirmed idea" must never reach a prompt as a current fact.

Measured defect (VISUALS-2 F-18, live): `/planner`'s CONFIRMED IDEA read *"AI Marketing Ops
Retainer for SMB Agencies"* while the company profile was a candle/soap brand for gift buyers.
`company_service.build_context_prompt` rendered it under the heading **CONFIRMED IDEA (idea_brief)**
into every prompt the CLI/fallback path builds, so every task was briefed on a different business
than the one it was asked to work on.  Settings had no control for it; the only writer was the
Planner's confirm step, fourteen days and several profile edits earlier.

The fix is provenance, from measured timestamps only.  `company_config.confirmed_idea` carries no
timestamp, but the Planner logs `idea_confirmed` in the SAME transaction as the write, and any later
profile edit moves `company_config.updated_at`.  So:

  * idea confirmed, profile untouched since  -> rendered as CONFIRMED, with the confirmation time;
  * profile edited after the confirmation    -> rendered as PRIOR IDEA history, name only, with an
                                                explicit "do not brief work from this" instruction;
  * idea stored with no confirmation record  -> rendered as an UNVERIFIED candidate;
  * no idea                                  -> nothing rendered.

Pinned here for each state, through the real renderer AND through a real agent's `build_prompt`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.business_planning.agent import BusinessPlanningAgent
from app.models.company import CompanyConfig
from app.models.report import ActivityLog
from app.services import company_service, planner_service
from app.services.company_service import build_context_prompt, get_full_context

NOW = datetime(2026, 10, 28, 22, 0, 0, tzinfo=UTC)
CONFIRMED_AT = NOW - timedelta(days=14)

STALE_IDEA = {
    "id": "idea-4fbfbdc0154d",
    "name": "AI Marketing Ops Retainer for SMB Agencies",
    "one_liner": "Done-for-you AI workflow setup sold as a fixed monthly retainer.",
    "buyer": "Owner/operator of a 2-20 person marketing agency or services SMB.",
    "channel": "Stripe payment link or PayPal invoice.",
    "price_band": [500.0, 2500.0],
}

CANDLE_PROFILE = {
    "name": "Hearth & Lather",
    "mission": "Small-batch candles and soaps for gift buyers.",
    "product_type": "candle",
    "industry": "Home fragrance",
    "target_market": "Gift buyers in the US Midwest.",
}


async def _company(session, *, updated_at: datetime, idea: dict | None = STALE_IDEA) -> CompanyConfig:
    company = CompanyConfig(
        **CANDLE_PROFILE,
        confirmed_idea=idea,
        updated_at=updated_at,
        created_at=updated_at - timedelta(days=30),
    )
    session.add(company)
    await session.commit()
    return company


async def _confirm_record(session, *, when: datetime) -> None:
    session.add(
        ActivityLog(
            agent_type=company_service.IDEA_AGENT_TYPE,
            action=company_service.IDEA_CONFIRMED_ACTION,
            summary="confirmed idea 'AI Marketing Ops Retainer for SMB Agencies'",
            created_at=when,
        )
    )
    await session.commit()


@pytest.fixture(autouse=True)
def _clear_cache():
    """The context cache is engine-keyed and process-wide: every test starts from a cold one."""
    company_service.invalidate_company_context()
    yield
    company_service.invalidate_company_context()


def test_the_planner_constants_this_module_reads_are_planner_services_own():
    """The two literals are duplicated to avoid an import cycle — pinned so they cannot drift."""
    assert company_service.IDEA_AGENT_TYPE == planner_service.PLANNER_AGENT_TYPE
    assert company_service.IDEA_CONFIRMED_ACTION == planner_service.CONFIRMED_ACTION


@pytest.mark.asyncio
async def test_a_profile_edit_after_the_confirmation_makes_the_idea_stale(async_db_session):
    """RED-1: the live shape — idea confirmed 14 days ago, profile edited since."""
    await _company(async_db_session, updated_at=NOW)
    await _confirm_record(async_db_session, when=CONFIRMED_AT)

    context = await get_full_context(async_db_session, use_cache=False)

    state = context["idea_brief_state"]
    assert state["status"] == company_service.IDEA_STALE, state
    assert state["confirmed_at"] is not None and state["profile_updated_at"] is not None
    assert "edited after" in state["reason"], state


@pytest.mark.asyncio
async def test_a_stale_idea_is_not_rendered_as_a_current_fact(async_db_session):
    """The prompt must not brief the old business, and must never call the idea current."""
    await _company(async_db_session, updated_at=NOW)
    await _confirm_record(async_db_session, when=CONFIRMED_AT)

    prompt = build_context_prompt(await get_full_context(async_db_session, use_cache=False))

    assert "CONFIRMED IDEA" not in prompt, prompt
    assert "PRIOR IDEA — NOT THE CURRENT BUSINESS" in prompt, prompt
    assert "do not brief work from this idea" in prompt, prompt
    # The NAME is kept (so the owner/agent can see what the history was)...
    assert "AI Marketing Ops Retainer for SMB Agencies" in prompt
    # ...but none of its detail may be quotable as a current fact.
    assert STALE_IDEA["one_liner"] not in prompt
    assert STALE_IDEA["buyer"] not in prompt
    assert STALE_IDEA["channel"] not in prompt
    assert "500.0-2500.0" not in prompt
    # And the real company is still the company.
    assert "Hearth & Lather" in prompt and "candle" in prompt


@pytest.mark.asyncio
async def test_a_stale_idea_does_not_reach_a_real_agents_prompt_as_a_fact(async_db_session):
    """The agent path (BusinessPlanningAgent.build_prompt) — the seam F-18 named."""
    await _company(async_db_session, updated_at=NOW)
    await _confirm_record(async_db_session, when=CONFIRMED_AT)
    context = await get_full_context(async_db_session, use_cache=False)

    prompt = BusinessPlanningAgent().build_prompt({"title": "compare candles and soaps"}, context)

    assert "CONFIRMED IDEA" not in prompt
    assert "PRIOR IDEA — NOT THE CURRENT BUSINESS" in prompt
    assert STALE_IDEA["one_liner"] not in prompt


@pytest.mark.asyncio
async def test_a_freshly_confirmed_idea_is_still_rendered_as_confirmed(async_db_session):
    """The fix must not demote a genuine, current confirmation (no behaviour loss)."""
    await _company(async_db_session, updated_at=NOW)
    await _confirm_record(async_db_session, when=NOW)

    context = await get_full_context(async_db_session, use_cache=False)
    prompt = build_context_prompt(context)

    assert context["idea_brief_state"]["status"] == company_service.IDEA_CONFIRMED
    assert "CONFIRMED IDEA (idea_brief" in prompt, prompt
    assert "has not been edited since" in prompt
    assert STALE_IDEA["one_liner"] in prompt, "a current idea keeps its detail"
    assert "PRIOR IDEA" not in prompt


@pytest.mark.asyncio
async def test_an_idea_with_no_confirmation_record_is_labelled_unverified(async_db_session):
    """An API-written idea is a CANDIDATE: we cannot know when or against what it was confirmed."""
    await _company(async_db_session, updated_at=NOW)

    context = await get_full_context(async_db_session, use_cache=False)
    prompt = build_context_prompt(context)

    assert context["idea_brief_state"]["status"] == company_service.IDEA_UNVERIFIED
    assert "UNVERIFIED" in prompt, prompt
    assert "CONFIRMED IDEA" not in prompt
    assert "never as a fact about the company above" in prompt


@pytest.mark.asyncio
async def test_no_idea_renders_no_idea_section_and_a_stable_state(async_db_session):
    await _company(async_db_session, updated_at=NOW, idea=None)

    context = await get_full_context(async_db_session, use_cache=False)

    assert context["idea_brief"] is None
    assert context["idea_brief_state"]["status"] == company_service.IDEA_NONE
    assert "IDEA" not in build_context_prompt(context)


@pytest.mark.asyncio
async def test_the_state_is_always_present_for_consumers_that_do_not_check_it(async_db_session):
    """Stable shape: the key exists even with no company row... except an empty context."""
    await _company(async_db_session, updated_at=NOW)
    context = await get_full_context(async_db_session, use_cache=False)
    assert set(context["idea_brief_state"]) >= {"status", "reason", "confirmed_at",
                                                "profile_updated_at"}
    assert (await get_full_context(async_db_session, use_cache=False)) != {}
    # An EMPTY context is still `{}` — the documented no-company shape is unchanged.
    rows = (await async_db_session.execute(select(CompanyConfig))).scalars().all()
    for row in rows:
        await async_db_session.delete(row)
    await async_db_session.commit()
    company_service.invalidate_company_context()
    assert await get_full_context(async_db_session, use_cache=False) == {}
