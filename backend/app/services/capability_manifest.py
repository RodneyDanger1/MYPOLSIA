"""Capability manifest (V16/E6) — the ONE derived view of what each agent can actually do.

WHY this module exists (owner directive: "agents and orchestrator can see everything they can
use/do, know what that is, know how to use it"): before V16 the fleet's real capability surface
was spread across two registries that nothing surfaced together —

  * `policy_firewall.AGENT_CAPABILITIES` — the RUNTIME TRUTH: which action classes an agent may
    take (enforced by `decide()` on every side effect), which external channels it may touch,
    and the registry-tool allowlist (`agent_tools`, enforced by `tool_registry.execute_tool`);
  * `tool_registry` — `TOOL_SPECS` (each tool's name + description), `TOOL_GUIDANCE` (the
    one-line when-to-use) and `tools_for()` (the firewall's allowlist read).

Nothing here is hand-copied: every field of every view below is READ from those two modules on
each call, so the manifest, the agents' own prompt blocks, the orchestrator's fleet map and the
`GET /api/v1/ops/capabilities` surface (H-C1) cannot disagree with what the runtime enforces.
An allowlist that names a tool the registry does not ship is a registry inconsistency: it
raises a NAMED `E_CONFIG` refusal instead of fabricating a capability no tool can execute.

The three consumers, one derivation:

  * `agent_manifest_entry()` / `capability_manifest()` — the H-C1 payload (ops route);
  * `render_agent_capability_block()` — the per-agent block `base_agent.layers_for` places in
    the `tool_doctrine` layer (stable prefix) of EVERY agent's assembled context;
  * `render_fleet_capability_map()` — the cross-agent map the ORCHESTRATOR's context additionally
    carries, so the planner routes work by what an agent can really do.

Honesty rules (repo-wide):
  * a registry tool is NAMED in a prompt block only when the active runtime can really call it
    (`tools_callable` — the same rule the V12 tool doctrine follows); under the CLI runtime the
    block says so instead of advertising a loop that does not exist;
  * an agent with no firewall entry gets NO fabricated classes/channels/tools — empty lists and
    an explicit fail-closed sentence (a request from it would be refused BY NAME by `decide()`);
  * descriptions are the registry's own bytes (pinned by tests), never re-written here.
"""
from __future__ import annotations

from typing import Any

from app.agents.policy_firewall import AGENT_CAPABILITIES, AgentCapability
from app.agents.tool_registry import TOOL_SPECS, tools_for
from app.core.errors import E_CONFIG, PolsiaError

#: Headers the prompt blocks render — also the markers tests assert on.
CAPABILITY_BLOCK_HEADER = "CAPABILITY MANIFEST"
FLEET_MAP_HEADER = "FLEET CAPABILITY MAP"

__all__ = [
    "CAPABILITY_BLOCK_HEADER",
    "FLEET_MAP_HEADER",
    "agent_manifest_entry",
    "capability_manifest",
    "registry_tool_entries",
    "render_agent_capability_block",
    "render_fleet_capability_map",
    "tool_entries_for",
]


def _specs_by_name() -> dict[str, dict]:
    """The registry's function specs by tool name, rebuilt from `TOOL_SPECS` on every read.

    Not cached on purpose: the manifest is a READ of the registry, not a second copy of it — a
    deliberate registry change (a new spec, a re-worded description) is visible immediately
    through every consumer below.
    """
    return {str(spec["function"]["name"]): spec for spec in TOOL_SPECS}


def _tool_entry(name: str, spec: dict | None) -> dict[str, str]:
    """One `{name, description}` row (H-C1 shape); a dangling name refuses BY NAME."""
    if spec is None:
        raise PolsiaError(
            E_CONFIG,
            f"capability manifest: a firewall allowlist names tool {name!r}, but the tool "
            "registry ships no spec for it — the allowlist and the registry disagree",
            how_to_fix=(
                "add the tool to tool_registry.TOOL_SPECS, or remove it from that agent's "
                "agent_tools in policy_firewall.AGENT_CAPABILITIES; the manifest never invents "
                "a capability the runtime cannot execute"
            ),
            context={"tool": name},
        )
    return {"name": name, "description": str(spec["function"]["description"])}


# ---------------------------------------------------------------------------
# the manifest (H-C1)
# ---------------------------------------------------------------------------

def registry_tool_entries() -> list[dict[str, str]]:
    """`[{name, description}]` for EVERY registry tool, sorted by name (H-C1 `registry_tools`)."""
    specs = _specs_by_name()
    return [
        {"name": name, "description": str(specs[name]["function"]["description"])}
        for name in sorted(specs)
    ]


def tool_entries_for(agent_type: str) -> list[dict[str, str]]:
    """`[{name, description}]` for the tools `agent_type` may call, in allowlist order.

    The allowlist is read through `tool_registry.tools_for` (the firewall's own mapping), so an
    unknown agent yields `[]` — never a default set.
    """
    specs = _specs_by_name()
    return [_tool_entry(name, specs.get(name)) for name in tools_for(agent_type)]


def agent_manifest_entry(agent_type: str) -> dict[str, Any]:
    """One H-C1 agent row: `{agent_type, tools, action_classes, channels}`.

    An agent with no firewall entry gets EMPTY lists — the fail-closed shape of `decide()`
    (unknown agent -> DENY) — never the `AgentCapability` dataclass defaults, which happen to
    permit `read` and would fabricate a capability nothing enforces.
    """
    capability = AGENT_CAPABILITIES.get(agent_type)
    if capability is None:
        return {"agent_type": agent_type, "tools": [], "action_classes": [], "channels": []}
    return {
        "agent_type": agent_type,
        "tools": tool_entries_for(agent_type),
        "action_classes": sorted(str(c) for c in capability.action_classes),
        "channels": list(capability.channels),
    }


def capability_manifest() -> dict[str, Any]:
    """The H-C1 payload, derived at call time: `{agents: [...], registry_tools: [...]}`."""
    return {
        "agents": [agent_manifest_entry(agent_type) for agent_type in sorted(AGENT_CAPABILITIES)],
        "registry_tools": registry_tool_entries(),
    }


# ---------------------------------------------------------------------------
# the prompt blocks (base_agent's tool_doctrine layer)
# ---------------------------------------------------------------------------

def _action_classes_text(capability: AgentCapability) -> str:
    return ", ".join(sorted(str(c) for c in capability.action_classes)) or "none"


def render_agent_capability_block(agent_type: str, *, tools_callable: bool) -> str:
    """The per-agent capability block injected into EVERY agent's assembled context.

    `tools_callable` is the caller's runtime fact (the DeepSeek tool loop is active for this
    run): registry tool names + descriptions are rendered only when the runtime can really call
    them — the honesty rule the tool-doctrine lines above the block already follow.
    """
    lines = [
        f"{CAPABILITY_BLOCK_HEADER} (derived live from the tool registry + policy firewall — "
        "this is what you can really do)",
    ]
    capability = AGENT_CAPABILITIES.get(agent_type)
    if capability is None:
        lines.append(
            f"- No capability entry exists for agent {agent_type!r} in the policy firewall: no "
            "action class, channel or tool is permitted (fail closed — a request would be "
            "refused BY NAME)."
        )
        return "\n".join(lines)
    lines.append(f"- Action classes permitted for you: {_action_classes_text(capability)}")
    lines.append(f"- External channels you may touch: {', '.join(capability.channels) or 'none'}")
    names = tools_for(agent_type)
    if not tools_callable:
        lines.append(
            "- Registry tool calls: not available on this runtime (the registry tool-call loop "
            "runs when the DeepSeek runtime is active; the tool names above are the CLI runtime's "
            "own namespace)."
        )
    elif not names:
        lines.append(
            "- Registry tool calls: none — this agent's registry allowlist is empty (the runtime "
            "requests no tools for it)."
        )
    else:
        specs = _specs_by_name()
        lines.append("- Registry tool calls available to you (the registry's own descriptions):")
        for name in names:
            entry = _tool_entry(name, specs.get(name))
            lines.append(f"  - {entry['name']}: {entry['description']}")
    return "\n".join(lines)


def render_fleet_capability_map(*, tools_callable: bool) -> str:
    """The cross-agent map the ORCHESTRATOR's context additionally carries.

    One line per firewall agent: its permitted action classes, its external channels and its
    registry tools — the planning material for routing work to an agent that can carry it.
    """
    lines = [
        f"{FLEET_MAP_HEADER} (every agent, derived live from the same registry + policy firewall "
        "— route work to what an agent can really do)",
    ]
    for agent_type in sorted(AGENT_CAPABILITIES):
        capability = AGENT_CAPABILITIES[agent_type]
        names = list(tools_for(agent_type))
        if tools_callable:
            tools_text = ", ".join(names) or "none"
        else:
            tools_text = "not on this runtime (the registry tool loop runs under DeepSeek)"
        lines.append(
            f"- {agent_type}: actions [{_action_classes_text(capability)}]; "
            f"channels [{', '.join(capability.channels) or 'none'}]; registry tools [{tools_text}]"
        )
    return "\n".join(lines)
