"""V12 / W-A+W-B integration — the loop driven through the REAL tool registry (hermetic).

The cross-lane seam check: the loop sends the registry's OWN `TOOL_SPECS`, calls its REAL
`execute_tool`, and passes its REAL refusal payloads back VERBATIM. Nothing here needs the
network — `pricing_catalog` is a file read, a denied tool is refused by the firewall before any
runner, and an unknown name never resolves — so this is a proper unit test, not a live probe.
`importorskip` keeps the file honest while W-B's module is absent.

If W-B changes the registry's vocabulary, return shape or refusal envelope, THIS is the file that
goes red first — which is exactly what a frozen seam is for.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime

tool_registry = pytest.importorskip("app.agents.tool_registry")

SAFE_TEST_KEY = "sk-v12-toolloop-registry-key"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _completion(content, *, finish: str = "stop", tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chat-1", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                  "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 8},
    }


class _Scripted:
    def __init__(self, *payloads) -> None:
        self.queue = list(payloads)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"body": json.loads(request.content)})
        assert self.queue, "unscripted extra provider request (the loop over-called)"
        return httpx.Response(200, json=self.queue.pop(0))

    def messages(self, index: int) -> list[dict]:
        return self.requests[index]["body"]["messages"]

    def tool_result(self, index: int = 1) -> str:
        return [m for m in self.messages(index) if m["role"] == "tool"][0]["content"]


def _run(server: _Scripted, *, allowed_tools, agent_type: str):
    inv = claude_cli.build_invocation(
        prompt="do the thing", json_schema=SUMMARY_SCHEMA, allowed_tools=allowed_tools,
        disallowed_tools=(), tools_available="", timeout_s=30, schema_name=agent_type,
    )
    result = deepseek_runtime.invoke(
        inv, client=httpx.Client(transport=httpx.MockTransport(server)), api_key=SAFE_TEST_KEY
    )
    return result, json.loads(result.stdout)


def test_the_real_agents_invocation_is_the_signal_this_loop_reads():
    """base_agent is the PRODUCER of the activation signal — pin producer and consumer together.

    `BasePolsiaAgent._real_call` passes the registry's `tools_for(agent_type)` into
    `allowed_tools` (the DeepSeek-bound argv), and this runtime reads exactly that flag. If either
    side changes its mind about the seam, this test fails rather than the live feature quietly
    going dark.
    """
    from app.agents.crew_factory import get_agent_class

    agent = get_agent_class("finance")()
    tools = tool_registry.tools_for("finance")
    assert tools, "finance must have registry tools for this pin to mean anything"
    inv = agent._build_invocation("do the thing", json_schema=SUMMARY_SCHEMA,
                                  runtime_tools=tools)

    assert "--allowedTools" in inv.argv
    assert inv.argv[inv.argv.index("--allowedTools") + 1] == ",".join(tools)
    # `--disallowedTools` carries the agent's CLI patterns; what must NOT be on a tool-carrying
    # DeepSeek invocation is the BLANKET `*` form (that is the runtime's no-tools veto).
    denied = (inv.argv[inv.argv.index("--disallowedTools") + 1]
              if "--disallowedTools" in inv.argv else "")
    assert "*" not in {pattern.strip() for pattern in denied.split(",")}

    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "macro_snapshot", "{}")]),
        _completion('{"summary": "planned with a real tool"}'),
    )
    result = deepseek_runtime.invoke(
        inv, client=httpx.Client(transport=httpx.MockTransport(server)), api_key=SAFE_TEST_KEY
    )
    envelope = json.loads(result.stdout)

    payload = json.loads(server.tool_result(1))
    assert payload["ok"] is True and payload["tool"] == "macro_snapshot"
    assert envelope["tools_used"] == ["macro_snapshot"]
    assert envelope["num_turns"] == 2
    assert envelope["is_error"] is False
    assert result.exit_code == 0


def test_a_no_tool_agent_stays_byte_identical_through_the_real_producer():
    """email_outreach has an empty allowlist -> no `tools` field, no extra envelope keys.

    (V20/L2 moved customer_support onto `phone_read`, so the canonical no-tools agent is a
    genuinely tool-less one: email_outreach.)
    """
    from app.agents.crew_factory import get_agent_class

    agent = get_agent_class("email_outreach")()
    assert tool_registry.tools_for("email_outreach") == ()
    inv = agent._build_invocation("do the thing", json_schema=SUMMARY_SCHEMA,
                                  runtime_tools=())

    assert "--allowedTools" not in inv.argv
    server = _Scripted(_completion('{"summary": "no tools involved"}'))
    result = deepseek_runtime.invoke(
        inv, client=httpx.Client(transport=httpx.MockTransport(server)), api_key=SAFE_TEST_KEY
    )
    envelope = json.loads(result.stdout)

    assert "tools" not in server.requests[0]["body"]
    assert "tools_used" not in envelope
    assert envelope["num_turns"] == 1


def test_real_registry_specs_are_what_ride_the_request():
    """The runtime sends the registry's list EXACTLY — its enforcement is the only allowlist."""
    server = _Scripted(_completion('{"summary": "no tools needed"}'))
    _, envelope = _run(server, allowed_tools=("pricing_catalog",), agent_type="finance")

    assert envelope["tools_used"] == []
    assert server.requests[0]["body"]["tools"] == tool_registry.TOOL_SPECS
    assert {spec["function"]["name"] for spec in tool_registry.TOOL_SPECS} == {
        "web_fetch", "macro_snapshot", "pricing_catalog", "phone_read"  # phone_read: V20/L2
    }


def test_real_registry_pricing_catalog_round_trip_for_an_allowed_agent():
    """A REAL tool executes (file read, no network) and its payload is what the model reads."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "pricing_catalog", "{}")]),
        _completion('{"summary": "catalog read"}'),
    )
    result, envelope = _run(server, allowed_tools=("pricing_catalog",), agent_type="finance")

    payload = json.loads(server.tool_result(1))
    assert payload["ok"] is True
    assert payload["tool"] == "pricing_catalog"
    assert payload["result"]["row_count"] > 0          # the real catalog on disk
    assert envelope["tools_used"] == ["pricing_catalog"]
    assert envelope["result"] == '{"summary": "catalog read"}'
    assert envelope["num_turns"] == 2
    assert result.exit_code == 0


def test_real_registry_denies_a_tool_outside_the_agents_allowlist():
    """social_media has NO tools: the firewall refuses by name and the run stays honest."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "macro_snapshot", "{}")]),
        _completion('{"summary": "worked without it"}'),
    )
    result, envelope = _run(server, allowed_tools=("pricing_catalog",), agent_type="social_media")

    refusal = json.loads(server.tool_result(1))
    assert refusal["ok"] is False
    assert refusal["error"]["code"] == "E_POLICY_DENIED"
    assert "not in allowlist" in refusal["error"]["message"]
    # the run's answer is the MODEL's text — never the refusal payload
    assert envelope["result"] == '{"summary": "worked without it"}'
    assert "E_POLICY_DENIED" not in envelope["result"]
    assert result.exit_code == 0


def test_real_registry_unknown_tool_is_a_named_refusal_passed_back_verbatim():
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_surfer", "{}")]),
        _completion('{"summary": "reported the refusal"}'),
    )
    _run(server, allowed_tools=("web_fetch",), agent_type="competitor_research")

    refusal = json.loads(server.tool_result(1))
    assert refusal["ok"] is False
    assert refusal["error"]["code"] == "E_VALIDATION"
    assert "web_surfer" in refusal["error"]["message"]
