"""W2 / RT-01 + RT-03 + RT-04 — a RAISED mid-loop failure must keep its journal and its usage.

The defect (runtime hunter, VERIFIED at HEAD): `deepseek_runtime.invoke` kept the tool loop's
state (numerator turns, `tools_used`, the action journal) in locals.  The two failure shapes that
`claude_cli.run_with_retries` RETRIES — a transport fault and a non-200 status — were converted
into an envelope carrying `_loop_evidence()`, but every RAISED refusal (`E_CLI_TIMEOUT`, the
non-JSON 200, the in-loop `E_CLI_JSON` shapes) went straight up with only its own context.  So:

  * `base_agent._real_call.dispatch` never saw the actions an attempt really produced, its
    `run_actions` replay list stayed empty, and the ladder's retry re-executed a tool that had
    already run (duplicate `github_commit` / PR / send);
  * the refusal the worker later persists carried no actions at all
    (`_common.actions_from_outcome(None, failure) == []`), so the NEXT delivery's replay list was
    empty too — the journal existed only in the dead attempt's memory;
  * the tokens the loop already measured were dropped, because `base_agent.measured_usage_on_failure`
    was reachable only from `_finish_call` (RT-03);
  * and even on the one seam that DID carry usage, `measured_usage_on_failure` reused
    `usage_metadata()`'s placeholders, so a failed run's audit copy claimed `context_tokens=0` /
    `stable_prefix_hash=""` / a `token_estimate` cost with a NULL estimate (RT-04).

What is pinned here, against the REAL runtime + REAL `BaseMeridianAgent._real_call` + the REAL
ladder.  The only doubles are the tree's own test seams: `httpx.MockTransport` (the provider) and a
counting registry installed on `deepseek_runtime._tool_registry` (the frozen executor seam, exactly
as `test_a3_writepath.py` does).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import accounting, claude_cli, deepseek_runtime, pricing
from app.agents.base_agent import BaseMeridianAgent, usage_from_outcome
from app.config import settings
from app.core.errors import E_AGENT_BAD_OUTPUT, E_CLI_TIMEOUT, MeridianError
from celery_app.tasks import _common

SAFE_TEST_KEY = "test-key-never-a-real-secret"
FETCH_ARGS = json.dumps({"url": "https://example.test/w2-page"})
FETCH_RESULT = "W2 page text — the tool really ran."


class _ToolAgent(BaseMeridianAgent):
    """A real subclass; only the runtime/retry seam is exercised (`run` is never dispatched)."""

    agent_type = "competitor_research"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "unused"}


class _Registry:
    """The frozen registry seam: exposes TOOL_SPECS, counts every execution, answers verbatim."""

    TOOL_SPECS = [
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch one http(s) URL and return its text.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                           "required": ["url"]},
        }},
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute_tool(self, name, arguments, *, agent_type, **_kwargs):
        self.calls.append((name, arguments))
        return FETCH_RESULT


def _usage() -> dict:
    return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8}


def _completion(content, *, finish: str = "stop", tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "chat-w2", "object": "chat.completion", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": _usage()}


def _tool_turn(call_id: str = "c1") -> dict:
    return _completion(
        None, finish="tool_calls",
        tool_calls=[{"id": call_id, "type": "function",
                     "function": {"name": "web_fetch", "arguments": FETCH_ARGS}}],
    )


class _Scripted:
    """A scripted provider: each request pops the next payload; an exception is RAISED."""

    def __init__(self, *payloads) -> None:
        self.queue = list(payloads)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        payload = self.queue.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return httpx.Response(200, json=payload)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def calls(self) -> int:
        return len(self.requests)


def _invocation():
    """The Invocation the ONLY argv builder emits for a DeepSeek tool run."""
    return claude_cli.build_invocation(
        prompt="do the thing", allowed_tools=("web_fetch",), timeout_s=30,
        schema_name="competitor_research",
    )


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The runtime's session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


@pytest.fixture()
def registry(monkeypatch):
    reg = _Registry()
    monkeypatch.setattr(deepseek_runtime, "_tool_registry", lambda: reg, raising=False)
    return reg


def _agent_path(monkeypatch, server: _Scripted, *, retries: int) -> None:
    """Wire the REAL `_real_call` to a scripted provider (the documented injection seams only)."""
    real_invoke = deepseek_runtime.invoke

    def observed(inv, **kwargs):
        return real_invoke(inv, client=server.client(), api_key=SAFE_TEST_KEY, **kwargs)

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    monkeypatch.setattr(deepseek_runtime, "invoke", observed)
    monkeypatch.setattr(claude_cli, "backoff_delay", lambda attempt, **_kwargs: 0.0)
    monkeypatch.setattr(settings, "agent_max_retries", retries)


# ═════════════════════════════════════════════════════════════════════════════════════════════
# RT-01 — the raised-refusal journal reaches the retry seam, so the tool is executed ONCE
# ═════════════════════════════════════════════════════════════════════════════════════════════

def test_a_raised_timeout_after_a_tool_turn_does_not_execute_the_tool_again(registry, monkeypatch):
    """The defect in one run: tool executes, the NEXT provider call raises, the ladder retries.

    At HEAD the retry's `replay` was `[]` because `run_actions` was only extended from a RETURNED
    envelope — the registry executed the same `call_key` a second time (the measured duplicate
    `github_commit`/PR class).  After the fix the raised refusal carries the journal, `dispatch`
    seeds `run_actions` from `exc.context["actions"]`, and attempt 2 REPLAYS it.
    """
    server = _Scripted(
        _tool_turn("c1"), httpx.ReadTimeout("attempt 1: provider read timed out"),
        _tool_turn("c2"), httpx.ReadTimeout("attempt 2: provider read timed out"),
    )
    _agent_path(monkeypatch, server, retries=1)

    with pytest.raises(MeridianError) as caught:
        _ToolAgent()._real_call("do the thing")

    assert caught.value.code == E_CLI_TIMEOUT
    assert len(registry.calls) == 1, (
        f"the retried attempt re-executed the tool: {registry.calls!r}"
    )
    assert server.calls == 4, "two dispatches, two provider calls each"
    actions = caught.value.context["actions"]
    assert [record["status"] for record in actions] == ["executed"], actions
    assert actions[0]["call_key"].startswith("web_fetch|")
    assert actions[0]["result"] == FETCH_RESULT
    # the journal reached the persistence READER the worker uses (a failed run has no output):
    assert _common.actions_from_outcome({}, caught.value) == actions


def test_a_total_failure_refusal_carries_the_actions_and_usage_for_the_worker(registry, monkeypatch):
    """On total failure the refusal context is the ONLY carrier the worker can persist (F-13).

    `agent_tasks` persists `_common.actions_from_outcome(None, failure)` to `tasks.metadata` and
    `usage_from_outcome` is the accounting reader.  Both must be non-empty when a tool ran and the
    provider measured tokens before raising.
    """
    server = _Scripted(
        _tool_turn("c1"), httpx.ReadTimeout("provider read timed out"),
    )
    _agent_path(monkeypatch, server, retries=0)
    agent = _ToolAgent()

    with pytest.raises(MeridianError) as caught:
        agent._real_call("do the thing")

    refusal = caught.value
    assert refusal.code == E_CLI_TIMEOUT
    actions = refusal.context["actions"]
    assert len(actions) == 1 and actions[0]["status"] == "executed"
    # the WORKER's readers see the same records (the persistence seam the retry needs)
    assert _common.actions_from_outcome({}, refusal)[0]["call_key"] == actions[0]["call_key"]
    assert _common.merge_actions([], actions)[0]["status"] == "executed"
    # and the agent's own run state carries what happened, not only the exception
    assert agent.last_actions == actions

    usage = usage_from_outcome({}, refusal)
    assert usage["tokens_input"] == 10, "the loop's measured prompt tokens must survive"
    assert usage["tokens_output"] == 5
    assert usage["tokens_used"] == 15
    assert usage["is_error"] is True and usage["error_code"] == E_CLI_TIMEOUT
    # RT-04: a failed run's prompt size/hash were never measured — no fabricated 0 / ""
    assert "context_tokens" not in usage
    assert "stable_prefix_hash" not in usage
    assert "repaired_by" not in usage
    assert "cli_attempts" not in usage
    # RT-04: the audit copy must reproduce the same cost it stores
    assert usage["cost_usd"] > 0
    assert usage["usage_json"]["cost_source"] == "token_estimate"
    assert usage["usage_json"]["pricing_version"] == pricing.PRICING_VERSION
    assert usage["usage_json"]["token_cost_estimate_usd"] == pytest.approx(usage["cost_usd"])


def test_the_runtime_raised_refusal_itself_carries_the_journal_and_usage(registry):
    """The runtime's half of RT-01: the raise that happens INSIDE the loop carries the evidence."""
    server = _Scripted(_tool_turn("c1"), httpx.ReadTimeout("read timed out"))

    with pytest.raises(MeridianError) as caught:
        deepseek_runtime.invoke(
            _invocation(), client=server.client(), api_key=SAFE_TEST_KEY, attempt=1,
        )

    context = caught.value.context
    assert context["tools_used"] == ["web_fetch"]
    assert [record["status"] for record in context["actions"]] == ["executed"]
    assert context["actions"][0]["call_key"].startswith("web_fetch|")
    assert context["usage"] == {
        "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2,
    }, "the measured tokens of the completed tool turn, in envelope shape"


def test_a_tool_less_raised_refusal_keeps_its_exact_context_shape():
    """Byte-compatibility: a tool-less run gains no NEW context keys (no fabricated actions/usage)."""
    server = _Scripted(httpx.ReadTimeout("read timed out"))
    inv = claude_cli.build_invocation(prompt="hi", timeout_s=30, schema_name="competitor_research")

    with pytest.raises(MeridianError) as caught:
        deepseek_runtime.invoke(inv, client=server.client(), api_key=SAFE_TEST_KEY)

    assert set(caught.value.context) == {"timeout_s", "url", "runtime"}


# ═════════════════════════════════════════════════════════════════════════════════════════════
# RT-03 — every raised refusal seam carries the measured usage
# ═════════════════════════════════════════════════════════════════════════════════════════════

def _measured_agent(input_tokens: int = 900, output_tokens: int = 120) -> _ToolAgent:
    agent = _ToolAgent()
    agent.last_usage = accounting.usage_from_envelope({
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_read_input_tokens": 0},
        "model": "deepseek-flash",
    })
    return agent


def test_the_repair_guard_carries_the_attempt_zero_usage():
    """F-23's guard is a RAISE outside `_finish_call`; F-20's usage carrier must reach it too."""
    agent = _measured_agent()
    assert agent.session_id is None

    with pytest.raises(MeridianError) as caught:
        agent._require_repair_input()

    assert caught.value.code == E_AGENT_BAD_OUTPUT
    usage = caught.value.context["usage"]
    assert usage["tokens_input"] == 900
    assert usage["tokens_output"] == 120
    assert usage["is_error"] is True
    assert usage["error_code"] == E_AGENT_BAD_OUTPUT


def test_the_repair_guard_carries_the_actions_when_the_run_made_any():
    """Same mirror rule for F-13: known effects ride the refusal, never only the agent's memory."""
    agent = _measured_agent()
    record = {"call_key": "web_fetch|abc", "tool": "web_fetch", "status": "executed",
              "args": {}, "run_id": None, "attempt": 1, "result": "x", "result_digest": "x"}
    agent.last_actions = [record]

    with pytest.raises(MeridianError) as caught:
        agent._require_repair_input()

    assert caught.value.context["actions"] == [record]


def test_an_unmeasured_guard_refusal_gains_no_fabricated_accounting_keys():
    """Negative control: no measurement -> no usage key (NULL, never a fabricated zero)."""
    agent = _ToolAgent()
    with pytest.raises(MeridianError) as caught:
        agent._require_repair_input()
    assert "usage" not in caught.value.context
    assert "actions" not in caught.value.context


# ═════════════════════════════════════════════════════════════════════════════════════════════
# RT-04 — the failed-run usage dict fabricates no measurement it never took
# ═════════════════════════════════════════════════════════════════════════════════════════════

def test_failed_run_usage_has_no_fabricated_context_size_or_prefix_hash():
    agent = _measured_agent()

    meta = agent.measured_usage_on_failure(E_AGENT_BAD_OUTPUT)

    assert meta["tokens_input"] == 900
    assert meta["cost_usd"] > 0
    # never measured for a failed run: omit, do not publish a placeholder
    assert "context_tokens" not in meta, "a failed run's prompt size is unknown, not 0"
    assert "stable_prefix_hash" not in meta, "a failed run assembled no context to hash"
    assert "repaired_by" not in meta
    assert "cli_attempts" not in meta


def test_failed_run_usage_audit_copy_reproduces_the_stored_cost():
    """`usage_json` must not claim a token estimate whose own number is NULL (RT-04)."""
    agent = _measured_agent()

    meta = agent.measured_usage_on_failure(E_AGENT_BAD_OUTPUT)

    assert meta["usage_json"]["cost_source"] == "token_estimate"
    assert meta["usage_json"]["token_cost_estimate_usd"] == pytest.approx(meta["cost_usd"])


def test_an_unpriced_failed_run_does_not_claim_a_token_estimate():
    """If the pinned table cannot price the model, the audit copy says UNKNOWN, never `token_estimate`."""
    agent = _ToolAgent()
    agent.last_usage = accounting.usage_from_envelope({
        "usage": {"input_tokens": 900, "output_tokens": 120},
        "model": "some-unpinned-model",
    })

    meta = agent.measured_usage_on_failure(E_AGENT_BAD_OUTPUT)

    assert "cost_usd" not in meta
    assert meta["cost_estimate_usd"] is None
    assert meta["usage_json"]["token_cost_estimate_usd"] is None
    assert meta["usage_json"]["cost_source"] is None, "no source may be claimed without an estimate"
