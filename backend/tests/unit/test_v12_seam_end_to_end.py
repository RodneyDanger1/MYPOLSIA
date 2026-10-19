"""V12 / F1 — the tool-layer ACTIVATION SEAM, end to end through the REAL chain.

WHY this file exists (A-2 REVIEW §1.3, adjudicated against the final tree): the review claimed
`base_agent._build_invocation`'s `runtime_tools` parameter had NO caller — so a DeepSeek run's
activation signal (`deepseek_runtime._allowed_tools_from_argv`) was said to read CLI-namespace
names (`AgentCapability.allowed_tools`) while `execute_tool` enforces REGISTRY names: loop ON
for no-tool agents, loop OFF for tool-owning agents, `runtime_tools` dead code. The final tree
passes `runtime_tools=tool_registry.tools_for(agent_type)` on every DeepSeek-bound run
(`_real_call`, base_agent.py:415-426) and `:380` consumes it. This file pins that whole chain so
it can never silently regress again:

    tool_registry.tools_for(agent)        (the frozen mapping — the single source of truth)
      -> base_agent._real_call            (registry list passed on DeepSeek runs; None on CLI)
      -> claude_cli.build_invocation      (renders `--allowedTools`; an empty list = no flag)
      -> deepseek_runtime.invoke          (REAL: plan, activation read, tool loop, envelope)
      -> body["tools"] == TOOL_SPECS      (the wire, when — and only when — tools are on)
      -> tool_registry.execute_tool       (REAL registry executes / refuses by name)

Nothing in the chain is stubbed: no `_build_invocation` monkeypatch, no hand-built invocation,
no fake registry, no fake retry ladder. The ONLY fake is the HTTP transport
(`httpx.MockTransport` — the house seam used by `test_deepseek_runtime.py`), handed to the REAL
`deepseek_runtime.invoke` through its own documented `client=` parameter.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import deepseek_runtime, tool_registry
from app.agents.base_agent import BasePolsiaAgent

#: Clearly fake, ASCII-only (httpx encodes header values as ASCII) — house pattern.
SAFE_TEST_KEY = "sk-f1-seam-test-key"

#: V20/L2: `phone_read` joined the registry — the frozen wire vocabulary is four names.
#: V24/L2: `web_search` + the three code tools joined — the frozen wire vocabulary is eight.
#: V25/L2: the GitHub quartet joined — the frozen wire vocabulary is twelve (V25-PLAN row 2:
#: "GitHub tools — repo/branch/commit/PR behind the firewalled registry + secret store").
FROZEN_TOOL_NAMES = {"web_fetch", "web_search", "macro_snapshot", "pricing_catalog",
                     "phone_read", "code_write", "code_read", "code_list",
                     "github_repo", "github_branch", "github_commit", "github_pr"}


def _agent(agent_type: str) -> BasePolsiaAgent:
    class _Probe(BasePolsiaAgent):
        def run(self, task, context):  # pragma: no cover - never dispatched here
            return {"summary": "probe"}

    _Probe.agent_type = agent_type
    return _Probe()


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The runtime's session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


def _completion(content, *, finish: str = "stop", tool_calls=None, usage=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chat-f1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "prompt_cache_hit_tokens": 2,
        },
    }


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class _Wire:
    """The ONE fake: the HTTP socket. Records every request the REAL runtime sends."""

    def __init__(self, responder):
        self.requests: list[dict] = []
        self._responder = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "body": body,
                              "headers": dict(request.headers)})
        return httpx.Response(200, json=self._responder(len(self.requests), body))


def _wire(monkeypatch, responder) -> tuple[_Wire, dict]:
    """Drive the whole REAL chain; fake nothing but the transport.

    `deepseek_runtime.invoke` is WRAPPED for observation only: the wrapper records the real
    Invocation `_real_call` built, then hands the REAL function its own documented `client=`
    test seam (an `httpx.Client` over `MockTransport`). The function under test runs unchanged.
    """
    wire = _Wire(responder)
    client = httpx.Client(transport=httpx.MockTransport(wire.handler))
    real_invoke = deepseek_runtime.invoke
    seen: dict = {"invs": []}

    def observed(inv, **kwargs):
        seen["invs"].append(inv)
        return real_invoke(inv, client=client, **kwargs)

    monkeypatch.setattr(deepseek_runtime, "invoke", observed)
    # The frozen key interface: the seam predicate (`available()`) AND the invoke-time resolver
    # are the same function — one patch keeps the run DeepSeek-bound without any real key.
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)
    return wire, seen


def _argv_flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


# ── Case A: agent WITH registry tools — registry list -> argv -> wire -> real execution ──────

@pytest.mark.parametrize(("agent_type", "expected"), [
    # (A-2's own parent-live-check agent) CLI map says `WebFetch` — the argv must say `web_fetch`.
    # V24/L2: the shipped search joins it (append-only; the registry order is the offer order).
    ("competitor_research", ("web_fetch", "web_search")),
    # CLI map is EMPTY — only the REGISTRY list can turn this agent's loop on at all.
    ("finance", ("macro_snapshot", "pricing_catalog")),
])
def test_case_a_registry_tools_reach_the_wire_as_the_activation_signal(monkeypatch, agent_type, expected):
    assert tool_registry.tools_for(agent_type) == expected  # frozen mapping guard

    wire, seen = _wire(monkeypatch, lambda n, body: _completion(json.dumps({"summary": "done"})))

    raw = _agent(agent_type)._real_call("do the thing")

    # hop 1 — the REAL invocation carries the REGISTRY list (not a CLI-namespace pattern).
    inv = seen["invs"][0]
    assert tuple((_argv_flag(inv.argv, "--allowedTools") or "").split(",")) == expected
    assert inv.schema_name == agent_type        # the registry identity for execute_tool rides along

    # hop 2 — the request ACTIVATED the loop and carries the registry's frozen specs.
    assert len(wire.requests) == 1
    request = wire.requests[0]
    assert request["url"] == "https://api.deepseek.com/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {SAFE_TEST_KEY}"
    body = request["body"]
    assert "tools" in body
    assert body["tools"] == tool_registry.TOOL_SPECS
    spec_names = {spec["function"]["name"] for spec in body["tools"]}
    assert spec_names == FROZEN_TOOL_NAMES
    assert set(expected) <= spec_names          # every allowlisted name is a frozen spec name

    # hop 3 — the run record: tools were available, the model called none (a measured zero).
    assert raw.envelope["tools_used"] == []
    assert raw.envelope["num_turns"] == 1
    assert raw.text == json.dumps({"summary": "done"})
    assert raw.transport == "http" and raw.attempts == 1


def test_case_a_tool_call_round_trips_through_the_real_registry(monkeypatch):
    """`pricing_catalog` is a hermetic file read: the loop REALLY executes it through
    `tool_registry.execute_tool` and the answer rides back as a `role:"tool"` message carrying
    the call's own id — then the model's next answer is the run's result."""
    def responder(n, body):
        if n == 1:
            return _completion(None, finish="tool_calls",
                               tool_calls=[_tool_call("call-1", "pricing_catalog", "{}")],
                               usage={"prompt_tokens": 10, "completion_tokens": 5,
                                      "total_tokens": 15, "prompt_cache_hit_tokens": 2})
        return _completion(json.dumps({"summary": "priced"}),
                           usage={"prompt_tokens": 20, "completion_tokens": 8,
                                  "total_tokens": 28, "prompt_cache_hit_tokens": 0})

    wire, _ = _wire(monkeypatch, responder)
    raw = _agent("finance")._real_call("price the offer")

    assert len(wire.requests) == 2
    assert "tools" in wire.requests[1]["body"]      # the loop's second call is still tool-bound
    follow_up = wire.requests[1]["body"]["messages"]
    assert follow_up[-2]["role"] == "assistant"
    assert follow_up[-2]["tool_calls"][0]["id"] == "call-1"
    assert follow_up[-1]["role"] == "tool"
    assert follow_up[-1]["tool_call_id"] == "call-1"
    payload = json.loads(follow_up[-1]["content"])
    assert payload["ok"] is True and payload["tool"] == "pricing_catalog"
    assert payload["result"]["row_count"] == 50     # the REAL catalog, not a proxy value

    assert raw.envelope["tools_used"] == ["pricing_catalog"]
    assert raw.envelope["num_turns"] == 2
    assert raw.envelope["usage"]["input_tokens"] == 30   # summed across the loop's provider calls
    assert raw.envelope["usage"]["output_tokens"] == 13
    assert raw.text == json.dumps({"summary": "priced"})  # a tool result is NEVER the answer


def test_case_a_over_requested_tool_is_refused_by_the_real_registry(monkeypatch):
    """`competitor_research` may call only web_fetch; a macro_snapshot request must come back as
    the registry's own E_POLICY_DENIED payload — executed by the REAL firewall at the registry
    seam — and the run still finishes on the model's next answer."""
    def responder(n, body):
        if n == 1:
            return _completion(None, finish="tool_calls",
                               tool_calls=[_tool_call("call-9", "macro_snapshot", "{}")])
        return _completion(json.dumps({"summary": "denial noted"}))

    wire, _ = _wire(monkeypatch, responder)
    raw = _agent("competitor_research")._real_call("read macro")

    tool_message = wire.requests[1]["body"]["messages"][-1]
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "E_POLICY_DENIED"
    assert "macro_snapshot" in payload["error"]["message"]
    assert raw.envelope["tools_used"] == ["macro_snapshot"]   # it REACHED the executor, was refused
    assert raw.text == json.dumps({"summary": "denial noted"})


# ── Case B: agent with NONE — the pre-loop contract, byte-identical ───────────────────────────

@pytest.mark.parametrize("agent_type", ["email_outreach", "no_such_agent"])
def test_case_b_agent_without_registry_tools_sends_no_tools_field(monkeypatch, agent_type):
    """`email_outreach`: mapped, `agent_tools=()` (V20/L2 moved customer_support onto
    `phone_read`, so the canonical no-tools agent is email_outreach); `no_such_agent`: an empty
    mapping must never default to tools. Both: no `tools` on the wire, no `tools_used` in the
    envelope, exactly one provider call — no key beyond the pre-loop body."""
    wire, seen = _wire(monkeypatch, lambda n, body: _completion(json.dumps({"summary": "plain"})))

    raw = _agent(agent_type)._real_call("plain work")

    inv = seen["invs"][0]
    assert "--allowedTools" not in inv.argv               # no CLI-namespace name leaks into argv
    assert len(wire.requests) == 1
    body = wire.requests[0]["body"]
    assert "tools" not in body
    assert not any(key.startswith("tool") for key in body)
    assert {"model", "messages", "stream", "max_tokens"} <= set(body)
    assert set(body) <= {"model", "messages", "stream", "max_tokens",
                         "thinking", "reasoning_effort"}
    assert "tools_used" not in raw.envelope
    assert raw.envelope["num_turns"] == 1
    assert raw.text == json.dumps({"summary": "plain"})


# ── Case C: doctrine — a tool is named only when this agent can really call it ────────────────

def test_case_c_doctrine_names_only_callable_registry_tools(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)

    finance = _agent("finance").layers_for({"title": "t"}, {})["tool_doctrine"].text
    assert "- Tools available to you: macro_snapshot, pricing_catalog." in finance
    assert "web_fetch" not in finance          # not callable for finance -> must not be named

    # V20/L2: customer_support now holds exactly the read-only phone tool; its doctrine names
    # that tool and must never name a tool it cannot call.
    support = _agent("customer_support").layers_for({"title": "t"}, {})["tool_doctrine"].text
    assert "phone_read" in support
    assert "macro_snapshot" not in support and "pricing_catalog" not in support
    assert "web_fetch" not in support

    # A genuinely tool-less agent keeps the honest no-tools text.
    empty = _agent("email_outreach").layers_for({"title": "t"}, {})["tool_doctrine"].text
    assert "none for this agent" in empty
    for name in FROZEN_TOOL_NAMES:
        assert name not in empty


# ── Hostile pass: the activation reader fails CLOSED on hostile/absent shapes ─────────────────

def test_hostile_malformed_argv_can_never_activate_the_loop():
    """A malformed, empty or deny-globbed argv resolves to NO tools — never to the registry's
    specs by accident (the fail-closed rulebook of the activation reader)."""
    from app.agents.deepseek_runtime import _allowed_tools_from_argv

    assert _allowed_tools_from_argv(["--allowedTools"]) == ()             # flag, no value
    assert _allowed_tools_from_argv(["--allowedTools", ""]) == ()         # empty value
    assert _allowed_tools_from_argv(["--allowedTools", " , "]) == ()      # blanks only
    assert _allowed_tools_from_argv(
        ["--disallowedTools", "*", "--allowedTools", "web_fetch"]) == ()  # the deny glob wins
    assert _allowed_tools_from_argv(
        ["--allowedTools", "web_fetch,,macro_snapshot,"]) == ("web_fetch", "macro_snapshot")
