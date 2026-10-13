"""RD8 — the DeepSeek runtime: request shape, envelope contract, ladder taxonomy, refusals.

Every test here runs WITHOUT network: the HTTP seam is an `httpx.MockTransport`, and the frozen
key interface (`deepseek_runtime._resolve_api_key`) is patched — so the suite passes whether or
not the parallel `secret_store` seat has landed. The base_agent assertions import the REAL
classifier (`_classify_invoke`, `_retryable`, `_finish_call`) — the envelope this runtime emits
is only correct if THOSE functions consume it with no changes.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, structured_output
from app.agents.base_agent import BasePolsiaAgent
from app.core.errors import E_CLI_JSON, E_CLI_TIMEOUT, E_DEEPSEEK_KEY, E_VALIDATION, PolsiaError

SAFE_TEST_KEY = "sk-test-not-a-real-key"


class _Agent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


class _SettingsProxy:
    """Answers overridden `deepseek_*` reads, delegates everything else to the real settings.

    Needed because pydantic BaseSettings refuses `setattr` for fields that have not landed yet
    (the parallel Seat 3 adds them): the runtime's module-global `settings` is swapped instead,
    so these tests pass BEFORE and AFTER Seat 3 — `_setting()` reads through this either way.
    """

    def __init__(self, base, overrides: dict):
        self.__dict__["_base"] = base
        self.__dict__["_overrides"] = overrides

    def __getattr__(self, name):  # only called when normal lookup misses
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def _override_setting(monkeypatch, name: str, value) -> None:
    """Simulate one `deepseek_*` Settings field for this test (chains across calls).

    Before Seat 3 lands, pydantic refuses `setattr` for the unknown field, so the runtime's
    module-global `settings` is swapped for a proxy that answers this read. Once the field
    exists, the real settings object is patched directly. Both routes go through `_setting()`.
    """
    base = deepseek_runtime.settings
    try:
        getattr(base, name)
    except AttributeError:  # field not in Settings yet (parallel seat)
        monkeypatch.setattr(deepseek_runtime, "settings", _SettingsProxy(base, {name: value}))
        return
    monkeypatch.setattr(base, name, value)


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


def _make_inv(prompt: str = "do the thing", *, schema: dict | None = None, system: str = "",
              resume: str | None = None) -> claude_cli.Invocation:
    """An Invocation from the ONLY argv builder — the runtime must accept it unchanged."""
    return claude_cli.build_invocation(
        prompt=prompt,
        json_schema=schema,
        system_prompt_static=system,
        resume_session=resume,
        timeout_s=30,
        schema_name="test",
    )


def _completion(content: str, *, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "finish_reason": finish,
            "message": {"role": "assistant", "content": content},
        }],
        "usage": usage if usage is not None else {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "prompt_cache_hit_tokens": 3,
        },
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


# ─────────────────────────────────────────────────────────────────────────────
# (a) missing key -> named E_DEEPSEEK_KEY refusal, how_to_fix points at Settings
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_key_is_a_named_refusal_with_the_settings_hint(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: None)

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(lambda request: httpx.Response(500)))

    assert excinfo.value.code == E_DEEPSEEK_KEY
    assert "Settings" in excinfo.value.how_to_fix
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)  # the env var NAME, never a value


def test_available_follows_the_frozen_key_interface(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "k")
    assert deepseek_runtime.available() is True
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: None)
    assert deepseek_runtime.available() is False


def test_resolve_api_key_uses_the_secret_store(monkeypatch):
    """The frozen interface contract: `secret_store.get_deepseek_api_key()` is the source."""
    secret_store = pytest.importorskip("app.services.secret_store")  # skips pre-Seat-2
    monkeypatch.setattr(secret_store, "get_deepseek_api_key", lambda: "stored-key")
    assert deepseek_runtime._resolve_api_key() == "stored-key"


def test_resolve_api_key_falls_back_to_env_without_the_store(monkeypatch):
    """Before/without `secret_store`, the env var alone decides — and absence is None."""
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "app.services.secret_store", None)  # force ImportError
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    assert deepseek_runtime._resolve_api_key() == "env-key"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert deepseek_runtime._resolve_api_key() is None


# ─────────────────────────────────────────────────────────────────────────────
# (b) happy path — the wire request and the envelope base_agent consumes
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_request_body_and_envelope_contract(monkeypatch):
    _override_setting(monkeypatch, "deepseek_model", "deepseek-flash")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        payload = json.dumps({"summary": "real work"})
        return httpx.Response(200, json=_completion(payload))

    result = deepseek_runtime.invoke(
        _make_inv("do the thing", schema=SUMMARY_SCHEMA, system="You are the test agent."),
        client=_client(handler),
        api_key=SAFE_TEST_KEY,
    )

    # -- the wire request ------------------------------------------------
    request = captured["request"]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {SAFE_TEST_KEY}"
    assert request.headers["content-type"].startswith("application/json")

    body = captured["body"]
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    # effort default "none" DISABLES thinking (no reasoning pass for deterministic JSON runs)
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body

    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert "You are the test agent." in messages[0]["content"]
    # docs warning: json_object MUST come with an instruction; the schema rides in it
    assert '"summary"' in messages[0]["content"]
    assert "JSON" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "do the thing"}

    # -- the envelope base_agent actually parses -------------------------
    assert result.exit_code == 0
    assert result.transport == "http"
    assert SAFE_TEST_KEY not in result.stdout  # a key must never ride the envelope
    envelope = structured_output.extract_envelope(result.stdout)  # the real parser
    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is False
    assert envelope["result"] == json.dumps({"summary": "real work"})
    assert envelope["structured_output"] == {"summary": "real work"}
    assert envelope["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
    }
    assert envelope["model"] == "deepseek-flash"
    assert envelope["num_turns"] == 1
    assert isinstance(envelope["duration_ms"], int)
    assert envelope["session_id"].startswith("deepseek-")
    for key in ("result", "structured_output", "is_error", "subtype",
                "usage", "num_turns", "duration_ms", "session_id", "model"):
        assert key in envelope

    # -- the REAL base_agent consumers agree: no failure, no retry, text + payload flow
    agent = _Agent()
    failure, detail, _ = agent._classify_invoke(result)
    assert failure is None, detail
    assert agent._retryable(result) is False
    raw = agent._finish_call(result, 1, transport="http")
    assert raw.text == json.dumps({"summary": "real work"})
    assert raw.envelope["structured_output"] == {"summary": "real work"}
    assert agent.last_usage is not None and agent.last_usage.input_tokens == 11
    assert agent.session_id == envelope["session_id"]

    resolution = structured_output.resolve_output(
        stdout=result.stdout, exit_code=0, validator=lambda payload: None
    )
    assert resolution.ok and resolution.payload == {"summary": "real work"}


def test_store_key_is_used_for_the_auth_header(monkeypatch):
    """The frozen resolver (stored key -> env) is what feeds the Authorization header."""
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "key-from-store")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_completion('{"summary": "x"}'))

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler))
    assert seen["auth"] == "Bearer key-from-store"
    assert "key-from-store" not in result.stdout


def test_plain_text_call_omits_json_mode_and_system_message(monkeypatch):
    _override_setting(monkeypatch, "deepseek_model", "deepseek-flash")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("plain answer"))

    result = deepseek_runtime.invoke(_make_inv("hi"), client=_client(handler), api_key=SAFE_TEST_KEY)
    body = captured["body"]
    assert "response_format" not in body
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert json.loads(result.stdout)["result"] == "plain answer"


def test_model_and_base_url_follow_settings(monkeypatch):
    _override_setting(monkeypatch, "deepseek_model", "deepseek-v4-pro")
    _override_setting(monkeypatch, "deepseek_base_url", "https://alt.example/v1/")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("ok"))

    deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert captured["body"]["model"] == "deepseek-v4-pro"
    assert captured["url"] == "https://alt.example/v1/chat/completions"  # rstrip("/") applied


def test_reasoning_effort_canonicalizes_and_from_settings(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("ok"))

    _override_setting(monkeypatch, "deepseek_reasoning_effort", "minimal")
    deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "low"  # documented alias canonicalized

    _override_setting(monkeypatch, "deepseek_reasoning_effort", "bogus")
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_VALIDATION  # never silently coerced


# ─────────────────────────────────────────────────────────────────────────────
# (c) HTTP taxonomy — every mapped status lands in the ladder's own classifier
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "subtype", "retryable"), [
    (400, "error_invalid_request", False),
    (401, "error_during_execution", False),
    (402, "error_insufficient_balance", False),
    (429, "error_rate_limit", True),
    (500, "error_server_error", True),
    (503, "error_overloaded", True),
])
def test_http_status_taxonomy_through_base_agent(status, subtype, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"status {status}"}})

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)

    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == subtype
    assert envelope["api_error_status"] == status
    assert result.exit_code != 0
    assert SAFE_TEST_KEY not in result.stdout and SAFE_TEST_KEY not in result.stderr

    agent = _Agent()
    failure, _, _ = agent._classify_invoke(result)
    assert failure is not None
    assert agent._retryable(result) is retryable


def test_auth_failure_classifies_as_cli_auth_failed():
    """401/403 reuse the EXISTING auth classification (mirrors 'refresh the CLI auth')."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Authentication Fails"}})

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    agent = _Agent()
    failure, detail, _ = agent._classify_invoke(result)
    assert failure is structured_output.FailureCode.CLI_AUTH_FAILED
    assert "401" in detail
    assert agent._retryable(result) is False  # do not retry until the key is fixed


# ─────────────────────────────────────────────────────────────────────────────
# (d) timeout / transport-error semantics mirror claude_cli.invoke
# ─────────────────────────────────────────────────────────────────────────────
def test_read_timeout_raises_E_CLI_TIMEOUT_like_the_cli_wall_clock(monkeypatch):
    _override_setting(monkeypatch, "deepseek_timeout_seconds", 7)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)

    assert excinfo.value.code == E_CLI_TIMEOUT
    assert "7s" in str(excinfo.value)
    assert "deepseek_timeout_seconds" in excinfo.value.how_to_fix


def test_connect_error_is_a_retryable_failure_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_api_error"
    assert envelope["api_error_status"] is None
    assert "ConnectError" in envelope["result"]
    assert _Agent()._retryable(result) is True  # the api_error marker = transient network


def test_run_with_retries_retries_a_raised_deepseek_timeout(monkeypatch):
    """The ladder's E_CLI_TIMEOUT retry (claude_cli.run_with_retries) works for this runtime."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("first attempt times out")
        return httpx.Response(200, json=_completion("second attempt"))

    client = _client(handler)

    def dispatch() -> claude_cli.InvokeResult:
        return deepseek_runtime.invoke(_make_inv(), client=client, api_key=SAFE_TEST_KEY)

    result, attempts = claude_cli.run_with_retries(dispatch, sleep=lambda seconds: None)
    assert attempts == 2
    assert result.exit_code == 0
    assert json.loads(result.stdout)["result"] == "second attempt"


# ─────────────────────────────────────────────────────────────────────────────
# (e) truncation — finish_reason == "length" is a named failure, never a success
# ─────────────────────────────────────────────────────────────────────────────
def test_finish_reason_length_is_a_named_failure_not_a_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('{"summary": "par', finish="length"))

    result = deepseek_runtime.invoke(_make_inv(schema=SUMMARY_SCHEMA), client=_client(handler),
                                     api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_max_tokens"
    assert "structured_output" not in envelope  # nothing fabricated from a truncated body
    assert "truncated" in envelope["result"]

    agent = _Agent()
    failure, detail, _ = agent._classify_invoke(result)
    assert failure is structured_output.FailureCode.ENVELOPE_IS_ERROR
    assert "error_max_tokens" in detail
    assert agent._retryable(result) is False  # same prompt would truncate again
    with pytest.raises(PolsiaError) as excinfo:
        agent._finish_call(result, 1, transport="http")
    assert "error_max_tokens" in str(excinfo.value)


def test_truncation_with_no_content_still_reports_truncation():
    """Thinking-mode truncation can return content=None; that is still truncation, not E_CLI_JSON."""
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _completion("", finish="length")
        payload["choices"][0]["message"]["content"] = None
        return httpx.Response(200, json=payload)

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert json.loads(result.stdout)["subtype"] == "error_max_tokens"


# ─────────────────────────────────────────────────────────────────────────────
# 200-with-a-wrong-body is E_CLI_JSON (mirrors 'stdout was not the documented envelope')
# ─────────────────────────────────────────────────────────────────────────────
def test_non_json_200_body_is_a_named_E_CLI_JSON_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway portal</html>")

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON


def test_200_without_choices_is_a_named_E_CLI_JSON_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {}})

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON


# ─────────────────────────────────────────────────────────────────────────────
# resume replay — the repair ladder's R1/R2 depend on the previous exchange
# ─────────────────────────────────────────────────────────────────────────────
def test_resume_replays_the_previous_exchange():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_completion(f"answer {len(bodies)}"))

    client = _client(handler)
    first = deepseek_runtime.invoke(_make_inv("first question"), client=client, api_key=SAFE_TEST_KEY)
    sid = json.loads(first.stdout)["session_id"]

    deepseek_runtime.invoke(_make_inv("second question", resume=sid), client=client,
                            api_key=SAFE_TEST_KEY)
    assert bodies[1]["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "second question"},
    ]


def test_unknown_resume_id_runs_fresh_and_keeps_the_session_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("ok"))

    result = deepseek_runtime.invoke(_make_inv("q", resume="deepseek-does-not-exist"),
                                     client=_client(handler), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["session_id"] == "deepseek-does-not-exist"


# ─────────────────────────────────────────────────────────────────────────────
# plan_from_invocation — the "accepts the same Invocation" contract
# ─────────────────────────────────────────────────────────────────────────────
def test_plan_recovers_prompt_system_schema_and_resume():
    inv = _make_inv("p", schema=SUMMARY_SCHEMA, system="sys", resume="sid-1")
    plan = deepseek_runtime.plan_from_invocation(inv)
    assert plan.prompt == "p"
    assert plan.system_prompt == "sys"
    assert plan.json_schema == SUMMARY_SCHEMA
    assert plan.resume_session_id == "sid-1"


def test_plan_reads_the_stdin_transport():
    inv = claude_cli.build_invocation(prompt="the big prompt", prompt_via_stdin=True, timeout_s=5)
    assert deepseek_runtime.plan_from_invocation(inv).prompt == "the big prompt"


def test_plan_without_a_prompt_is_a_named_refusal():
    inv = claude_cli.Invocation(argv=["claude", "--output-format", "json"])
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.plan_from_invocation(inv)
    assert excinfo.value.code == E_VALIDATION
