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
    for agent_type in ("social_media", "email_outreach", "customer_support", "ads_management",
                       "code_generation"):
        assert capability_manifest.tool_entries_for(agent_type) == [], agent_type


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

PINNED_REGISTRY_TOOLS: tuple[str, ...] = ("macro_snapshot", "pricing_catalog", "web_fetch")

PINNED_AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "ads_management": (),
    "business_planning": ("macro_snapshot", "pricing_catalog"),
    "code_generation": (),
    "company_planner": ("macro_snapshot", "pricing_catalog"),
    "competitor_research": ("web_fetch",),
    "customer_support": (),
    "email_outreach": (),
    "finance": ("macro_snapshot", "pricing_catalog"),
    "orchestrator": ("macro_snapshot", "pricing_catalog"),
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
