"""SV-W4-06 / SV-W4-07 END-TO-END (wave 5) — the documented alias and the output bound must reach
the SERIALIZED DeepSeek request body, not just a resolver.

Wave 4 closed the two findings at the resolver level (`_reasoning_effort()` maps `ultra -> max`;
`deepseek_max_tokens` is a Settings field).  That is not the same claim as "the value the operator
configures is the value on the wire": `_build_body` could ignore either resolver, the body could be
serialized from a different dict, or the request could be built before the settings are read.  These
tests capture the ACTUAL bytes the runtime hands to httpx (`httpx.MockTransport` receives the real
`httpx.Request`; `json.loads(request.content)` is the payload the provider would parse) — no key and
no network are needed.

Documented sources (fetched 2026-10-28/29, both official):
  * `reasoning_effort` mapping table — minimal->low, low->low, medium->high, high->high, xhigh->high,
    max->max, ultra->max (api-docs.deepseek.com/guides/thinking_mode);
  * `max_tokens` — "The value must be between 1 and 384K (393216). When not set, the default is 8K
    in non-thinking mode, 64K in thinking mode (128K with `reasoning_effort` set to `max`)."
    (api-docs.deepseek.com/api/create-chat-completion).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime
from app.config import Settings
from app.core.errors import E_VALIDATION, MeridianError

SAFE_TEST_KEY = "sk-test-not-a-real-key"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def _invocation():
    return claude_cli.build_invocation(
        prompt="summarise the day",
        json_schema=SUMMARY_SCHEMA,
        system_prompt_static="you are a test",
        resume_session=None,
        timeout_s=30,
        schema_name="test",
    )


def _completion() -> dict:
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": '{"summary": "ok"}'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def _capture():
    """`(bodies, client)` — every SERIALIZED request body the runtime sends, in order."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_completion())

    return bodies, httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _clean_session_store():
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


def test_the_env_configured_ultra_alias_and_bound_reach_the_serialized_body(monkeypatch) -> None:
    """The whole chain: env -> Settings -> runtime -> the bytes httpx would have sent."""
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "ultra")
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "16384")
    built = Settings(_env_file=None)
    assert built.deepseek_reasoning_effort == "ultra"
    assert built.deepseek_max_tokens == 16384
    monkeypatch.setattr(deepseek_runtime, "settings", built)

    bodies, client = _capture()
    deepseek_runtime.invoke(_invocation(), client=client, api_key=SAFE_TEST_KEY)

    assert len(bodies) == 1, bodies
    body = bodies[0]
    assert body["thinking"] == {"type": "enabled"}, body
    assert body["reasoning_effort"] == "max", (
        "the documented ultra alias must be canonicalized BEFORE serialization"
    )
    assert body["max_tokens"] == 16384, "the configured output bound must be on the wire"
    assert body["response_format"] == {"type": "json_object"}, body


def test_the_shipped_defaults_are_what_a_call_really_sends(monkeypatch) -> None:
    """No configuration: effort `none` disables thinking and the bound is the shipped 8192."""
    monkeypatch.delenv("DEEPSEEK_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("DEEPSEEK_MAX_TOKENS", raising=False)
    monkeypatch.setattr(deepseek_runtime, "settings", Settings(_env_file=None))

    bodies, client = _capture()
    deepseek_runtime.invoke(_invocation(), client=client, api_key=SAFE_TEST_KEY)

    body = bodies[0]
    assert body["thinking"] == {"type": "disabled"}, body
    assert "reasoning_effort" not in body, "effort none must not send a reasoning_effort field"
    assert body["max_tokens"] == deepseek_runtime.DEEPSEEK_DEFAULT_MAX_TOKENS == 8192


@pytest.mark.parametrize("bound", [1, 65536, 393216])
def test_the_documented_range_is_configurable_end_to_end(monkeypatch, bound: int) -> None:
    """Both ends of the vendor's documented 1..393216 range must reach the body untruncated."""
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", str(bound))
    monkeypatch.setattr(deepseek_runtime, "settings", Settings(_env_file=None))

    bodies, client = _capture()
    deepseek_runtime.invoke(_invocation(), client=client, api_key=SAFE_TEST_KEY)

    assert bodies[0]["max_tokens"] == bound


def test_a_non_positive_bound_falls_back_and_is_still_sent(monkeypatch) -> None:
    monkeypatch.setattr(deepseek_runtime.settings, "deepseek_max_tokens", 0)

    bodies, client = _capture()
    deepseek_runtime.invoke(_invocation(), client=client, api_key=SAFE_TEST_KEY)

    assert bodies[0]["max_tokens"] == deepseek_runtime.DEEPSEEK_DEFAULT_MAX_TOKENS


def test_an_unknown_effort_is_refused_before_any_request_is_built(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "turbo")
    monkeypatch.setattr(deepseek_runtime, "settings", Settings(_env_file=None))
    bodies, client = _capture()

    with pytest.raises(MeridianError) as excinfo:
        deepseek_runtime.invoke(_invocation(), client=client, api_key=SAFE_TEST_KEY)

    assert excinfo.value.code == E_VALIDATION
    assert bodies == [], "a refused effort must not spend a request"
    assert "ultra" in str(excinfo.value), "the refusal names every documented alias"


def test_the_alias_table_is_the_documented_one() -> None:
    """All seven documented rows, not just the ones a wave remembered."""
    documented = {
        "minimal": "low", "low": "low", "medium": "high", "high": "high",
        "xhigh": "high", "max": "max", "ultra": "max",
    }
    aliases = deepseek_runtime.REASONING_EFFORT_ALIASES
    canonical = set(deepseek_runtime.REASONING_EFFORT_CANONICAL)
    for requested, expected in documented.items():
        if requested in canonical:
            assert deepseek_runtime.REASONING_EFFORT_ALIASES.get(requested, requested) == expected
        else:
            assert aliases.get(requested) == expected, (
                f"{requested!r} must map to {expected!r} per the vendor's table"
            )
