"""V29 follow-up F — the HARD-WIRING audit: registry, grants, manifest, includes, tool loop.

Five layers have to agree or the fleet advertises a capability nothing enforces:

  1. `tool_registry` — the 17 specs, the 17 runners and the 17 guidance lines are the SAME
     seventeen tools (a spec with no runner is a prompt promise nothing can execute);
  2. every spec is a CLOSED object (`additionalProperties: false` at every level, `required`
     names real properties) so the provider cannot hand the runner keys the schema never
     described;
  3. `policy_firewall.AGENT_CAPABILITIES` — each V29 tool is granted to exactly the agents whose
     role needs it (least privilege, pinned per agent, over- AND under-grant both fail);
  4. `capability_manifest` / `GET /api/v1/ops/capabilities` — the operator surface lists the real
     seventeen (names + the registry's own description bytes), an unknown agent gets NO
     fabricated defaults, and the four V29 routes are in `app.openapi()`;
  5. the DeepSeek tool loop still echoes `reasoning_content` VERBATIM on every assistant
     tool-call turn — across MULTIPLE turns, not just the last — because the provider rejects a
     later request whose tool conversation dropped it.

No network and no paid call: the loop runs on `httpx.MockTransport` (the existing transport
seam) with a fake registry patched into `deepseek_runtime._tool_registry`; the API test uses the
same in-process client every other route test uses.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, policy_firewall, tool_registry
from app.agents.tool_registry import (
    _RUNNERS,
    TOOL_GUIDANCE,
    TOOL_SPECS,
    execute_tool,
    tools_for,
)
from app.core.errors import E_POLICY_DENIED, E_VALIDATION
from app.services import capability_manifest

#: The five tools V29 added — the ones whose grants this audit pins.
NEW_TOOLS = ("research_fetch", "fx_convert", "signals_read", "domain_health", "link_status")

#: V29's least-privilege decision, spelled out PER AGENT (not a subset check): the exact set of
#: V29 tools each agent may call.  A grant added to another agent — or silently dropped — fails.
#: `ord` order is the allowlist's use order and is pinned separately below.
EXPECTED_V29_GRANTS: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"signals_read", "fx_convert"}),
    "business_planning": frozenset({"fx_convert"}),
    "competitor_research": frozenset(
        {"research_fetch", "signals_read", "domain_health", "link_status"}
    ),
    "company_planner": frozenset(),
    "social_media": frozenset({"signals_read"}),
    "email_outreach": frozenset({"link_status"}),
    "customer_support": frozenset(),
    "ads_management": frozenset(),
    "code_generation": frozenset(),
    "finance": frozenset({"fx_convert"}),
}

SPEC_NAMES = tuple(str(spec["function"]["name"]) for spec in TOOL_SPECS)


def _spec(name: str) -> dict:
    return next(spec for spec in TOOL_SPECS if spec["function"]["name"] == name)


# ─────────────────────────────────────────────────────────────────────────────
# (1) 17 specs == 17 runners == 17 guidance lines
# ─────────────────────────────────────────────────────────────────────────────

def test_the_registry_ships_exactly_seventeen_of_each_and_they_are_the_same_names() -> None:
    assert len(TOOL_SPECS) == 17
    assert len(SPEC_NAMES) == len(set(SPEC_NAMES)) == 17
    assert set(_RUNNERS) == set(SPEC_NAMES), (
        "a spec without a runner is a tool the model can request and nothing can execute; "
        f"spec-only={sorted(set(SPEC_NAMES) - set(_RUNNERS))}, "
        f"runner-only={sorted(set(_RUNNERS) - set(SPEC_NAMES))}"
    )
    assert set(TOOL_GUIDANCE) == set(SPEC_NAMES), (
        "every registry tool needs exactly one when-to-use line (base_agent renders them): "
        f"missing={sorted(set(SPEC_NAMES) - set(TOOL_GUIDANCE))}, "
        f"stale={sorted(set(TOOL_GUIDANCE) - set(SPEC_NAMES))}"
    )
    assert all(isinstance(text, str) and text.strip() for text in TOOL_GUIDANCE.values())


def test_each_runner_is_the_function_written_for_that_tool() -> None:
    """A copy-paste in `_RUNNERS` (e.g. `domain_health` wired to `_run_link_status`) is a silent
    mis-wire: the spec would execute another tool's code under its own name."""
    for name, runner in _RUNNERS.items():
        assert runner.__name__ == f"_run_{name}", (
            f"{name} is wired to {runner.__name__}, which implements another tool"
        )


def test_no_stale_action_class_entries() -> None:
    """`_TOOL_ACTION_CLASS` may only name shipped tools (a stale entry is drift, not a gate)."""
    assert set(tool_registry._TOOL_ACTION_CLASS) <= set(SPEC_NAMES)


# ─────────────────────────────────────────────────────────────────────────────
# (2) every spec is a closed object
# ─────────────────────────────────────────────────────────────────────────────

def _open_objects(schema: object, path: str) -> list[str]:
    """Every object in `schema` that is not CLOSED, plus `required` names that do not exist."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems
    kind = schema.get("type")
    if kind == "object":
        if schema.get("additionalProperties") is not False:
            problems.append(
                f"{path}: additionalProperties={schema.get('additionalProperties', '<absent>')!r}"
            )
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in properties:
                problems.append(f"{path}: required {name!r} is not a declared property")
        for name, child in properties.items():
            problems.extend(_open_objects(child, f"{path}.{name}"))
    elif kind == "array":
        problems.extend(_open_objects(schema.get("items", {}), f"{path}[]"))
    return problems


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_each_spec_is_a_closed_object(name: str) -> None:
    spec = _spec(name)
    assert set(spec) == {"type", "function"}, spec.keys()
    assert spec["type"] == "function"
    function = spec["function"]
    assert set(function) == {"name", "description", "parameters"}, function.keys()
    assert function["name"] == name
    assert isinstance(function["description"], str) and function["description"].strip()
    assert function["parameters"]["type"] == "object"
    assert _open_objects(function["parameters"], name) == []


# ─────────────────────────────────────────────────────────────────────────────
# (3) the V29 grants are exactly least-privilege, agent by agent
# ─────────────────────────────────────────────────────────────────────────────

def test_every_agent_has_a_pinned_least_privilege_v29_grant_set() -> None:
    # A NEW agent is a new least-privilege decision: it must be added here deliberately rather
    # than inheriting tools by accident.
    assert set(EXPECTED_V29_GRANTS) == set(policy_firewall.AGENT_CAPABILITIES)


@pytest.mark.parametrize("agent_type", sorted(EXPECTED_V29_GRANTS))
def test_v29_tool_grants_are_exactly_the_least_privilege_set(agent_type: str) -> None:
    granted = {name for name in tools_for(agent_type) if name in NEW_TOOLS}
    assert granted == set(EXPECTED_V29_GRANTS[agent_type]), (
        f"{agent_type}: granted={sorted(granted)}, "
        f"expected={sorted(EXPECTED_V29_GRANTS[agent_type])}"
    )


def test_v29_grants_only_extend_allowlists_that_already_existed() -> None:
    """Each V29 tool lands on an agent that already holds the READ class and a registry list."""
    for agent_type, granted in EXPECTED_V29_GRANTS.items():
        if not granted:
            continue
        capability = policy_firewall.AGENT_CAPABILITIES[agent_type]
        assert policy_firewall.ActionClass.READ in capability.action_classes, agent_type
        # no V29 tool is a mutation: none of them may be gated as a write class
        for name in granted:
            assert name not in tool_registry._TOOL_ACTION_CLASS or name == "code_write", name


def test_every_granted_tool_is_actually_executable_for_that_agent() -> None:
    """A grant whose action class the agent lacks is a dead promise: `decide()` denies it.

    This is the OTHER half of least privilege — no over-grant, and no grant the firewall's own
    class leg would refuse the moment the model called it.
    """
    for agent_type, capability in policy_firewall.AGENT_CAPABILITIES.items():
        for name in capability.agent_tools:
            action_class = tool_registry._TOOL_ACTION_CLASS.get(
                name, policy_firewall.ActionClass.READ
            )
            assert action_class in capability.action_classes, (
                f"{agent_type} may call {name!r} but lacks its class {action_class}"
            )
        assert len(capability.agent_tools) == len(set(capability.agent_tools)), agent_type


def test_no_registry_tool_is_granted_to_nobody() -> None:
    granted = {
        name
        for capability in policy_firewall.AGENT_CAPABILITIES.values()
        for name in capability.agent_tools
    }
    assert set(SPEC_NAMES) - granted == set(), (
        "a registry tool no agent may call is prompt surface with no owner: "
        f"{sorted(set(SPEC_NAMES) - granted)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (4) refusals: unknown tool, unknown agent, ungranted agent
# ─────────────────────────────────────────────────────────────────────────────

def _refusal(tool: str, *, agent_type: str, arguments: dict | None = None) -> dict:
    payload = json.loads(execute_tool(tool, arguments or {}, agent_type=agent_type))
    assert payload["ok"] is False, payload
    return payload["error"]


def test_unknown_tool_is_E_VALIDATION_before_any_agent_can_be_considered() -> None:
    for agent_type in ("orchestrator", "ghost_agent"):
        error = _refusal("teleport", agent_type=agent_type)
        assert error["code"] == E_VALIDATION, error
        assert "teleport" in error["message"]
        # the refusal names the REAL surface so a model can correct itself
        for name in SPEC_NAMES:
            assert name in error["message"]
        assert error["context"]["tool"] == "teleport"


def test_unknown_agent_gets_no_tools_and_no_fabricated_manifest_defaults() -> None:
    assert tools_for("ghost_agent") == ()
    entry = capability_manifest.agent_manifest_entry("ghost_agent")
    assert entry == {"agent_type": "ghost_agent", "tools": [],
                     "action_classes": [], "channels": []}
    block = capability_manifest.render_agent_capability_block("ghost_agent", tools_callable=True)
    assert "No capability entry exists" in block
    for name in SPEC_NAMES:
        assert name not in block, f"the fail-closed block advertised {name}"

    error = _refusal("web_fetch", agent_type="ghost_agent",
                     arguments={"url": "https://example.com"})
    assert error["code"] == E_POLICY_DENIED, error
    assert "unknown agent" in error["message"]


@pytest.mark.parametrize(
    ("agent_type", "tool"),
    (
        ("company_planner", "signals_read"),
        ("company_planner", "link_status"),
        ("ads_management", "link_status"),
        ("customer_support", "fx_convert"),
        ("email_outreach", "research_fetch"),
        ("social_media", "domain_health"),
        ("business_planning", "domain_health"),
        ("finance", "signals_read"),
        ("code_generation", "research_fetch"),
    ),
)
def test_an_ungranted_agent_is_refused_E_POLICY_DENIED(agent_type: str, tool: str) -> None:
    assert tool in SPEC_NAMES                    # the tool exists; only the GRANT is missing
    assert tool not in tools_for(agent_type)
    error = _refusal(tool, agent_type=agent_type)
    assert error["code"] == E_POLICY_DENIED, error
    assert tool in error["message"] and agent_type in error["message"]
    assert "policy_firewall.AGENT_CAPABILITIES" in error["how_to_fix"]


# ─────────────────────────────────────────────────────────────────────────────
# (5) /ops/capabilities lists the real seventeen; the V29 routes are in OpenAPI
# ─────────────────────────────────────────────────────────────────────────────

async def test_ops_capabilities_lists_the_real_seventeen(api_client, auth_headers) -> None:
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["registry_tools"]) == 17
    assert [row["name"] for row in data["registry_tools"]] == sorted(SPEC_NAMES)
    for row in data["registry_tools"]:
        assert row["description"] == _spec(row["name"])["function"]["description"]

    # every firewall agent appears once, with ITS allowlist and the registry's bytes
    rows = {row["agent_type"]: row for row in data["agents"]}
    assert set(rows) == set(policy_firewall.AGENT_CAPABILITIES)
    for agent_type, row in rows.items():
        assert [tool["name"] for tool in row["tools"]] == list(tools_for(agent_type))
        for tool in row["tools"]:
            assert tool["description"] == _spec(tool["name"])["function"]["description"]


def test_the_four_v29_routes_are_in_openapi() -> None:
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    expected = {
        "/api/v1/signals": "get",
        "/api/v1/trust/domain": "get",
        "/api/v1/trust/link": "get",
        "/api/v1/economics/convert": "get",
    }
    for path, method in expected.items():
        assert path in paths, f"{path} is not in app.openapi() — the router is not included"
        assert method in paths[path], f"{method.upper()} {path} missing from OpenAPI"
    # the operator surface that must show all seventeen is itself routed
    assert "get" in paths["/api/v1/ops/capabilities"]


# ─────────────────────────────────────────────────────────────────────────────
# (6) the tool loop still echoes reasoning_content — through the transport seam
# ─────────────────────────────────────────────────────────────────────────────

#: Clearly fake, ASCII-only key (httpx encodes header values as ASCII) — the house pattern.
SAFE_TEST_KEY = "sk-test-followup-f"


class _FakeRegistry:
    """The laziest registry that satisfies the loop's frozen seam (no product code runs)."""

    TOOL_SPECS = [
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch one http(s) URL.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    def execute_tool(self, name, arguments, *, agent_type):
        self.calls.append((name, arguments, agent_type))
        return json.dumps({"ok": True, "tool": name})


def _completion(content, *, finish: str = "stop", tool_calls=None, reasoning=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chat-1", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                  "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 8},
    }


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def test_the_loop_sends_every_turns_reasoning_content_back_to_deepseek(monkeypatch) -> None:
    """Two tool turns, then the answer: BOTH assistant echoes keep their reasoning verbatim.

    The provider documents that, with `tools` in play, a later request must carry the prior
    assistant turn's `reasoning_content`; echoing only the most recent turn would 400 the third
    request.  No network: `httpx.MockTransport` is the existing transport seam.
    """
    fake = _FakeRegistry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: fake, raising=False)

    scripted = [
        _completion(None, finish="tool_calls", reasoning="step one: I need the page",
                    tool_calls=[_tool_call("c1", "web_fetch")]),
        _completion(None, finish="tool_calls", reasoning="step two: verify the source",
                    tool_calls=[_tool_call("c2", "web_fetch")]),
        _completion("done", reasoning="not echoed: this turn ends the loop"),
    ]
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=scripted[len(requests) - 1])

    invocation = claude_cli.build_invocation(
        prompt="research the thing", json_schema=None, resume_session=None,
        allowed_tools=("web_fetch",), disallowed_tools=(), tools_available="default",
        timeout_s=30, schema_name="competitor_research",
    )
    result = deepseek_runtime.invoke(
        invocation, client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=SAFE_TEST_KEY,
    )
    envelope = json.loads(result.stdout)

    assert len(requests) == 3, "the loop must make exactly one call per scripted turn"
    assert envelope["is_error"] is False and envelope["result"] == "done"
    assert [name for name, _args, _agent in fake.calls] == ["web_fetch", "web_fetch"]

    def assistants(index: int) -> list[dict]:
        return [m for m in requests[index]["messages"] if m["role"] == "assistant"]

    # request 2 carries the first turn's reasoning...
    first_echo = assistants(1)
    assert len(first_echo) == 1
    assert first_echo[0]["reasoning_content"] == "step one: I need the page"
    assert first_echo[0]["content"] == ""                 # null content normalized, not dropped
    assert first_echo[0]["tool_calls"] == [_tool_call("c1", "web_fetch")]

    # ...and request 3 carries BOTH, each verbatim, each followed by its tool result
    second_echo = assistants(2)
    assert [m["reasoning_content"] for m in second_echo] == [
        "step one: I need the page", "step two: verify the source",
    ]
    assert [m["tool_calls"][0]["id"] for m in second_echo] == ["c1", "c2"]

    tool_messages = [m for m in requests[2]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]

    # the specs stay in play on every tool-enabled request
    assert all(body.get("tools") == fake.TOOL_SPECS for body in requests)
    # the loop's FINAL turn is not echoed at all (nothing follows it)
    assert len(assistants(2)) == 2


def test_the_loop_requests_the_real_seventeen_specs_and_refuses_an_unknown_tool_by_name() -> None:
    """The loop's request surface IS `TOOL_SPECS`; an invented tool name is refused E_VALIDATION.

    Uses the REAL registry (no fake, no patch) — `teleport` is refused before any policy lookup
    or network call, so this stays hermetic while pinning the loop↔registry wiring end to end.
    """
    scripted = [
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "teleport", "{}")]),
        _completion("done"),
    ]
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=scripted[len(requests) - 1])

    invocation = claude_cli.build_invocation(
        prompt="try an invented tool", json_schema=None, resume_session=None,
        allowed_tools=("web_fetch",), disallowed_tools=(), tools_available="default",
        timeout_s=30, schema_name="competitor_research",
    )
    result = deepseek_runtime.invoke(
        invocation, client=httpx.Client(transport=httpx.MockTransport(handler)),
        api_key=SAFE_TEST_KEY,
    )

    assert len(requests) == 2
    assert json.loads(result.stdout)["is_error"] is False

    # 17 specs, byte-for-byte the registry's own objects (copied, not rebuilt)
    assert len(requests[0]["tools"]) == 17
    assert requests[0]["tools"] == TOOL_SPECS

    # the refusal the MODEL sees is the registry's named E_VALIDATION payload, verbatim
    tool_message = next(m for m in requests[1]["messages"] if m["role"] == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == E_VALIDATION
    assert payload["error"]["context"]["tool"] == "teleport"
