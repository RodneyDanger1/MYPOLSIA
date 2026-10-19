"""V16/E6 — the capability manifest: DERIVED from the two registry sources, drift-pinned.

WHY this file exists: the capability-visibility family (owner: "agents and orchestrator can see
everything they can use/do, know what that is, know how to use it") promises that "what an agent
can do" is read from ONE place at runtime — `policy_firewall.AGENT_CAPABILITIES` (action classes,
channels, the registry allowlist) plus `tool_registry` (TOOL_SPECS descriptions, TOOL_GUIDANCE,
`tools_for`) — never hand-copied into a manifest, a prompt or an endpoint. This file pins:

  * the manifest derives every field from those two sources (descriptions byte-identical to the
    registry specs);
  * a DRIFT PIN: literal snapshots of the registry tool set, the per-agent tool allowlist and
    every agent's action classes + channels.  Change either registry source and the pin FAILS,
    forcing a deliberate re-pin in the same commit — the same discipline `EXPECTED_ROUTES` uses;
  * the integrity rule: an allowlist naming a tool the registry does not ship raises a NAMED
    config refusal instead of fabricating a capability an agent cannot execute.
"""
from __future__ import annotations

import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.agents.policy_firewall import AgentCapability
from app.core.errors import E_CONFIG, PolsiaError
from app.services import capability_manifest


def _spec_description(name: str) -> str:
    return next(
        spec["function"]["description"]
        for spec in tool_registry.TOOL_SPECS
        if spec["function"]["name"] == name
    )


# ── derivation from the two sources ──────────────────────────────────────────────────────────

def test_manifest_covers_exactly_the_firewall_agents():
    data = capability_manifest.capability_manifest()
    assert [row["agent_type"] for row in data["agents"]] == sorted(pf.AGENT_CAPABILITIES)


def test_every_entry_derives_from_the_two_registries():
    data = capability_manifest.capability_manifest()
    for row in data["agents"]:
        cap = pf.AGENT_CAPABILITIES[row["agent_type"]]
        assert row["tools"] == [
            {"name": name, "description": _spec_description(name)} for name in cap.agent_tools
        ], row["agent_type"]
        assert row["action_classes"] == sorted(str(c) for c in cap.action_classes)
        assert row["channels"] == list(cap.channels)


def test_descriptions_are_byte_identical_to_the_registry_specs():
    data = capability_manifest.capability_manifest()
    finance = next(r for r in data["agents"] if r["agent_type"] == "finance")
    by_name = {tool["name"]: tool["description"] for tool in finance["tools"]}
    assert by_name["macro_snapshot"] == _spec_description("macro_snapshot")
    assert by_name["pricing_catalog"] == _spec_description("pricing_catalog")
    research = next(r for r in data["agents"] if r["agent_type"] == "competitor_research")
    assert research["tools"][0]["description"] == _spec_description("web_fetch")


def test_registry_tools_are_every_spec_sorted_by_name():
    data = capability_manifest.capability_manifest()
    assert data["registry_tools"] == [
        {"name": spec["function"]["name"], "description": spec["function"]["description"]}
        for spec in sorted(tool_registry.TOOL_SPECS, key=lambda s: s["function"]["name"])
    ]


def test_empty_allowlist_renders_an_empty_list_never_a_default_set():
    # V24/L2: code_generation LEFT this list (it now holds the three code tools).
    for agent_type in ("social_media", "email_outreach", "ads_management"):
        assert capability_manifest.tool_entries_for(agent_type) == [], agent_type


def test_the_v24_tools_are_visible_exactly_where_the_allowlist_grants_them():
    """V24/L2 — `web_search` and the code tools ride the same derivation as every other tool.

    `web_search` appears in the manifest for competitor_research ONLY, the three code tools for
    code_generation ONLY, each with the registry's OWN description; the registry catalog carries
    all four.  (The literal drift rows below are the byte-level pin.)
    """
    holders = {
        agent_type: [tool["name"] for tool in capability_manifest.tool_entries_for(agent_type)]
        for agent_type in pf.AGENT_CAPABILITIES
    }
    assert holders["competitor_research"] == ["web_fetch", "web_search"]
    # V25/L2: the trio keeps its positions and the GitHub quartet follows it (append-only).
    assert holders["code_generation"][:3] == ["code_write", "code_read", "code_list"]
    assert {agent for agent, names in holders.items() if "web_search" in names} == {
        "competitor_research"}
    assert {agent for agent, names in holders.items()
            if {"code_write", "code_read", "code_list"} & set(names)} == {"code_generation"}
    registry_names = [tool["name"] for tool in capability_manifest.registry_tool_entries()]
    for name in ("web_search", "code_write", "code_read", "code_list"):
        assert name in registry_names
    entry = capability_manifest.tool_entries_for("competitor_research")[1]
    assert entry["description"] == _spec_description("web_search")


def test_the_v25_github_tools_are_visible_exactly_where_the_allowlist_grants_them():
    """V25/L2 — the source-control quartet rides the same derivation as every other tool.

    All four appear in the manifest for `code_generation` ONLY, in the firewall's use order
    (repo -> branch -> commit -> PR), each with the registry's OWN description; the registry
    catalog carries all four.  (The literal drift rows above are the byte-level pin.)
    """
    holders = {
        agent_type: [tool["name"] for tool in capability_manifest.tool_entries_for(agent_type)]
        for agent_type in pf.AGENT_CAPABILITIES
    }
    quartet = ["github_repo", "github_branch", "github_commit", "github_pr"]
    assert holders["code_generation"] == [
        "code_write", "code_read", "code_list", *quartet]
    assert {agent for agent, names in holders.items() if set(names) & set(quartet)} == {
        "code_generation"}
    registry_names = [tool["name"] for tool in capability_manifest.registry_tool_entries()]
    for name in quartet:
        assert name in registry_names
        entry = next(tool for tool in capability_manifest.tool_entries_for("code_generation")
                     if tool["name"] == name)
        assert entry["description"] == _spec_description(name), name


def test_the_phone_tool_is_visible_exactly_where_the_allowlist_grants_it():
    """V20/L2 — the owner's phone rides the same derivation as every other registry tool.

    `phone_read` must appear in the manifest for the two agents the policy firewall grants it
    to, with the registry's OWN description, and in NO other agent's row; the registry catalog
    carries it too. (The literal drift rows below are the byte-level pin.)
    """
    holders = {
        agent_type: [tool["name"] for tool in capability_manifest.tool_entries_for(agent_type)]
        for agent_type in pf.AGENT_CAPABILITIES
    }
    with_phone = {agent for agent, names in holders.items() if "phone_read" in names}
    assert with_phone == {"orchestrator", "customer_support"}
    assert holders["orchestrator"] == ["macro_snapshot", "pricing_catalog", "phone_read"]
    assert holders["customer_support"] == ["phone_read"]
    registry_names = [tool["name"] for tool in capability_manifest.registry_tool_entries()]
    assert "phone_read" in registry_names
    phone_entry = capability_manifest.tool_entries_for("customer_support")[0]
    assert phone_entry["description"] == _spec_description("phone_read")


def test_unknown_agent_gets_no_fabricated_capability():
    entry = capability_manifest.agent_manifest_entry("no_such_agent")
    assert entry == {"agent_type": "no_such_agent", "tools": [], "action_classes": [],
                     "channels": []}
    assert capability_manifest.tool_entries_for("no_such_agent") == []


def test_dangling_allowlist_entry_raises_a_named_config_refusal(monkeypatch):
    """An allowlist naming a tool no spec ships is a registry inconsistency: refuse BY NAME."""
    monkeypatch.setitem(
        pf.AGENT_CAPABILITIES, "finance",
        AgentCapability(allowed_tools=(), agent_tools=("ghost_tool",)),
    )
    with pytest.raises(PolsiaError) as excinfo:
        capability_manifest.tool_entries_for("finance")
    assert excinfo.value.code == E_CONFIG
    assert "ghost_tool" in str(excinfo.value)


# ── the DRIFT PIN (literal snapshots — a registry/allowlist change must fail these) ──────────
# Same discipline as tests/parity/test_api_surface_parity.py's EXPECTED_ROUTES: adding,
# renaming or removing a registry tool / allowlist entry / action class / channel is a
# deliberate act, and the commit that changes it updates these rows in the same commit.
#
# WAVE V20 (L2, frozen `phone_read`): one tool joined the registry and two allowlists —
#   * PINNED_REGISTRY_TOOLS gains `phone_read` (4 tools, sorted);
#   * `orchestrator` gains it after macro/pricing (the owner: "agents/orchestrator can
#     see/use the phone"); `customer_support` gains it as its ONLY registry tool (it already
#     reads the owner's inbound correspondence).  Every other agent's row is UNCHANGED.
# The rows below are the deliberate re-pin for that change; no action class or channel moved.
#
# WAVE V24 (L2, real agent tools): FOUR tools joined the registry and TWO agent rows moved —
#   * PINNED_REGISTRY_TOOLS gains `web_search`, `code_write`, `code_read`, `code_list`
#     (8 tools, sorted) — the frozen V24 interface: DeepSeek Search API + the workspace trio;
#   * `competitor_research` gains `web_search` AFTER `web_fetch` (append-only): its ROLE already
#     instructed "Use WebSearch to find candidates and WebFetch to read the pages";
#   * `code_generation` moves from `()` to (`code_write`, `code_read`, `code_list`): the agent
#     whose duty is "ship a change" finally has a real (workspace-confined) file surface.
#   * the ORCHESTRATOR row is deliberately UNCHANGED (no code_read/code_list): "a planner that
#     can act is a planner that can be injected into acting" (its own module docstring).
# No action class or channel moved anywhere in V24.
#
# WAVE V25 (L2, GitHub source control + per-run workspace): FOUR tools joined the registry and
# ONE agent row moved again —
#   * PINNED_REGISTRY_TOOLS gains `github_repo`, `github_branch`, `github_commit`, `github_pr`
#     (12 tools, sorted).  Evidence: V25-PLAN "Remaining work" row 2 — "**GitHub tools** —
#     repo/branch/commit/PR behind the firewalled registry + secret store ... The code agent can
#     actually ship code; source history like Polsia".
#   * `code_generation` gains the quartet AFTER the workspace trio (append-only): it is the one
#     role that ships source, the one holder of the `github` channel, and the one CLI allowlist
#     that already speaks git (while still denying `git push`).  Every other row is UNCHANGED.
#   * the ORCHESTRATOR row stays unchanged again — a source-control writer would widen the
#     planner into an actor, exactly the V24 decision.
# No action class or channel moved anywhere in V25 either (the quartet rides the `github`
# channel `code_generation` already had; the manifest action-class rows are byte-identical).

PINNED_REGISTRY_TOOLS: tuple[str, ...] = (
    "code_list", "code_read", "code_write", "github_branch", "github_commit", "github_pr",
    "github_repo", "macro_snapshot", "phone_read", "pricing_catalog", "web_fetch", "web_search")

PINNED_AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "ads_management": (),
    "business_planning": ("macro_snapshot", "pricing_catalog"),
    "code_generation": ("code_write", "code_read", "code_list",
                        "github_repo", "github_branch", "github_commit", "github_pr"),
    "company_planner": ("macro_snapshot", "pricing_catalog"),
    "competitor_research": ("web_fetch", "web_search"),
    "customer_support": ("phone_read",),
    "email_outreach": (),
    "finance": ("macro_snapshot", "pricing_catalog"),
    "orchestrator": ("macro_snapshot", "pricing_catalog", "phone_read"),
    "social_media": (),
}

PINNED_ACTION_CLASSES: dict[str, tuple[str, ...]] = {
    "ads_management": ("external_reversible", "internal_write", "read"),
    "business_planning": ("internal_write", "read"),
    "code_generation": ("external_irreversible", "external_reversible", "internal_write", "read"),
    "company_planner": ("internal_write", "read"),
    "competitor_research": ("internal_write", "read"),
    "customer_support": ("external_reversible", "internal_write", "read"),
    "email_outreach": ("external_reversible", "internal_write", "read"),
    "finance": ("external_irreversible", "internal_write", "read"),
    "orchestrator": ("internal_write", "read"),
    "social_media": ("external_reversible", "internal_write", "read"),
}

PINNED_CHANNELS: dict[str, tuple[str, ...]] = {
    "ads_management": ("google_ads", "meta_ads"),
    "business_planning": (),
    "code_generation": ("github",),
    "company_planner": (),
    "competitor_research": ("web",),
    "customer_support": ("email",),
    "email_outreach": ("email",),
    "finance": ("stripe",),
    "orchestrator": (),
    "social_media": ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube"),
}


def test_drift_pin_the_registry_tool_set_is_frozen():
    live = tuple(sorted(spec["function"]["name"] for spec in tool_registry.TOOL_SPECS))
    assert live == PINNED_REGISTRY_TOOLS, (
        "the tool registry changed — update PINNED_REGISTRY_TOOLS (and the manifest consumers) "
        "deliberately in this commit"
    )


def test_drift_pin_the_agent_allowlist_map_is_frozen():
    live = {agent_type: tool_registry.tools_for(agent_type) for agent_type in pf.AGENT_CAPABILITIES}
    assert set(live) == set(PINNED_AGENT_TOOLS), (
        "a firewall agent was added/removed — pin every agent deliberately"
    )
    assert live == PINNED_AGENT_TOOLS, (
        "the agent→tool allowlist changed — update PINNED_AGENT_TOOLS deliberately in this commit"
    )


def test_drift_pin_action_classes_are_frozen():
    live = {
        agent_type: tuple(sorted(str(c) for c in cap.action_classes))
        for agent_type, cap in pf.AGENT_CAPABILITIES.items()
    }
    assert live == PINNED_ACTION_CLASSES, (
        "an agent's permitted action classes changed — update PINNED_ACTION_CLASSES deliberately"
    )


def test_drift_pin_channels_are_frozen():
    live = {agent_type: tuple(cap.channels) for agent_type, cap in pf.AGENT_CAPABILITIES.items()}
    assert live == PINNED_CHANNELS, (
        "an agent's permitted channels changed — update PINNED_CHANNELS deliberately"
    )


def test_the_manifest_payload_equals_the_drift_pins():
    data = capability_manifest.capability_manifest()
    tools_map = {row["agent_type"]: tuple(t["name"] for t in row["tools"])
                 for row in data["agents"]}
    assert tools_map == PINNED_AGENT_TOOLS
    classes_map = {row["agent_type"]: tuple(row["action_classes"]) for row in data["agents"]}
    assert classes_map == PINNED_ACTION_CLASSES
    channels_map = {row["agent_type"]: tuple(row["channels"]) for row in data["agents"]}
    assert channels_map == PINNED_CHANNELS
