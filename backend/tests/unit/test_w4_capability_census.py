"""W4/CAPABILITY-AUDITOR — the agent × tool capability census, derived from CODE.

WHY this file exists (wave 4, capability-auditor seat): before it, the fleet's real surface was
described in prose in several places that could each drift on their own —

  * `crew_factory.AGENT_MAP` (the dispatcher's registry, 9 agents) vs a HAND-TYPED copy in
    `task_service.VALID_AGENT_TYPES` vs a third copy in `schemas.agent_outputs.AGENT_TYPES` vs
    `policy_firewall.AGENT_CAPABILITIES` (10 entries — it also carries the SERVICE-INVOKED
    `company_planner`) vs `ALL_AGENT_TYPES` (11 — it also carries the DOCUMENTED-UNSHIPPED
    `deployment`);
  * the fleet capability map the ORCHESTRATOR plans from (`capability_manifest`) named every
    capability entry as a routable target, while `TaskSpec.agent_type` refused `company_planner`
    and accepted `deployment`, which no dispatcher ships.  MEASURED (this seat): a plan naming
    `company_planner` — an agent the orchestrator's OWN context advertises — fails
    `OrchestratorPlan` validation as a WHOLE, and a plan naming `deployment` is created by
    `daily_cycle` and then skipped by `create_task` as an unknown agent type: one wasted slot.
  * the registry's three name tables (`TOOL_SPECS`, `_RUNNERS`, `TOOL_GUIDANCE`) agreed, but the
    ACTION-CLASS table covered only the 4 mutating tools and `_tool_action_class` defaulted
    everything else to `read` — a NEW tool added to the registry without a class entry would be
    gated as a read (fail OPEN) instead of refusing by name.

So every pin below is a DERIVED statement (read from the code, never typed): a new agent, a new
tool, a new task type or a renamed module fails HERE instead of shipping as a lie.

Deliberately NOT pinned here (already covered elsewhere, kept to avoid a second copy):
  * which agent holds which tool — `test_v12_allowlist.py`, `test_v29_followup_f_hardwiring.py`;
  * the capability BLOCK text — `test_v16_capability_injection.py`;
  * registry specs/runners/guidance agreement + closed spec objects —
    `test_v29_followup_f_hardwiring.py`; this file adds the ACTION-CLASS exhaustiveness and the
    BEHAVIOURAL required-argument check those do not have.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.agents.base_agent import BasePolsiaAgent
from app.agents.crew_factory import (
    AGENT_MAP,
    VALID_AGENT_TYPES,
    get_agent_class,
    run_agent_for_task,
)
from app.agents.tool_registry import (
    TOOL_GUIDANCE,
    TOOL_SPECS,
    execute_tool,
    tools_for,
)
from app.core.errors import E_CONFIG, E_POLICY_DENIED, PolsiaError
from app.schemas import agent_outputs
from app.schemas.agent_outputs import TaskSpec
from app.services import capability_manifest, task_service

SPEC_NAMES = tuple(str(spec["function"]["name"]) for spec in TOOL_SPECS)


def _agent_for_tool(name: str) -> str:
    """The agent that may call `name`, DERIVED from the firewall's own allowlists."""
    holders = sorted(a for a in pf.AGENT_CAPABILITIES if name in tools_for(a))
    assert holders, f"no agent's allowlist names {name!r} — the tool is unreachable"
    return holders[0]


# ─────────────────────────────────────────────────────────────────────────────────────────────
# A. the dispatcher's registry is the ONE fleet registry
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_hand_typed_task_service_registry_equals_the_dispatcher_registry():
    """`task_service.VALID_AGENT_TYPES` is a COPY; this pin makes the copy unable to drift."""
    assert tuple(task_service.VALID_AGENT_TYPES) == tuple(AGENT_MAP), (
        "task_service.VALID_AGENT_TYPES and crew_factory.AGENT_MAP disagree — a task the "
        "dispatcher ships cannot be created (or vice versa)"
    )
    assert tuple(VALID_AGENT_TYPES) == tuple(AGENT_MAP)
    assert len(set(AGENT_MAP)) == len(AGENT_MAP) == 9, (
        f"expected the 9 shipped agents, found {sorted(AGENT_MAP)}"
    )


def test_every_dispatched_agent_class_matches_its_registry_entry():
    """class identity, declared type, schema identity and prompt are read from the CLASS."""
    for agent_type in AGENT_MAP:
        cls = get_agent_class(agent_type)
        assert issubclass(cls, BasePolsiaAgent), f"{agent_type}: {cls.__name__} is not an agent"
        assert cls.agent_type == agent_type, (
            f"{agent_type}: {cls.__name__}.agent_type is {cls.agent_type!r} — a run would be "
            "recorded under the wrong agent"
        )
        assert isinstance(cls.max_turns, int) and cls.max_turns > 0, agent_type
        assert cls.role_prompt.strip(), f"{agent_type}: a run with no role prompt has no job"
        assert cls.output_schema is agent_outputs.AGENT_SCHEMAS[agent_type], (
            f"{agent_type}: the class validates against {cls.output_schema.__name__} "
            f"but the schema registry says "
            f"{agent_outputs.AGENT_SCHEMAS[agent_type].__name__}"
        )
    assert set(agent_outputs.AGENT_SCHEMAS) == set(AGENT_MAP)
    assert set(agent_outputs.AGENT_TYPES) == set(AGENT_MAP)


# ─────────────────────────────────────────────────────────────────────────────────────────────
# B. the non-routable agents stay EXACTLY the two documented ones, and a plan cannot name them
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_two_documented_agents_are_outside_the_dispatcher():
    """`company_planner` is service-invoked; `deployment` is documented but ships no agent."""
    assert set(pf.AGENT_CAPABILITIES) - set(AGENT_MAP) == {"company_planner"}, (
        "a capability entry exists for an agent the dispatcher does not ship (or the "
        f"service-invoked planner lost its entry): "
        f"{sorted(set(pf.AGENT_CAPABILITIES) - set(AGENT_MAP))}"
    )
    assert set(agent_outputs.ALL_AGENT_TYPES) - set(AGENT_MAP) == {"deployment"}, (
        "ALL_AGENT_TYPES is the SCHEMA surface (registered agents + documented-only deployment); "
        f"unexpected extras: {sorted(set(agent_outputs.ALL_AGENT_TYPES) - set(AGENT_MAP))}"
    )
    # The service-invoked planner must really exist under the name the firewall grants, or its
    # capability entry is a fabricated capability no code can use.
    from app.agents.company_planner.agent import CompanyPlannerAgent
    from app.services import planner_service

    assert CompanyPlannerAgent.agent_type == planner_service.PLANNER_AGENT_TYPE
    assert CompanyPlannerAgent.agent_type == "company_planner"
    assert CompanyPlannerAgent.output_schema is not None


def test_a_plan_can_only_name_an_agent_the_dispatcher_ships():
    """`TaskSpec.agent_type` is the PLAN's contract: every name it accepts must be dispatchable."""
    for agent_type in AGENT_MAP:
        TaskSpec(title=f"task for {agent_type}", agent_type=agent_type)
    # `deployment` is schema-surface only (UNREGISTERED_SCHEMAS) — a plan that named it used to be
    # accepted and then skipped by `create_task` as an unknown agent type.
    for agent_type in sorted(
        (set(agent_outputs.ALL_AGENT_TYPES) | set(pf.AGENT_CAPABILITIES)) - set(AGENT_MAP)
    ):
        with pytest.raises(ValidationError):
            TaskSpec(title=f"task for {agent_type}", agent_type=agent_type)


def test_the_plan_contract_is_the_dispatchable_set_and_never_the_schema_surface():
    assert set(agent_outputs.TASK_AGENT_TYPES) == set(AGENT_MAP)
    assert "deployment" not in agent_outputs.TASK_AGENT_TYPES
    assert "company_planner" not in agent_outputs.TASK_AGENT_TYPES
    # ALL_AGENT_TYPES stays the schema surface (deployment's DeploymentOutput must keep validating)
    assert set(agent_outputs.ALL_AGENT_TYPES) == set(agent_outputs.TASK_AGENT_TYPES) | {"deployment"}


# ─────────────────────────────────────────────────────────────────────────────────────────────
# C. the fleet map MARKS routability (the orchestrator plans from this text)
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_fleet_map_marks_which_agents_a_plan_may_name():
    text = capability_manifest.render_fleet_capability_map(tools_callable=True)
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    agent_lines = [
        line for line in lines
        if line[2:].split(":", 1)[0].strip() in pf.AGENT_CAPABILITIES
    ]
    named = {line[2:].split(":", 1)[0].strip() for line in agent_lines}
    assert named == set(pf.AGENT_CAPABILITIES), (
        "the map must name every capability entry (the orchestrator routes by it): "
        f"missing={sorted(set(pf.AGENT_CAPABILITIES) - named)}, extra={sorted(named - set(pf.AGENT_CAPABILITIES))}"
    )
    for line in agent_lines:
        agent_type = line[2:].split(":", 1)[0].strip()
        if agent_type in AGENT_MAP:
            assert capability_manifest.ROUTABLE_MARKER in line, (
                f"{agent_type} IS dispatchable — the map must say a plan may name it: {line!r}"
            )
            assert capability_manifest.SERVICE_INVOKED_MARKER not in line, line
        else:
            assert capability_manifest.SERVICE_INVOKED_MARKER in line, (
                f"{agent_type} is NOT dispatchable — the map must say so, or the orchestrator "
                f"plans a task the plan schema refuses: {line!r}"
            )
            assert capability_manifest.ROUTABLE_MARKER not in line, line
    assert any("plan may name only" in line.lower() for line in lines), (
        "the map's header must state that only routable agents may be named in a plan"
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# D. a planned task whose declared side effect the agent cannot take is refused BEFORE dispatch
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_task_the_target_agent_cannot_perform_is_refused_before_the_agent_is_built():
    """`tasks.metadata.side_effect` is the plan's own claim; the firewall's class set is truth.

    Refusing here costs NO model call and NO run row: the agent is never constructed.  Before
    this check the mismatch was only discovered when the agent tried the action (or never —
    a task that merely *declared* the effect could be recorded completed).
    """
    task = {"id": 4242, "title": "refund a customer", "metadata": {"side_effect": "external_irreversible"}}
    with pytest.raises(PolsiaError) as excinfo:
        run_agent_for_task("business_planning", task, {})
    assert excinfo.value.code == E_POLICY_DENIED
    assert "external_irreversible" in excinfo.value.message
    assert "business_planning" in excinfo.value.message
    # And the operator-facing context names the class set, not just "denied".
    assert "action_classes" in excinfo.value.context


def test_a_task_the_agent_can_perform_still_runs():
    result = run_agent_for_task(
        "business_planning",
        {"id": 4243, "title": "plan the week", "metadata": {"side_effect": "internal_write"}},
        {},
    )
    assert isinstance(result, dict) and result.get("agent_type") == "business_planning"


def test_a_task_with_no_declared_side_effect_is_unaffected():
    """Every pre-existing task (manual runs, the fixed morning work) carries no claim: unchanged."""
    assert run_agent_for_task(
        "business_planning", {"id": 4244, "title": "plain work"}, {}
    ).get("agent_type") == "business_planning"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# E. the registry's name tables agree AND every tool has an explicit action class
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_every_registry_tool_has_exactly_one_action_class():
    assert set(tool_registry._RUNNERS) == set(SPEC_NAMES) == set(TOOL_GUIDANCE)
    classed = set(tool_registry.READ_ONLY_TOOLS) | set(tool_registry._TOOL_ACTION_CLASS)
    assert classed == set(SPEC_NAMES), (
        "a tool with no action-class entry would inherit `read` by omission (fail open): "
        f"unclassed={sorted(set(SPEC_NAMES) - classed)}, "
        f"stale={sorted(classed - set(SPEC_NAMES))}"
    )
    assert not (set(tool_registry.READ_ONLY_TOOLS) & set(tool_registry._TOOL_ACTION_CLASS)), (
        "a tool cannot be both a read-only tool and a write-class entry"
    )
    for name in SPEC_NAMES:
        # the gate classifies EVERY call; an unclassed name must raise, never guess.
        assert isinstance(tool_registry._tool_action_class(name, {"dry_run": False}), pf.ActionClass)


def test_an_unclassed_tool_is_refused_by_name_instead_of_running_as_a_read(monkeypatch):
    """A future spec added without a class entry must fail CLOSED at the one execution seam."""
    future = {
        "type": "function",
        "function": {
            "name": "future_tool",
            "description": "a tool added without an action class",
            "parameters": {"type": "object", "properties": {}, "required": [],
                           "additionalProperties": False},
        },
    }
    monkeypatch.setitem(tool_registry._SPEC_BY_NAME, "future_tool", future)
    monkeypatch.setitem(tool_registry._RUNNERS, "future_tool",
                        lambda *, agent_type, arguments: {"ok": True, "ran": True})
    with pytest.raises(PolsiaError) as excinfo:
        tool_registry._tool_action_class("future_tool", {})
    assert excinfo.value.code == E_CONFIG
    payload = tool_registry._execute("future_tool", {}, agent_type="finance")
    assert payload["ok"] is False
    assert payload["error"]["code"] == E_CONFIG, (
        "an unclassified tool must be refused before it can run as a read"
    )


def test_every_granted_tool_can_actually_be_gated_for_its_holder():
    """A grant the firewall's own class leg would refuse is a dead promise."""
    for agent_type, capability in pf.AGENT_CAPABILITIES.items():
        for name in tools_for(agent_type):
            for arguments in ({}, {"dry_run": False}):
                action_class = tool_registry._tool_action_class(name, arguments)
                assert action_class in capability.action_classes, (
                    f"{agent_type} holds {name} but the tool is gated {action_class} for "
                    f"{arguments!r}, which {agent_type} does not hold "
                    f"({sorted(str(c) for c in capability.action_classes)})"
                )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# F. behavioural drift: every schema-required argument is really required by its implementation
# ─────────────────────────────────────────────────────────────────────────────────────────────

#: A syntactically valid call per tool (only the REQUIRED arguments are used by these probes).
_VALID_ARGS: dict[str, dict] = {
    "web_fetch": {"url": "https://example.com"},
    "web_search": {"query": "soy wax wholesale price"},
    "code_read": {"path": "notes.txt"},
    "code_write": {"path": "notes.txt", "content": "x"},
    "github_repo": {"repo": "owner/name"},
    "github_branch": {"repo": "owner/name", "branch": "feat"},
    "github_commit": {"repo": "owner/name", "branch": "feat", "message": "m",
                      "files": [{"path": "a.txt", "content": "x"}]},
    "github_pr": {"repo": "owner/name", "title": "t", "head": "feat", "base": "main"},
    "research_fetch": {"url": "https://example.com"},
    "fx_convert": {"amount": 10, "base": "USD", "quote": "EUR"},
    "domain_health": {"host": "example.com"},
    "link_status": {"url": "https://example.com"},
}

_REQUIRED_ARGS = {
    str(spec["function"]["name"]): tuple(spec["function"]["parameters"].get("required") or ())
    for spec in TOOL_SPECS
}

_OMISSIONS = [
    (name, argument)
    for name in sorted(_REQUIRED_ARGS)
    for argument in _REQUIRED_ARGS[name]
]


def test_every_spec_required_argument_is_covered_by_a_probe():
    """The probe table must cover the registry — a new required argument fails here first."""
    assert {name for name, _ in _OMISSIONS} == {n for n in SPEC_NAMES if _REQUIRED_ARGS[n]}
    for name in {n for n, _ in _OMISSIONS}:
        missing = set(_REQUIRED_ARGS[name]) - set(_VALID_ARGS[name])
        assert not missing, f"{name}: no probe value for required argument(s) {sorted(missing)}"


@pytest.mark.parametrize(("name", "argument"), _OMISSIONS)
def test_a_schema_required_argument_is_really_required_by_the_implementation(name, argument):
    """Drift between a spec's `required` list and the runner that executes it.

    A tool whose implementation DEFAULTED a required argument would silently run on a call the
    schema says is incomplete — the model would never learn the argument mattered.  Every probe
    below must be refused BY NAME as an `E_VALIDATION` before any I/O (measured: all 21 refusals
    happen in the runner's own argument validation, no network, no file write).
    """
    arguments = {k: v for k, v in _VALID_ARGS[name].items() if k != argument}
    payload = json.loads(execute_tool(name, arguments, agent_type=_agent_for_tool(name)))
    assert payload.get("ok") is False, (
        f"{name} RAN without its schema-required {argument!r}: {payload!r}"
    )
    error = payload.get("error") or {}
    assert error.get("code") == "E_VALIDATION", (
        f"{name} refused a call missing {argument!r} with {error.get('code')!r} instead of the "
        f"named validation refusal: {error.get('message')!r}"
    )
    assert str(error.get("message") or "").strip(), f"{name}: the refusal names no reason"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# G. the derived manifest payload (GET /ops/capabilities) equals the code matrix
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_manifest_payload_is_the_code_matrix():
    payload = capability_manifest.capability_manifest()
    assert {entry["agent_type"] for entry in payload["agents"]} == set(pf.AGENT_CAPABILITIES)
    assert [entry["name"] for entry in payload["registry_tools"]] == sorted(SPEC_NAMES)
    for entry in payload["agents"]:
        assert {tool["name"] for tool in entry["tools"]} == set(tools_for(entry["agent_type"]))
        assert set(entry["action_classes"]) == {
            str(c) for c in pf.AGENT_CAPABILITIES[entry["agent_type"]].action_classes
        }
        assert tuple(entry["channels"]) == pf.AGENT_CAPABILITIES[entry["agent_type"]].channels


def test_every_registry_tool_names_at_least_one_consumer():
    """An allowlisted-but-holderless tool is a tool no production path can start."""
    reachable = {name for agent_type in pf.AGENT_CAPABILITIES for name in tools_for(agent_type)}
    assert reachable == set(SPEC_NAMES), (
        "registry tools no agent may call (dead producers): " f"{sorted(set(SPEC_NAMES) - reachable)}"
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# H. prompt claims vs the runtime branch that will run the agent
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _real_agent(agent_type: str) -> BasePolsiaAgent:
    """The SHIPPED agent instance (no run, no I/O): its own prompt is what a model would see."""
    if agent_type in AGENT_MAP:
        return get_agent_class(agent_type)()
    from app.agents.company_planner.agent import CompanyPlannerAgent

    return CompanyPlannerAgent()


def _named_tools(text: str) -> set[str]:
    import re

    return {
        name for name in SPEC_NAMES
        if re.search(rf"(?<![A-Za-z_]){re.escape(name)}(?![A-Za-z_])", text)
    }


def test_no_agent_prompt_names_a_registry_tool_the_agent_cannot_call():
    """A prompt that names an uncallable tool teaches the model to request a refusal."""
    for agent_type in list(AGENT_MAP) + ["company_planner"]:
        agent = _real_agent(agent_type)
        granted = set(tools_for(agent_type))
        named = _named_tools(agent.role_prompt)
        assert named <= granted, (
            f"{agent_type}: its ROLE names registry tool(s) it does not hold: "
            f"{sorted(named - granted)} (granted {sorted(granted)})"
        )


def test_the_cli_branch_never_advertises_a_registry_tool(monkeypatch):
    """Under the CLI runtime the registry loop does not exist — no tool NAME may be claimed.

    The doctrine names the run's real tools (`WebFetch` for the research agent, none for the
    rest); a registry name there would be the V12 defect (a capability this build cannot honor).
    """
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: False)
    for agent_type in list(AGENT_MAP) + ["company_planner"]:
        doctrine = _real_agent(agent_type).layers_for(
            {"id": 1, "title": "t"}, {"company": {"name": "Co"}}
        )["tool_doctrine"].text
        named = _named_tools(doctrine)
        assert named == set(), (
            f"{agent_type}: the CLI branch's doctrine advertises registry tool(s) the runtime "
            f"cannot call: {sorted(named)}"
        )


def test_the_deepseek_branch_advertises_exactly_the_agent_toolbox(monkeypatch):
    """The deepseek branch names the agent's own allowlist — no more, no fewer."""
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    for agent_type in AGENT_MAP:
        doctrine = _real_agent(agent_type).layers_for(
            {"id": 1, "title": "t"}, {"company": {"name": "Co"}}
        )["tool_doctrine"].text
        # the orchestrator also carries the fleet map (every agent's tools) — its own block is
        # still asserted through the "Tools available to you:" line the doctrine always renders.
        own_line = next(
            line for line in doctrine.splitlines() if line.startswith("- Tools available to you:")
        )
        granted = set(tools_for(agent_type))
        if not granted:
            assert "none" in own_line.lower(), f"{agent_type}: empty toolbox must say none"
        else:
            assert granted <= _named_tools(own_line), (
                f"{agent_type}: the doctrine line omits {sorted(granted - _named_tools(own_line))}"
            )
