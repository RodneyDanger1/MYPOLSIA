"""V12 / W-A — the DeepSeek runtime's tool-calling loop against the FROZEN registry seam.

Read-verified facts encoded here:

* with NO tools on the invocation (`--allowedTools` absent/empty, i.e. today's `tool_less`
  argv) the request body and the reply envelope carry no new keys and exactly one provider
  call happens — the pre-loop behavior, unchanged;
* with tools on, the runtime sends the registry's `TOOL_SPECS`, executes every call the model
  returns through `execute_tool(name, arguments, agent_type=...)` and appends the answer as a
  `role:"tool"` message carrying the SAME `tool_call_id` (the protocol the API requires);
* the loop is bounded at 4 provider calls; a reply that still asks for tools at the bound is a
  NAMED refusal envelope (`E_TOOL_LOOP`), never a hang and never a fabricated answer;
* a tool result NEVER becomes `result`: the run's text is the model's own final message;
* malformed arguments are never executed (no fabricated `{}` for garbage), a throwing
  `execute_tool` never escapes the runtime, and a registry refusal payload is passed back
  VERBATIM (the registry owns its E_* vocabulary);
* usage is summed across every provider call in the loop — all-measured or unmeasured, because
  a partial sum is fabrication-precision (the house NULL-vs-zero rule);
* `reasoning_content` rides back verbatim on the next request (the provider 400s a tool
  conversation that drops it).

No network: the HTTP seam is `httpx.MockTransport`; the registry seam is a test double patched
into `deepseek_runtime._tool_registry` (the lazy-import seam). One `importorskip` test pins the
REAL registry's shape when W-B's module has landed.
"""
from __future__ import annotations

import inspect
import json
import sys

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, pricing, structured_output
from app.agents.base_agent import BasePolsiaAgent
from app.core.errors import E_CLI_JSON, E_CONFIG, PolsiaError

#: Clearly fake, ASCII-only (httpx encodes header values as ASCII) — house pattern.
SAFE_TEST_KEY = "sk-v12-toolloop-test-key"

#: The brief's bound (a literal here on purpose: RED must fail per-test, not at collection).
MAX_ITERATIONS = 4

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class _Agent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


# ── the frozen registry seam, faked ──────────────────────────────────────────────────────────

class _FakeRegistry:
    """Stand-in for the frozen `app.agents.tool_registry` seam (W-B owns the real module)."""

    TOOL_SPECS = [
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch one http(s) URL and return its text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                           "required": ["url"]},
        }},
        {"type": "function", "function": {
            "name": "macro_snapshot",
            "description": "The cached macro snapshot.",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]

    def __init__(self, *, results: dict | None = None, raise_for: set | None = None,
                 returns=None) -> None:
        self.results = results or {}
        self.raise_for = raise_for or set()
        self.returns = returns
        self.calls: list[tuple[str, dict, str]] = []

    def execute_tool(self, name, arguments, *, agent_type):
        self.calls.append((name, arguments, agent_type))
        if name in self.raise_for:
            raise RuntimeError("registry exploded")
        if self.returns is not None:
            return self.returns
        if name in self.results:
            return self.results[name]
        return json.dumps({"ok": True, "tool": name, "data": f"real output of {name}"})


@pytest.fixture()
def registry(monkeypatch):
    """Install the fake registry on the runtime's lazy-import seam."""
    reg = _FakeRegistry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: reg, raising=False)
    return reg


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


# ── request scripting ───────────────────────────────────────────────────────────────────────

def _usage(p: int | None = 11, c: int | None = 7, hit: int | None = 3,
           miss: int | None = 8) -> dict:
    usage: dict = {"total_tokens": (p or 0) + (c or 0)}
    if p is not None:
        usage["prompt_tokens"] = p
    if c is not None:
        usage["completion_tokens"] = c
    if hit is not None:
        usage["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        usage["prompt_cache_miss_tokens"] = miss
    return usage


def _tool_call(call_id: str, name: str, arguments) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _completion(content, *, finish: str = "stop", usage: dict | None = None,
                tool_calls=None, reasoning: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": _usage() if usage is None else usage,
    }


class _Scripted:
    """One scripted provider: each call pops the next payload; extras are a hard failure."""

    def __init__(self, *payloads) -> None:
        self.queue = list(payloads)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "method": request.method,
                              "body": json.loads(request.content)})
        assert self.queue, "unscripted extra provider request (the loop over-called)"
        payload = self.queue.pop(0)
        if isinstance(payload, int):  # a bare int scripts an HTTP status
            return httpx.Response(payload, json={"error": {"message": f"status {payload}"}})
        return httpx.Response(200, json=payload)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def messages(self, index: int) -> list[dict]:
        return self.requests[index]["body"]["messages"]

    @property
    def calls(self) -> int:
        return len(self.requests)


def _make_inv(prompt: str = "do the thing", *, schema: dict | None = None,
              allowed_tools: tuple[str, ...] = (), agent_type: str = "competitor_research",
              denied: tuple[str, ...] = (), tools_available: str = "default",
              resume: str | None = None) -> claude_cli.Invocation:
    return claude_cli.build_invocation(
        prompt=prompt,
        json_schema=schema,
        resume_session=resume,
        allowed_tools=allowed_tools,
        disallowed_tools=denied,
        tools_available=tools_available,
        timeout_s=30,
        schema_name=agent_type,
    )


def _invoke(server: _Scripted, inv: claude_cli.Invocation, **kwargs):
    return deepseek_runtime.invoke(inv, client=server.client(), api_key=SAFE_TEST_KEY, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# (1) no tools -> byte-identical to the pre-loop runtime
# ─────────────────────────────────────────────────────────────────────────────
def test_tools_off_sends_no_tools_field_and_adds_no_envelope_keys(registry):
    server = _Scripted(_completion('{"summary": "real work"}'))
    # the EXACT tool_less argv base_agent builds today (allowed=(), denied=("*",), tools="")
    inv = _make_inv(schema=SUMMARY_SCHEMA, denied=("*",), tools_available="")

    result = _invoke(server, inv)
    envelope = json.loads(result.stdout)

    body = server.requests[0]["body"]
    assert "tools" not in body
    assert body["stream"] is False and body["thinking"] == {"type": "disabled"}
    assert "tools_used" not in envelope          # no tool metadata on a tool-less run
    assert "tool_iterations" not in envelope
    assert envelope["num_turns"] == 1            # exactly one provider call
    assert envelope["result"] == '{"summary": "real work"}'
    assert envelope["subtype"] == "success"
    assert registry.calls == []                  # nothing executed


def test_registry_is_not_even_imported_when_no_tools_are_requested(monkeypatch):
    """The lazy seam must not be touched on the tool-less path (W-B may not have landed yet)."""
    def _boom():  # pragma: no cover - must never run
        raise AssertionError("the tool registry was imported for a tool-less run")

    monkeypatch.setattr(deepseek_runtime, "_tool_registry", _boom, raising=False)
    server = _Scripted(_completion("plain answer"))
    result = _invoke(server, _make_inv("hi"))
    assert json.loads(result.stdout)["result"] == "plain answer"
    assert server.calls == 1


def test_tool_calls_reply_without_tools_requested_is_still_E_CLI_JSON(monkeypatch):
    """The pre-loop guard survives: a bare tool-call reply is off-contract when we asked for none."""
    server = _Scripted(_completion(None, finish="tool_calls",
                                   tool_calls=[_tool_call("c1", "web_fetch", "{}")]))
    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _make_inv())
    assert excinfo.value.code == E_CLI_JSON


# ─────────────────────────────────────────────────────────────────────────────
# (2) tools on: the specs ride the request, the loop runs
# ─────────────────────────────────────────────────────────────────────────────
def test_tools_on_but_a_direct_answer_costs_one_call(registry):
    server = _Scripted(_completion('{"summary": "no tools needed"}'))
    inv = _make_inv(schema=SUMMARY_SCHEMA, allowed_tools=("web_fetch",))

    result = _invoke(server, inv)
    envelope = json.loads(result.stdout)

    assert server.requests[0]["body"]["tools"] == registry.TOOL_SPECS
    assert envelope["result"] == '{"summary": "no tools needed"}'
    assert envelope["num_turns"] == 1
    assert envelope["tools_used"] == []          # measured zero: tools were on, none ran
    assert registry.calls == []


def test_single_tool_round_trip_executes_the_call_and_answers(registry):
    registry.results["web_fetch"] = "Fetched https://example.test — 12 chars of text."
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", '{"url": "https://example.test"}')]),
        _completion('{"summary": "used the fetch"}'),
    )
    inv = _make_inv(schema=SUMMARY_SCHEMA, allowed_tools=("web_fetch",))

    result = _invoke(server, inv)
    envelope = json.loads(result.stdout)

    # -- executed with the parsed arguments and the invocation's agent_type ----
    assert registry.calls == [("web_fetch", {"url": "https://example.test"}, "competitor_research")]

    # -- the protocol: assistant tool_calls echo + role:"tool" + tool_call_id ---
    assert server.calls == 2
    second = server.messages(1)
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    echo = second[2]
    assert echo["tool_calls"] == [_tool_call("c1", "web_fetch", '{"url": "https://example.test"}')]
    assert second[3] == {"role": "tool", "tool_call_id": "c1",
                         "content": "Fetched https://example.test — 12 chars of text."}
    assert server.requests[1]["body"]["tools"] == registry.TOOL_SPECS   # tools stay in play

    # -- the run's answer is the model's text, never the tool result ----------
    assert envelope["result"] == '{"summary": "used the fetch"}'
    assert envelope["structured_output"] == {"summary": "used the fetch"}
    assert "Fetched https://example.test" not in envelope["result"]
    assert envelope["num_turns"] == 2
    assert envelope["tools_used"] == ["web_fetch"]


def test_a_multi_call_turn_executes_every_call_with_matching_ids(registry):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[
            _tool_call("c1", "web_fetch", '{"url": "https://a.test"}'),
            _tool_call("c2", "macro_snapshot", "{}"),
        ]),
        _completion("done"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",)))

    assert [c[0] for c in registry.calls] == ["web_fetch", "macro_snapshot"]
    tool_messages = [m for m in server.messages(1) if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert json.loads(tool_messages[1]["content"])["tool"] == "macro_snapshot"


def test_reasoning_content_from_a_tool_turn_rides_back_verbatim(registry):
    """Docs: with `tools` present, prior-turn reasoning_content must be passed back in full."""
    server = _Scripted(
        _completion(None, finish="tool_calls", reasoning="step 1: I need the page",
                    tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("done"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",)))

    # No system message here (no schema/system prompt), so find the echo by role.
    echo = next(m for m in server.messages(1) if m["role"] == "assistant")
    assert echo["reasoning_content"] == "step 1: I need the page"


# ─────────────────────────────────────────────────────────────────────────────
# (3) the bound: a loop that never stops is refused BY NAME
# ─────────────────────────────────────────────────────────────────────────────
def test_the_loop_is_capped_and_refuses_by_name(registry):
    endless = [
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call(f"c{i}", "web_fetch", "{}")])
        for i in range(MAX_ITERATIONS)
    ]
    server = _Scripted(*endless)

    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert server.calls == MAX_ITERATIONS                    # never over-calls
    assert len(registry.calls) == MAX_ITERATIONS - 1         # the capped turn is NOT executed
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_tool_loop"
    assert "E_TOOL_LOOP" in envelope["result"]
    assert str(MAX_ITERATIONS) in envelope["result"]
    assert envelope["num_turns"] == MAX_ITERATIONS
    assert envelope["tools_used"] == ["web_fetch"]
    assert result.exit_code != 0

    # the REAL consumers must classify it as a failure and must NOT retry it
    agent = _Agent()
    failure, detail, _ = agent._classify_invoke(result)
    assert failure is structured_output.FailureCode.ENVELOPE_IS_ERROR
    assert "error_tool_loop" in detail
    assert agent._retryable(result) is False


def test_the_cap_is_not_reached_by_a_normal_two_round_conversation(registry):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c2", "macro_snapshot", "{}")]),
        _completion("finished"),
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is False
    assert envelope["num_turns"] == 3
    assert envelope["tools_used"] == ["web_fetch", "macro_snapshot"]


# ─────────────────────────────────────────────────────────────────────────────
# (4) honest failure paths: refusals pass through, garbage never executes
# ─────────────────────────────────────────────────────────────────────────────
def test_a_registry_refusal_is_passed_back_verbatim_and_never_becomes_the_answer(registry):
    refusal = json.dumps({"ok": False, "error": {"code": "E_TOOL_DENIED",
                                                 "message": "web_fetch is not allowed"}})
    registry.results["web_fetch"] = refusal
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("I could not fetch that; reporting without it."),
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    tool_message = [m for m in server.messages(1) if m["role"] == "tool"][0]
    assert tool_message["content"] == refusal            # verbatim: the registry owns the wording
    assert envelope["result"] == "I could not fetch that; reporting without it."
    assert "E_TOOL_DENIED" not in envelope["result"]


def test_unknown_tool_name_reaches_the_registry_and_its_payload_returns_verbatim(registry):
    registry.results["web_surfer"] = "E_TOOL_UNKNOWN: no such tool 'web_surfer'"
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_surfer", "{}")]),
        _completion("reported"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",)))

    assert registry.calls[0][0] == "web_surfer"          # the registry names the refusal
    tool_message = [m for m in server.messages(1) if m["role"] == "tool"][0]
    assert tool_message["content"] == "E_TOOL_UNKNOWN: no such tool 'web_surfer'"


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", "\"a string\"", "42", True])
def test_malformed_arguments_are_named_and_never_executed(registry, arguments):
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", arguments)]),
        _completion("recovered"),
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert registry.calls == []                          # nothing ran on garbage
    tool_message = [m for m in server.messages(1) if m["role"] == "tool"][0]
    assert tool_message["content"].startswith("E_TOOL_ARGS:")
    assert tool_message["tool_call_id"] == "c1"
    assert envelope["result"] == "recovered"


@pytest.mark.parametrize("arguments", ["", None, "{}"])
def test_empty_arguments_mean_no_arguments(registry, arguments):
    """An empty/absent arguments field is the ordinary no-arg call, not garbage."""
    call = _tool_call("c1", "macro_snapshot", "{}")
    if arguments is None:
        del call["function"]["arguments"]
    else:
        call["function"]["arguments"] = arguments
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[call]),
        _completion("done"),
    )
    _invoke(server, _make_inv(allowed_tools=("macro_snapshot",)))
    assert registry.calls == [("macro_snapshot", {}, "competitor_research")]


def test_arguments_already_parsed_as_an_object_are_used_as_is(registry):
    """Some OpenAI-compatible proxies hand back `arguments` as an object, not a JSON string."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", {"url": "https://x.test"})]),
        _completion("done"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    assert registry.calls == [("web_fetch", {"url": "https://x.test"}, "competitor_research")]


def test_a_blanket_denial_wins_over_an_allowlist(registry):
    """`--disallowedTools *` is the explicit no-tools form: it vetoes even a non-empty allowlist."""
    server = _Scripted(_completion("no tools for this one"))
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",), denied=("*",)))

    assert "tools" not in server.requests[0]["body"]
    assert "tools_used" not in json.loads(result.stdout)


def test_a_throwing_execute_tool_never_escapes_the_runtime(monkeypatch):
    reg = _FakeRegistry(raise_for={"web_fetch"})
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: reg, raising=False)
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("recovered anyway"),
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))

    assert json.loads(result.stdout)["result"] == "recovered anyway"
    tool_message = [m for m in server.messages(1) if m["role"] == "tool"][0]
    assert tool_message["content"].startswith("E_TOOL_EXEC:")
    assert "RuntimeError" in tool_message["content"]


def test_a_non_string_tool_result_is_refused_by_name(monkeypatch):
    """The seam TYPES the return as str; anything else must not be smuggled through."""
    reg = _FakeRegistry(returns={"ok": True, "rows": [1, 2]})
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: reg, raising=False)
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("noted"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    tool_message = [m for m in server.messages(1) if m["role"] == "tool"][0]
    assert tool_message["content"].startswith("E_TOOL_CONTRACT:")


@pytest.mark.parametrize("broken", [
    "not-a-dict",
    {"type": "function", "function": {"name": "web_fetch", "arguments": "{}"}},   # no id
    {"id": "c1", "type": "function", "function": "not-a-dict"},
    {"id": "c1", "type": "function", "function": {"arguments": "{}"}},            # no name
    {"id": "c1", "type": "function", "function": {"name": "", "arguments": "{}"}},
])
def test_structurally_broken_tool_calls_are_named_E_CLI_JSON(registry, broken):
    """A call that cannot be answered with a tool_call_id is off-contract — never guessed at."""
    server = _Scripted(_completion(None, finish="tool_calls", tool_calls=[broken]))
    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    assert excinfo.value.code == E_CLI_JSON
    assert registry.calls == []


def test_a_tool_calls_reply_with_no_calls_at_all_is_named(registry):
    server = _Scripted(_completion(None, finish="tool_calls", tool_calls=[]))
    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    assert excinfo.value.code == E_CLI_JSON


def test_a_missing_registry_module_is_a_named_E_CONFIG_refusal(monkeypatch):
    """Tools were requested but W-B's module is not deployed: named, never a raw ImportError."""
    agents_pkg = sys.modules["app.agents"]
    monkeypatch.delattr(agents_pkg, "tool_registry", raising=False)
    monkeypatch.setitem(sys.modules, "app.agents.tool_registry", None)  # forces ImportError
    server = _Scripted(_completion("never reached"))

    with pytest.raises(PolsiaError) as excinfo:
        _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    assert excinfo.value.code == E_CONFIG
    assert "tool_registry" in str(excinfo.value)
    assert server.calls == 0


def test_a_transport_failure_mid_loop_keeps_the_measured_usage(registry):
    """Tokens already spent by executed tool turns must not vanish on a later provider failure."""
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", "web_fetch", "{}")], usage=_usage(10, 5, 2, 8)),
        500,
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert envelope["is_error"] is True
    assert envelope["api_error_status"] == 500
    assert envelope["usage"]["input_tokens"] == 10
    assert envelope["usage"]["output_tokens"] == 5
    assert envelope["num_turns"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# (5) usage / cost accounting across turns
# ─────────────────────────────────────────────────────────────────────────────
def test_usage_and_cost_are_summed_across_tool_turns(registry):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")],
                    usage=_usage(10, 5, 2, 8)),
        _completion('{"summary": "done"}', usage=_usage(20, 9, 4, 16)),
    )
    result = _invoke(server, _make_inv(schema=SUMMARY_SCHEMA, allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert envelope["usage"] == {"input_tokens": 30, "output_tokens": 14,
                                 "cache_read_input_tokens": 6}
    assert envelope["num_turns"] == 2
    assert envelope["pricing_version"] == pricing.PRICING_VERSION
    assert isinstance(envelope["cost_estimate_usd"], float)

    agent = _Agent()
    raw = agent._finish_call(result, 1, transport="http")
    assert agent.last_usage is not None
    assert agent.last_usage.input_tokens == 30
    assert agent.last_usage.output_tokens == 14
    assert agent.last_usage.num_turns == 2
    assert raw.text == '{"summary": "done"}'


def test_one_unmeasured_turn_makes_the_whole_aggregate_unmeasured(registry):
    """A partial sum would be fabrication-precision: measured-or-absent, never mixed."""
    missing = _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")])
    missing.pop("usage")
    server = _Scripted(missing, _completion('{"summary": "done"}', usage=_usage(20, 9, 4, 16)))
    result = _invoke(server, _make_inv(schema=SUMMARY_SCHEMA, allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert envelope["usage"] == {"input_tokens": None, "output_tokens": None,
                                 "cache_read_input_tokens": None}
    assert "cost_estimate_usd" not in envelope      # unknown tokens -> unknown money
    agent = _Agent()
    raw = agent._finish_call(result, 1, transport="http")
    assert raw.text == '{"summary": "done"}'
    assert agent.last_usage is not None and agent.last_usage.input_tokens is None


# ─────────────────────────────────────────────────────────────────────────────
# (6) the seam contract: agent_type, the explicit kwarg, the recorded names
# ─────────────────────────────────────────────────────────────────────────────
def test_agent_type_rides_from_the_invocation_and_a_kwarg_wins(registry):
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("done"),
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c2", "web_fetch", "{}")]),
        _completion("done again"),
    )
    _invoke(server, _make_inv(allowed_tools=("web_fetch",), agent_type="competitor_research"))
    assert registry.calls[-1][2] == "competitor_research"

    _invoke(server, _make_inv(allowed_tools=("web_fetch",), agent_type="competitor_research"),
            agent_type="finance")
    assert registry.calls[-1][2] == "finance"


def test_an_explicit_tools_kwarg_activates_the_loop_without_argv_flags(registry):
    server = _Scripted(_completion("answered without tools"))
    result = _invoke(server, _make_inv(), tools=registry.TOOL_SPECS)
    assert server.requests[0]["body"]["tools"] == registry.TOOL_SPECS
    assert json.loads(result.stdout)["tools_used"] == []


def test_recorded_tool_names_are_truncated_but_the_executed_name_is_exact(registry):
    long_name = "webfetch_" + "x" * 300
    registry.results[long_name] = "irrelevant"
    server = _Scripted(
        _completion(None, finish="tool_calls",
                    tool_calls=[_tool_call("c1", long_name, "{}")]),
        _completion("done"),
    )
    result = _invoke(server, _make_inv(allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    assert registry.calls[0][0] == long_name            # the registry saw the exact name
    recorded = envelope["tools_used"][0]
    assert 0 < len(recorded) < len(long_name)           # truncation-safe metadata
    assert recorded.startswith("webfetch_")


def test_a_tool_loop_survives_being_resumed(registry):
    """The stored exchange for a resumed run carries the tool turns it really had."""
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion("first answer", usage=_usage()),
        _completion("second answer", usage=_usage()),
    )
    inv = _make_inv("q1", allowed_tools=("web_fetch",))
    first = _invoke(server, inv)
    sid = json.loads(first.stdout)["session_id"]

    _invoke(server, _make_inv("q2", allowed_tools=("web_fetch",), resume=sid))
    roles = [m["role"] for m in server.messages(2)]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    assert server.messages(2)[1]["tool_calls"][0]["id"] == "c1"


def test_the_result_envelope_has_the_documented_keys_for_a_tool_run(registry):
    """base_agent parses `result`/`structured_output`/`is_error`/`subtype` off this shape."""
    server = _Scripted(
        _completion(None, finish="tool_calls", tool_calls=[_tool_call("c1", "web_fetch", "{}")]),
        _completion('{"summary": "ok"}'),
    )
    result = _invoke(server, _make_inv(schema=SUMMARY_SCHEMA, allowed_tools=("web_fetch",)))
    envelope = json.loads(result.stdout)

    for key in ("type", "subtype", "is_error", "result", "session_id", "num_turns",
                "duration_ms", "usage", "model", "stop_reason", "provider", "tools_used"):
        assert key in envelope
    assert envelope["provider"] == "deepseek"
    assert envelope["stop_reason"] == "stop"
    assert envelope["is_error"] is False


# ─────────────────────────────────────────────────────────────────────────────
# (7) W-B's real module, when it has landed
# ─────────────────────────────────────────────────────────────────────────────
def test_the_real_registry_seam_matches_the_frozen_interface():
    """importorskip: this is the ONLY test that reads W-B's module, and it pins the contract."""
    tool_registry = pytest.importorskip("app.agents.tool_registry")

    specs = tool_registry.TOOL_SPECS
    assert isinstance(specs, list) and specs
    for spec in specs:
        assert spec["type"] == "function"
        assert isinstance(spec["function"]["name"], str) and spec["function"]["name"]
        assert isinstance(spec["function"]["parameters"], dict)
    assert {s["function"]["name"] for s in specs} >= {"web_fetch", "macro_snapshot",
                                                      "pricing_catalog"}

    sig = inspect.signature(tool_registry.execute_tool)
    # V24/L2: `task_title` is the OPTIONAL keyword the executor uses for web_search's
    # ori_question — keyword-only with a None default, so every pre-V24 call site is unchanged.
    assert list(sig.parameters) == ["name", "arguments", "agent_type", "task_title"]
    assert sig.parameters["agent_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["task_title"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["task_title"].default is None
    assert sig.return_annotation in (str, "str")
    assert deepseek_runtime._tool_registry() is tool_registry   # the lazy seam resolves it
