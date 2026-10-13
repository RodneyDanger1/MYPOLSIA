"""RD13 — adversarial edge sweep: the DeepSeek runtime's parse, error mapping and refusals.

Read-verified facts encoded here (docs enum for `finish_reason`:
stop | length | content_filter | tool_calls | insufficient_system_resource | aborted):

* a non-200 whose status is NOT in the runtime's table must classify by FAMILY — unknown
  5xx > `error_server_error` (retryable), everything else (404/3xx/418/451) >
  `error_invalid_request` (NOT retryable). Borrowing the `api_error` transport marker for a
  404 made the ladder retry a request that never reached a model (`error_api_error` is the
  documented TRANSPORT flag).
* `content_filter` ("content was omitted due to a flag from our content filters"),
  `insufficient_system_resource` ("request interrupted due to insufficient resource of the
  inference system") and `aborted` ("generation was interrupted") are NOT finished answers:
  each maps to a named failure subtype; only `error_overloaded` carries a transient marker
  (resource pressure can clear on a retry).
* `choices[0]` variants, a content-parts LIST (documented for REQUEST messages only), a
  reasoning-only message and a tool-calls reply with null content are all refused by NAME
  (E_CLI_JSON) — never a crash, never a fabricated payload.
* `usage` values that are not real integers collapse to None (measured-by-absence) instead of
  crashing `accounting._int_or_none` inside the REAL `base_agent._finish_call`.
* a misconfigured `DEEPSEEK_BASE_URL` is a named E_VALIDATION refusal; httpx's raw
  `ValueError`/`InvalidURL` must never escape the runtime.
* an endpoint echoing the Authorization header must not leak the key into a failure envelope.

No network: every call runs through `httpx.MockTransport`; the key interface is patched.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, structured_output
from app.agents.base_agent import BasePolsiaAgent
from app.core.errors import E_CLI_JSON, E_CLI_TIMEOUT, E_VALIDATION, PolsiaError

#: Clearly fake, shaped like the real thing (the raw value never rides a real request here).
SAFE_TEST_KEY = "sk-rd13-fake-0000-A1B2C3D4"


class _Agent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


@pytest.fixture(autouse=True)
def _clean_session_store():
    """The session store is process-global by design; keep each test hermetic."""
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


def _set_setting(monkeypatch, name: str, value) -> None:
    """Patch one real `deepseek_*` Settings field for this test (fields exist as of RD8 seat 3)."""
    monkeypatch.setattr(deepseek_runtime.settings, name, value)


def _make_inv(prompt: str = "do the thing", *, schema: dict | None = None, system: str = "",
              resume: str | None = None) -> claude_cli.Invocation:
    return claude_cli.build_invocation(
        prompt=prompt,
        json_schema=schema,
        system_prompt_static=system,
        resume_session=resume,
        timeout_s=30,
        schema_name="test",
    )


def _completion(content, *, finish: str = "stop", usage: dict | None = None) -> dict:
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


def _ok_handler(payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


# ─────────────────────────────────────────────────────────────────────────────
# choices[0] shape variants — named refusal, never a crash, never a fake payload
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [
    {},                                                   # no choices at all
    {"choices": None},
    {"choices": "not-a-list"},
    {"choices": []},                                      # empty list
    {"choices": [None]},                                  # non-dict entry
    {"choices": ["a string"]},
    {"choices": [42]},
    {"choices": [{}]},                                    # empty dict: no message, no finish_reason
    {"choices": [{"message": "not-a-dict"}]},
    {"choices": [{"message": {}}]},                       # message without content
    {"choices": [{"message": {"role": "assistant"}}]},    # no content key at all
])
def test_choices_shape_variants_are_named_E_CLI_JSON_refusals(payload):
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON


def test_content_parts_list_is_refused_by_name_not_guessed():
    """The docs allow a content-parts LIST for REQUEST messages; an assistant completion types
    `content` as string|null. A proxy echoing the list back is off-contract: refuse by name
    rather than guess which part was the answer."""
    payload = _completion([{"type": "text", "text": "hello"}])
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON
    assert "not a string" in str(excinfo.value)


def test_reasoning_only_message_never_surfaces_hidden_reasoning_as_the_answer():
    for message in (
        {"role": "assistant", "reasoning_content": "hidden chain of thought"},
        {"role": "assistant", "content": None, "reasoning_content": "hidden chain of thought"},
    ):
        payload = _completion("")
        payload["choices"][0]["message"] = message
        with pytest.raises(PolsiaError) as excinfo:
            deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                    api_key=SAFE_TEST_KEY)
        assert excinfo.value.code == E_CLI_JSON  # no reasoning text may become `result`


# ─────────────────────────────────────────────────────────────────────────────
# finish_reason variants: only stop/tool_calls produce text; the rest are NAMED failures
# ─────────────────────────────────────────────────────────────────────────────
def test_finish_reason_stop_is_a_success_and_tool_calls_with_text_still_is():
    for finish in ("stop", "tool_calls"):
        result = deepseek_runtime.invoke(
            _make_inv(), client=_client(_ok_handler(_completion("answer", finish=finish))),
            api_key=SAFE_TEST_KEY)
        envelope = json.loads(result.stdout)
        assert envelope["is_error"] is False
        assert envelope["stop_reason"] == finish  # the variant is never hidden
        assert envelope["result"] == "answer"


def test_finish_reason_tool_calls_with_null_content_is_named_E_CLI_JSON():
    """This runtime never requests tools, so a bare tool-call reply is off-contract — named."""
    payload = _completion(None, finish="tool_calls")
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON


@pytest.mark.parametrize(("finish", "subtype", "retryable", "marker"), [
    ("content_filter", "error_content_filter", False, "content filter"),
    ("insufficient_system_resource", "error_overloaded", True, "insufficient inference resources"),
    ("aborted", "error_aborted", False, "interrupted"),
])
def test_incomplete_finish_reasons_are_named_failures_never_success(
    finish, subtype, retryable, marker
):
    """Every adjacent documented stop reason is a failure envelope with a stable subtype; only
    resource pressure is retryable (marker `overloaded`), via the REAL classifier."""
    result = deepseek_runtime.invoke(
        _make_inv(schema=SUMMARY_SCHEMA),
        client=_client(_ok_handler(_completion('{"summary": "par', finish=finish))),
        api_key=SAFE_TEST_KEY)

    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == subtype
    assert envelope["api_error_status"] is None
    assert "structured_output" not in envelope      # nothing fabricated from an unfinished body
    assert marker in envelope["result"]
    assert result.exit_code != 0
    assert SAFE_TEST_KEY not in result.stdout

    agent = _Agent()
    failure, detail, _ = agent._classify_invoke(result)
    assert failure is structured_output.FailureCode.ENVELOPE_IS_ERROR
    assert subtype in detail
    assert agent._retryable(result) is retryable
    with pytest.raises(PolsiaError) as excinfo:
        agent._finish_call(result, 1, transport="http")
    assert subtype in str(excinfo.value)


def test_content_filter_with_null_content_is_the_filter_failure_not_E_CLI_JSON():
    """Filtered output often arrives with content=None; that is still the filter failure."""
    payload = _completion(None, finish="content_filter")
    result = deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                     api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_content_filter"


def test_length_truncation_stays_error_max_tokens_and_not_retryable():
    """The existing contract survives the table refactor, verbatim in shape."""
    result = deepseek_runtime.invoke(
        _make_inv(), client=_client(_ok_handler(_completion('{"summary": "par', finish="length"))),
        api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["subtype"] == "error_max_tokens"
    assert "truncated" in envelope["result"]
    assert _Agent()._retryable(result) is False


# ─────────────────────────────────────────────────────────────────────────────
# usage: missing / partial / wrong-typed — unreachable crash paths become "unmeasured"
# ─────────────────────────────────────────────────────────────────────────────
def test_usage_missing_entirely_stays_unmeasured_and_does_not_crash_the_consumer():
    payload = _completion("ok")
    payload.pop("usage")
    result = deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                     api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["usage"] == {
        "input_tokens": None, "output_tokens": None, "cache_read_input_tokens": None,
    }
    agent = _Agent()
    raw = agent._finish_call(result, 1, transport="http")  # the REAL consumer path
    assert raw.text == "ok"
    assert agent.last_usage is not None
    assert agent.last_usage.tokens_used is None  # absent != zero, no one-sided sum


def test_wrong_typed_usage_values_do_not_crash_the_consumer():
    """`int("n/a")` used to raise inside `accounting._int_or_none` on a SUCCESSFUL run."""
    payload = _completion("ok", usage={
        "prompt_tokens": "n/a",
        "completion_tokens": {"unexpected": 1},
        "prompt_cache_hit_tokens": True,
    })
    result = deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                     api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["usage"] == {
        "input_tokens": None, "output_tokens": None, "cache_read_input_tokens": None,
    }
    agent = _Agent()
    raw = agent._finish_call(result, 1, transport="http")  # must NOT raise
    assert raw.text == "ok"
    assert agent.last_usage is not None and agent.last_usage.input_tokens is None


def test_usage_of_a_non_dict_shape_and_a_measured_zero():
    payload = _completion("ok", usage=[1, 2, 3])
    result = deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                     api_key=SAFE_TEST_KEY)
    assert json.loads(result.stdout)["usage"]["input_tokens"] is None

    payload = _completion("ok", usage={"prompt_tokens": 0, "completion_tokens": 0})
    result = deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(payload)),
                                     api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["usage"]["input_tokens"] == 0  # a measured zero stays a zero
    assert envelope["usage"]["output_tokens"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# extra keys + unicode + large bodies round-trip byte-exactly
# ─────────────────────────────────────────────────────────────────────────────
def test_unexpected_extra_keys_are_ignored_everywhere():
    payload = _completion('{"summary": "x"}', usage={
        "prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11,
        "prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 4,
        "completion_tokens_details": {"reasoning_tokens": 3},
    })
    payload["choices"][0]["logprobs"] = {"content": []}
    payload["choices"][0]["message"]["refusal"] = None
    payload["choices"][0]["message"]["annotations"] = []
    payload["system_fingerprint"] = "fp_abc"
    payload["created"] = 123
    result = deepseek_runtime.invoke(
        _make_inv(schema=SUMMARY_SCHEMA), client=_client(_ok_handler(payload)),
        api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is False
    assert envelope["structured_output"] == {"summary": "x"}


def test_unicode_emoji_content_round_trips():
    content = json.dumps({"summary": "fête 🎉 中文 — naïve café ☕"}, ensure_ascii=False)
    result = deepseek_runtime.invoke(
        _make_inv(schema=SUMMARY_SCHEMA), client=_client(_ok_handler(_completion(content))),
        api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["result"] == content
    assert envelope["structured_output"]["summary"] == "fête 🎉 中文 — naïve café ☕"
    assert "🎉" in result.stdout  # ensure_ascii=False: the envelope carries real unicode


def test_a_very_large_body_round_trips():
    big = "x" * 300_000
    result = deepseek_runtime.invoke(
        _make_inv(), client=_client(_ok_handler(_completion(big))), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["result"] == big
    assert len(envelope["result"]) == 300_000


# ─────────────────────────────────────────────────────────────────────────────
# transport / timeout mapping mirrors claude_cli.invoke
# ─────────────────────────────────────────────────────────────────────────────
def test_connection_reset_is_a_retryable_transport_failure_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset by peer")

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == "error_api_error"
    assert envelope["api_error_status"] is None
    assert "ReadError" in envelope["result"]
    assert _Agent()._retryable(result) is True


def test_connect_timeout_raises_E_CLI_TIMEOUT_like_the_cli_wall_clock():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_TIMEOUT


# ─────────────────────────────────────────────────────────────────────────────
# HTTP status taxonomy — table rows + the family fallback for everything else
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "retryable"), [
    (400, False), (401, False), (402, False), (403, False), (408, True), (422, False),
    (429, True), (500, True), (502, True), (503, True), (504, True),
])
def test_documented_status_rows_classify_through_the_real_ladder(status, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"status {status}"}})

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["api_error_status"] == status
    assert _Agent()._retryable(result) is retryable


@pytest.mark.parametrize(("status", "subtype", "retryable"), [
    (404, "error_invalid_request", False),   # wrong base URL/proxy: retrying cannot help
    (418, "error_invalid_request", False),
    (451, "error_invalid_request", False),
    (301, "error_invalid_request", False),   # follow_redirects is off by default
    (501, "error_server_error", True),       # unknown server-side failure: transient family
    (505, "error_server_error", True),
])
def test_unknown_http_status_never_borrows_the_transport_marker(status, subtype, retryable):
    """`error_api_error` is the TRANSPORT flag; an unmapped status must classify by family."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"status {status} page")

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    envelope = json.loads(result.stdout)
    assert envelope["is_error"] is True
    assert envelope["subtype"] == subtype
    assert envelope["subtype"] != "error_api_error"
    assert _Agent()._retryable(result) is retryable


# ─────────────────────────────────────────────────────────────────────────────
# the key must never ride a failure envelope (a hostile/broken endpoint echo)
# ─────────────────────────────────────────────────────────────────────────────
def test_key_echoed_by_a_hostile_error_body_is_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"gateway error, saw Authorization: Bearer {SAFE_TEST_KEY}")

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert SAFE_TEST_KEY not in result.stdout
    assert SAFE_TEST_KEY not in result.stderr
    assert "(key hidden)" in result.stdout


def test_key_echoed_in_a_non_json_200_body_is_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>key {SAFE_TEST_KEY} rejected</html>")

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_CLI_JSON
    assert SAFE_TEST_KEY not in str(excinfo.value)
    assert "(key hidden)" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# a misconfigured base URL is a NAMED refusal — never a raw httpx exception
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("base_url", [
    "api.deepseek.com",              # the docs host copied without a scheme
    "ftp://api.deepseek.com",        # non-http scheme
    "https://",                      # no host
    "not a url",
])
def test_misconfigured_base_url_is_a_named_E_VALIDATION_refusal(monkeypatch, base_url):
    _set_setting(monkeypatch, "deepseek_base_url", base_url)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)  # pragma: no cover - must never run
        return httpx.Response(200, json=_completion("ok"))

    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_VALIDATION
    assert "DEEPSEEK_BASE_URL" in excinfo.value.how_to_fix
    assert calls == []  # refused before anything could be sent


def test_invalid_port_is_a_named_E_VALIDATION_refusal_not_a_raw_httpx_InvalidURL(monkeypatch):
    """`https://host:9a9/...` passes the scheme/host check and only fails inside httpx —
    the post-site catch turns that into the same named refusal."""
    _set_setting(monkeypatch, "deepseek_base_url", "https://api.deepseek.com:9a9")
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(_ok_handler(_completion("ok"))),
                                api_key=SAFE_TEST_KEY)
    assert excinfo.value.code == E_VALIDATION
    assert "could not be sent" in str(excinfo.value)


def test_documented_base_urls_still_pass_validation(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_completion("ok"))

    deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert captured["url"] == "https://api.deepseek.com/chat/completions"

    _set_setting(monkeypatch, "deepseek_base_url", "http://127.0.0.1:8080/v1/")
    deepseek_runtime.invoke(_make_inv(), client=_client(handler), api_key=SAFE_TEST_KEY)
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"


# ─────────────────────────────────────────────────────────────────────────────
# structured_output consumption of the new/edge envelopes
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_output_never_treats_is_error_plus_subtype_success_as_a_success():
    stdout = json.dumps({
        "type": "result", "is_error": True, "subtype": "success",
        "result": '{"summary": "looks fine"}',
    })
    resolution = structured_output.resolve_output(stdout=stdout, exit_code=0)
    assert resolution.ok is False
    assert resolution.code is structured_output.FailureCode.ENVELOPE_IS_ERROR


def test_resolve_output_classifies_new_subtypes_as_named_failures():
    for subtype in ("error_content_filter", "error_overloaded", "error_aborted"):
        stdout = json.dumps({
            "type": "result", "is_error": True, "subtype": subtype,
            "result": "detail", "api_error_status": None,
        })
        resolution = structured_output.resolve_output(stdout=stdout, exit_code=0)
        assert resolution.ok is False
        assert resolution.code is structured_output.FailureCode.ENVELOPE_IS_ERROR
        assert subtype in resolution.detail


def test_resolve_output_rejects_a_non_object_structured_output():
    stdout = json.dumps({
        "type": "result", "is_error": False, "subtype": "success",
        "result": "see payload", "structured_output": ["not", "an", "object"],
    })
    resolution = structured_output.resolve_output(stdout=stdout, exit_code=0)
    assert resolution.ok is False
    assert resolution.code is structured_output.FailureCode.SCHEMA_VALIDATION_FAILED
