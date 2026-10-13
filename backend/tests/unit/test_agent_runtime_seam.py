"""RD8 seam — `base_agent._real_call` selects the provider by key availability.

The seam contract: with a DeepSeek key (stored or DEEPSEEK_API_KEY) the single dispatch site
calls the DeepSeek runtime; without one the Claude CLI path runs UNCHANGED. Both sides are
patched here (no network, no subprocess), so these tests pin routing, transport bookkeeping and
the once-per-logical-run choice — including that a raised E_CLI_TIMEOUT is retried by the SAME
`claude_cli.run_with_retries` ladder the CLI path uses.
"""
from __future__ import annotations

import json

import pytest

from app.agents import claude_cli, deepseek_runtime
from app.agents.base_agent import BasePolsiaAgent
from app.config import settings
from app.core.errors import E_CLI_EXIT, E_CLI_TIMEOUT, PolsiaError


class _Agent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


def _envelope(**overrides) -> str:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"summary": "ok"}',
        "usage": {},
        "num_turns": 1,
    }
    envelope.update(overrides)
    return json.dumps(envelope)


def _ok_result(*, transport: str = "http") -> claude_cli.InvokeResult:
    return claude_cli.InvokeResult(
        stdout=_envelope(), stderr="", exit_code=0, wall_seconds=0.01, transport=transport
    )


def _rate_limited_result() -> claude_cli.InvokeResult:
    return claude_cli.InvokeResult(
        stdout=_envelope(subtype="error_rate_limit", is_error=True, api_error_status=429,
                         result="rate limited"),
        stderr="rate limited",
        exit_code=1,
        wall_seconds=0.01,
        transport="http",
    )


def _instant_backoff(monkeypatch) -> None:
    """run_with_retries sleeps via backoff_delay(); make retries instant in tests."""
    monkeypatch.setattr(claude_cli, "backoff_delay", lambda attempt, **_kwargs: 0.0)


def test_key_present_dispatches_to_the_deepseek_runtime(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "key-present")
    seen: dict = {}

    def fake_deepseek(inv):
        seen["inv"] = inv
        return _ok_result()

    def forbidden_claude(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("claude_cli.invoke must not run when a DeepSeek key exists")

    monkeypatch.setattr(deepseek_runtime, "invoke", fake_deepseek)
    monkeypatch.setattr(claude_cli, "invoke", forbidden_claude)

    agent = _Agent()
    raw = agent._real_call("hello deepseek")

    assert "inv" in seen
    assert raw.text == '{"summary": "ok"}'
    assert raw.transport == "http"          # the run is labelled for what it really was
    assert raw.exit_code == 0
    assert raw.attempts == 1


def test_key_absent_dispatches_to_the_claude_cli_path(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: None)

    def forbidden_deepseek(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("deepseek_runtime.invoke must not run without a key")

    seen: dict = {}

    def fake_claude(inv, *, executable=None):
        seen["inv"] = inv
        seen["executable"] = executable
        return _ok_result(transport=inv.transport)

    monkeypatch.setattr(deepseek_runtime, "invoke", forbidden_deepseek)
    monkeypatch.setattr(claude_cli, "invoke", fake_claude)

    agent = _Agent()
    raw = agent._real_call("hello claude")

    assert seen["executable"] == agent.cli_path   # the CLI path is invoked EXACTLY as before
    assert raw.transport in ("argv", "stdin")
    assert raw.text == '{"summary": "ok"}'


def test_provider_is_chosen_once_per_logical_run(monkeypatch):
    """Retries of one run must never switch providers mid-flight."""
    checks = {"n": 0}

    def resolver():
        checks["n"] += 1
        return "key-present"

    dispatches = {"n": 0}

    def always_rate_limited(inv):
        dispatches["n"] += 1
        return _rate_limited_result()

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", resolver)
    monkeypatch.setattr(deepseek_runtime, "invoke", always_rate_limited)
    _instant_backoff(monkeypatch)

    agent = _Agent()
    with pytest.raises(PolsiaError) as excinfo:
        agent._real_call("retry me")

    assert excinfo.value.code == E_CLI_EXIT          # the 429 envelope's named E-code
    assert dispatches["n"] == 1 + settings.agent_max_retries
    assert checks["n"] == 1                          # key consulted once, not per attempt


def test_deepseek_timeout_refusal_is_retried_by_the_shared_ladder(monkeypatch):
    """A raised E_CLI_TIMEOUT from the DeepSeek runtime retries exactly like the CLI timeout."""
    calls = {"n": 0}

    def flaky(inv):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PolsiaError(E_CLI_TIMEOUT, "deepseek API exceeded 7s",
                              how_to_fix="raise deepseek_timeout_seconds in Settings")
        return _ok_result()

    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: "key-present")
    monkeypatch.setattr(deepseek_runtime, "invoke", flaky)
    _instant_backoff(monkeypatch)

    agent = _Agent()
    raw = agent._real_call("hello again")

    assert calls["n"] == 2
    assert agent.last_attempts == 2
    assert raw.transport == "http"
    assert raw.text == '{"summary": "ok"}'
