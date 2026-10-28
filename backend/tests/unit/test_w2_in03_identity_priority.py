"""W2 / IN-03 — at shipped budgets, the identity layer must keep the RULES and the CONTRACT.

The defect (intelligence hunter, PRE-EXISTING at HEAD): `settings.planner_context_token_budget`
is 12,000 and `budgets_for_total()` gives the identity layer a ~744-token share.  The identity
composition put the agent's ROLE PROSE first and the NON-NEGOTIABLE RULES + output contract last,
and `assemble()` truncates from the END — so at shipped settings `finance` and `email_outreach`
(the two longest role prompts) lost the universal doctrine (untrusted-is-DATA, never-invent,
autonomous-operator, status=refused) and the explicit required-field contract that tells the model
which JSON keys it must emit.  The assembled prompts contained neither string (measured).

The fix is an ORDERING change, not a budget change: the rules and the contract are rendered FIRST,
so a truncation can only ever cut the role PROSE.  The truncation marker stays (a cut is always
explicit).  No layer was added and no weight moved — this file also pins that the layer shape is
still the shipped one.
"""
from __future__ import annotations

import pytest

from app.agents import deepseek_runtime
from app.agents.base_agent import CONTEXT_LAYER_WEIGHTS, TRUNCATION_MARKER
from app.agents.context_assembler import DEFAULT_BUDGETS, budgets_for_total, measure_tokens
from app.agents.crew_factory import AGENT_MAP, get_agent_class
from app.config import settings

RULES_HEADING = "NON-NEGOTIABLE RULES"
CONTRACT_LINE = "- Your output contract:"


def _section(prompt: str, name: str) -> str:
    """One rendered layer's text (`<name>\\n...\\n</name>`), for a layer-scoped assertion."""
    opener = f"<{name}>\n"
    start = prompt.index(opener) + len(opener)
    return prompt[start:prompt.index(f"\n</{name}>", start)]


@pytest.fixture(autouse=True)
def _deepseek_runtime(monkeypatch):
    """The shipped path with a key configured: that is the runtime every runner uses."""
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)


def _shipped_identity_budget() -> int:
    budgets = budgets_for_total(
        settings.planner_context_token_budget, weights=CONTEXT_LAYER_WEIGHTS
    )
    return budgets["identity"]


def _every_agent() -> list[tuple[str, type]]:
    """Every shipped `BasePolsiaAgent` subclass: `AGENT_MAP`'s nine PLUS `company_planner`.

    REPAIR NOTE (F1, 2026-10-28 — the coder was killed after the fix but before this pin was
    widened): `AGENT_MAP` is the WORKER's factory registry and holds nine agents.
    `planner_service` does not go through it — it imports `CompanyPlannerAgent` directly
    (`app/services/planner_service.py:51`, `PLANNER_AGENT_TYPE = "company_planner"`) — so a pin
    iterating only `AGENT_MAP` covered 9 of the 10 instantiable agents.  Measured: the missing
    agent's identity layer is 860 tokens against the shipped 744-token budget, so it truncates
    exactly like `finance` / `email_outreach`; without this line the claim "pin covers ALL agents"
    was false for one shipped agent.
    """
    from app.agents.company_planner.agent import CompanyPlannerAgent

    agents: list[tuple[str, type]] = [
        (agent_type, get_agent_class(agent_type)) for agent_type in AGENT_MAP
    ]
    agents.append(("company_planner", CompanyPlannerAgent))
    return agents


def test_the_identity_layer_puts_rules_and_output_contract_first():
    """Ordering is the fix: both blocks precede the role prose in EVERY agent's composition."""
    for agent_type, agent_class in _every_agent():
        agent = agent_class()
        identity = agent.layers_for({"title": "t"}, {})["identity"].body()
        role = agent.role_prompt
        assert role, f"{agent_type} must carry a role prompt for this pin to mean anything"
        assert identity.startswith(RULES_HEADING), (
            f"{agent_type}: the identity layer starts with role prose, so a budget cut removes "
            "the rules (IN-03)"
        )
        assert CONTRACT_LINE in identity
        assert identity.index(CONTRACT_LINE) < identity.index(role), (
            f"{agent_type}: the output contract must precede the role prose"
        )


def test_every_agent_keeps_both_blocks_in_the_ASSEMBLED_prompt_at_shipped_budgets():
    """The acceptance pin: assembling ANY agent at the shipped budget keeps both blocks intact."""
    budget = _shipped_identity_budget()
    assert settings.planner_context_token_budget == 12_000, (
        "this pin is written against the shipped 12,000-token planner budget"
    )
    for agent_type, agent_class in _every_agent():
        assembled = agent_class().assemble_prompt({"title": "t"}, {})
        assert RULES_HEADING in assembled.full_prompt, (
            f"{agent_type}: the NON-NEGOTIABLE RULES were dropped by the identity budget"
        )
        assert CONTRACT_LINE in assembled.full_prompt, (
            f"{agent_type}: the output contract was dropped by the identity budget"
        )
        section = _section(assembled.full_prompt, "identity")
        assert measure_tokens(section) <= budget + 1, (
            f"{agent_type}: the identity layer exceeded its own budget"
        )


def test_a_truncated_identity_cuts_the_prose_and_states_the_cut():
    """The two long-role agents still truncate — but only prose, and the marker is explicit."""
    budget = _shipped_identity_budget()
    truncated: list[str] = []
    for agent_type, agent_class in _every_agent():
        agent = agent_class()
        full_identity = agent.layers_for({"title": "t"}, {})["identity"].body()
        if measure_tokens(full_identity) <= budget:
            continue
        truncated.append(agent_type)
        assembled = agent.assemble_prompt({"title": "t"}, {})
        section = _section(assembled.full_prompt, "identity")
        assert TRUNCATION_MARKER.strip() in section, (
            f"{agent_type}: an identity cut must leave the explicit truncation marker"
        )
        cut = section.index(TRUNCATION_MARKER.strip())
        assert section.index(RULES_HEADING) < cut
        assert section.index(CONTRACT_LINE) < cut, (
            f"{agent_type}: the output contract was cut by the identity budget"
        )
        assert f"identity: hard-truncated to ~{budget} tokens" in assembled.warnings

    assert "finance" in truncated and "email_outreach" in truncated, (
        "the shipped budget must still truncate the two long-role agents, or this pin is vacuous"
    )
    assert "company_planner" in truncated, (
        "the directly-dispatched agent truncates too, so its inclusion makes this pin mean something"
    )


def test_the_layer_shape_and_weights_are_unchanged():
    """No new layer and no budget move: the IN-03 fix is ordering only (the pins stay live)."""
    assert sum(CONTEXT_LAYER_WEIGHTS.values()) == 25_800
    assert set(CONTEXT_LAYER_WEIGHTS) == set(DEFAULT_BUDGETS)
    budgets = budgets_for_total(settings.planner_context_token_budget,
                                weights=CONTEXT_LAYER_WEIGHTS)
    assert budgets["identity"] == 744
