"""V16/E6 — the capability block rides EVERY agent's assembled context; the orchestrator gets
the full cross-agent map.

WHY this file exists: agents must KNOW what they can do (owner directive: "agents and
orchestrator can see everything they can use/do, know what that is, know how to use it").  The
block is rendered by `app.services.capability_manifest` from the SAME two sources the policy
firewall enforces (`AGENT_CAPABILITIES` + `tool_registry.tools_for`/`TOOL_SPECS`/`TOOL_GUIDANCE`),
so the advertised capability and the executed capability cannot drift apart.  This file pins:

  * the per-agent block (tool names + registry descriptions + when-to-use, action classes,
    channels) lands in the `tool_doctrine` layer, which rides the cacheable stable prefix;
  * the honesty rules survive: an empty allowlist says none, the CLI runtime never claims a
    registry tool it cannot call;
  * the orchestrator additionally carries the FULL cross-agent map (every agent, its action
    classes, channels and registry tools) and no other agent does;
  * the block FITS the doctrine layer's token budget for every agent — a silent truncation of
    the capability block is exactly the false-visibility this feature exists to kill.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.agents import deepseek_runtime, tool_registry
from app.agents import policy_firewall as pf
from app.agents.base_agent import BasePolsiaAgent


class _ProbeOutput(BaseModel):
    """A minimal output schema so `_outcome_contract` renders — this file reads DOCTRINE text.

    (`company_planner` has no `schemas.agent_outputs` entry — its real agent sets
    `output_schema = PlannerProposalDraft` — so a schema-less probe would raise on render.)
    """

    summary: str
    status: str = "ok"


def _probe(agent_type: str) -> BasePolsiaAgent:
    class _Probe(BasePolsiaAgent):
        output_schema = _ProbeOutput

        def run(self, task, context):
            return {"summary": "probe"}

    _Probe.agent_type = agent_type
    return _Probe()


def _doctrine(agent_type: str, *, deepseek: bool, monkeypatch) -> str:
    monkeypatch.setattr(deepseek_runtime, "available", lambda: deepseek)
    return _probe(agent_type).layers_for(
        {"title": "t"}, {"company": {"name": "Co"}}
    )["tool_doctrine"].text


def _spec_description(name: str) -> str:
    return next(
        spec["function"]["description"]
        for spec in tool_registry.TOOL_SPECS
        if spec["function"]["name"] == name
    )


# ── the per-agent block ──────────────────────────────────────────────────────────────────────

def test_the_block_carries_action_classes_channels_and_tool_descriptions(monkeypatch):
    doctrine = _doctrine("finance", deepseek=True, monkeypatch=monkeypatch)

    for action_class in pf.AGENT_CAPABILITIES["finance"].action_classes:
        assert str(action_class) in doctrine, f"finance's action class {action_class} must be named"
    assert "stripe" in doctrine, "finance's real channel must be named"
    assert _spec_description("macro_snapshot") in doctrine, (
        "the registry's own description of the tool must ride the block"
    )
    assert _spec_description("pricing_catalog") in doctrine
    assert "web_fetch" not in doctrine, "a tool finance cannot call must not be named"


def test_the_block_states_none_for_an_empty_registry_allowlist(monkeypatch):
    # V29: social_media now holds signals_read; ads_management is the empty-registry agent.
    doctrine = _doctrine("ads_management", deepseek=True, monkeypatch=monkeypatch)

    assert "google_ads" in doctrine and "meta_ads" in doctrine, (
        "ads_management's real channels are the visibility this feature adds"
    )
    assert "external_reversible" in doctrine
    assert "none" in doctrine


def test_the_phone_tool_rides_the_block_exactly_where_the_allowlist_grants_it(monkeypatch):
    """V20/L2 — the owner's phone is advertised with the registry's OWN description.

    The capability block is DERIVED from `AGENT_CAPABILITIES` + `TOOL_SPECS`, so this is also
    the proof that granting `phone_read` to two agents changed their prompt truth without a
    second hand-copy anywhere: the two holders name it (description included), and an agent
    outside the allowlist never does.  The orchestrator also carries the fleet map, which names
    every agent's tools — that is why it is checked only as a holder here.
    """
    for agent_type in ("orchestrator", "customer_support"):
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        assert "phone_read" in doctrine, f"{agent_type} holds phone_read — it must be named"
        assert _spec_description("phone_read") in doctrine, (
            "the registry's own description of phone_read must ride the block"
        )
    for agent_type in ("social_media", "business_planning", "finance", "competitor_research"):
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        assert "phone_read" not in doctrine, (
            f"{agent_type} cannot call phone_read — it must not be named"
        )


def test_the_v24_tools_ride_the_block_exactly_where_the_allowlist_grants_them(monkeypatch):
    """V24/L2 — `web_search` and the three code tools are advertised with the registry's OWN
    descriptions to the ONE agent that holds each, and to no other agent.

    The block is DERIVED (`AGENT_CAPABILITIES` + `TOOL_SPECS`), so this is also the proof that
    the V24 allowlist grew the prompt truth without a second hand-copy anywhere.  The search
    line must also be honest about WHEN to use it (discovery) versus `web_fetch` (a known URL).
    """
    research = _doctrine("competitor_research", deepseek=True, monkeypatch=monkeypatch)
    assert "web_search" in research
    assert _spec_description("web_search") in research
    assert "discovery" in research.lower()

    code = _doctrine("code_generation", deepseek=True, monkeypatch=monkeypatch)
    for name in ("code_write", "code_read", "code_list"):
        assert name in code, f"code_generation holds {name} — it must be named"
        assert _spec_description(name) in code

    # The orchestrator is excluded HERE on purpose: it carries the fleet map, which legitimately
    # names every agent's tools (that is the planner's material — pinned elsewhere).  Its OWN
    # toolbox is asserted through `tools_for` in the allowlist pins.
    for agent_type in ("social_media", "business_planning", "finance"):
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        assert "web_search" not in doctrine, f"{agent_type} does not hold web_search"
        assert "code_write" not in doctrine, f"{agent_type} does not hold code_write"


def test_the_v25_github_tools_ride_the_block_exactly_where_the_allowlist_grants_them(monkeypatch):
    """V25/L2 — the source-control quartet is advertised with the registry's OWN descriptions
    to the ONE agent that holds it, and to no other agent.

    The block is DERIVED (`AGENT_CAPABILITIES` + `TOOL_SPECS`), so this is also the proof that
    the V25 allowlist grew the prompt truth without a second hand-copy anywhere.  The commit
    line must also state WHEN it is the right tool: source history, not scratch work (the local
    workspace), and the dry-run default.
    """
    code = _doctrine("code_generation", deepseek=True, monkeypatch=monkeypatch)
    for name in ("github_repo", "github_branch", "github_commit", "github_pr"):
        assert name in code, f"code_generation holds {name} — it must be named"
        assert _spec_description(name) in code
    assert "dry_run" in code
    assert "workspace" in code.lower(), "the local-vs-GitHub choice must be stated"

    for agent_type in ("social_media", "business_planning", "finance", "email_outreach"):
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        for name in ("github_repo", "github_branch", "github_commit", "github_pr"):
            assert name not in doctrine, f"{agent_type} does not hold {name}"


def test_the_block_names_the_channels_of_every_agent(monkeypatch):
    for agent_type, cap in pf.AGENT_CAPABILITIES.items():
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        for channel in cap.channels:
            assert channel in doctrine, f"{agent_type}: channel {channel!r} must be named"


# ── the orchestrator's full cross-agent map ──────────────────────────────────────────────────

def test_orchestrator_carries_the_full_cross_agent_map(monkeypatch):
    doctrine = _doctrine("orchestrator", deepseek=True, monkeypatch=monkeypatch)

    for agent_type in pf.AGENT_CAPABILITIES:
        assert agent_type in doctrine, f"the map must name {agent_type}"
    assert "github" in doctrine, "code_generation's channel rides the map"
    assert "web_fetch" in doctrine, "competitor_research's tool rides the map"
    assert "tiktok" in doctrine, "social_media's channels ride the map"


def test_only_the_orchestrator_carries_the_fleet_map(monkeypatch):
    for agent_type in pf.AGENT_CAPABILITIES:
        if agent_type == "orchestrator":
            continue
        doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
        assert "FLEET CAPABILITY MAP" not in doctrine, (
            f"{agent_type} must not carry the fleet map — the orchestrator plans, agents act"
        )


# ── the CLI runtime stays honest ─────────────────────────────────────────────────────────────

def test_cli_runtime_block_never_claims_a_registry_tool(monkeypatch):
    doctrine = _doctrine("competitor_research", deepseek=False, monkeypatch=monkeypatch)

    assert "WebFetch" in doctrine, "the CLI runtime keeps its own tool claim"
    assert "web_fetch" not in doctrine, "a registry tool the CLI runtime cannot call must stay out"
    assert "read" in doctrine, "the action classes are runtime-independent truth"


# ── assembled context + budget fit ───────────────────────────────────────────────────────────

def test_the_block_rides_the_cacheable_stable_prefix(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    assembled = _probe("finance").assemble_prompt({"title": "t"}, {"company": {"name": "Co"}})

    assert _spec_description("macro_snapshot") in assembled.stable_prefix, (
        "the capability block is stable doctrine — it belongs in the cacheable prefix"
    )
    assert _spec_description("macro_snapshot") in assembled.full_prompt


def test_no_agent_truncates_its_capability_block(monkeypatch):
    """The doctrine layer budget must FIT every agent's block — truncation is named if not."""
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    for agent_type in pf.AGENT_CAPABILITIES:
        assembled = _probe(agent_type).assemble_prompt({"title": "t"}, {})
        assert not any(w.startswith("tool_doctrine") for w in assembled.warnings), (
            f"{agent_type}: the capability block overflowed its layer budget: {assembled.warnings}"
        )


@pytest.mark.parametrize("agent_type", sorted(pf.AGENT_CAPABILITIES))
def test_every_agent_gets_a_capability_block(agent_type, monkeypatch):
    doctrine = _doctrine(agent_type, deepseek=True, monkeypatch=monkeypatch)
    assert "CAPABILITY MANIFEST" in doctrine, f"{agent_type} must carry its capability block"
