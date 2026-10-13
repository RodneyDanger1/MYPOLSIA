"""R20 item 1 — accounting's non-numeric usage guard, through the REAL consumer path.

Symptom this pins: any envelope producer except DeepSeek could crash a SUCCESSFUL run mid-flight
— a non-numeric value in any measured field reached `accounting._int_or_none` / `_float_or_none`
inside `base_agent._finish_call`, and `int("n/a")` raised ValueError/TypeError, so the model's
answer was lost (RD13 fixed only the DeepSeek producer; R20 item 1 makes the consumer boundary
total).

Contract pinned here (the envelope consumers' rule: None means "unmeasured", a measured 0 stays 0):
  * a value whose TYPE cannot be a measurement collapses to None — never a raise, never a coerced
    number (`"5"` is not 5; `True` is not 1 — a bool is a flag) and never a truncation (12.5 is
    not 12);
  * finite integral floats (`18.0`, a number that round-tripped through a JSON float) survive as
    the integer they are; NaN/inf are unmeasured;
  * measured values and measured zeros pass through untouched.

Every test drives `BasePolsiaAgent._finish_call` on a claude-CLI-shaped `InvokeResult` — the same
real-consumer pattern as `test_rd13_deepseek_edges.py`'s D3 tests (the mock CLI and the raw CLI
envelope are the producers this guard exists for).
"""
from __future__ import annotations

import json

import pytest

from app.agents import claude_cli
from app.agents.base_agent import BasePolsiaAgent
from app.core.errors import E_CLI_EXIT, PolsiaError


class _Agent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "test"}


def _invoke_result(env: dict, exit_code: int = 0) -> claude_cli.InvokeResult:
    return claude_cli.InvokeResult(
        stdout=json.dumps(env), stderr="", exit_code=exit_code, wall_seconds=1.0,
        transport="argv",
    )


def _success_env(**overrides) -> dict:
    env = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "session_id": "s1",
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 0,
        },
        "total_cost_usd": 0.05,
        "duration_ms": 1200,
        "num_turns": 1,
    }
    env.update(overrides)
    return env


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
TOP_FIELDS = ("total_cost_usd", "duration_ms", "num_turns")
#: field name -> RunUsage attribute (two usage keys are stored under shorter names)
ATTR = {
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}
#: Every producer-side junk shape RD13 named, plus the adjacent ones a proxy can emit.
JUNK_VALUES = ["n/a", "", "5", {"unexpected": 1}, [1, 2], True, False]


def _env_with(field: str, value) -> dict:
    env = _success_env()
    if field in env["usage"]:
        env["usage"][field] = value
    else:
        env[field] = value
    return env


@pytest.mark.parametrize("junk", JUNK_VALUES)
@pytest.mark.parametrize("field", (*USAGE_FIELDS, *TOP_FIELDS))
def test_junk_in_any_measured_field_never_crashes_the_run(field, junk):
    """The RED leg: pre-fix, `"n/a"`/`{}`/`[1,2]` raised inside `_finish_call` (probe-first)."""
    agent = _Agent()

    raw = agent._finish_call(_invoke_result(_env_with(field, junk)), 1, transport="argv")

    assert raw.text == "ok"  # the successful run survived, result intact
    usage = agent.last_usage
    assert usage is not None
    attr = ATTR.get(field, field)
    assert getattr(usage, attr) is None  # unmeasured — not coerced, not truncated


def test_all_junk_at_once_leaves_every_measurement_unmeasured():
    env = _success_env(
        usage={
            "input_tokens": "n/a",
            "output_tokens": {"unexpected": 1},
            "cache_read_input_tokens": [1, 2],
            "cache_creation_input_tokens": True,
        },
        total_cost_usd="cheap",
        duration_ms="1200ms",
        num_turns={"n": 1},
    )
    agent = _Agent()

    raw = agent._finish_call(_invoke_result(env), 1, transport="argv")

    assert raw.text == "ok"
    usage = agent.last_usage
    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_creation_tokens is None
    assert usage.total_cost_usd is None
    assert usage.duration_ms is None
    assert usage.num_turns is None
    assert usage.tokens_used is None                    # one unknown side -> no sum
    assert usage.as_dict()["cost_estimate_usd"] is None  # never `round(None)`, never 0.0


def test_measured_values_and_measured_zeros_pass_through_untouched():
    agent = _Agent()
    agent._finish_call(_invoke_result(_success_env()), 1, transport="argv")
    usage = agent.last_usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (3, 0)
    assert usage.tokens_used == 18
    assert usage.total_cost_usd == 0.05
    assert usage.duration_ms == 1200
    assert usage.num_turns == 1

    zero_env = _success_env(
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        total_cost_usd=0.0,
        duration_ms=0,
        num_turns=0,
    )
    agent = _Agent()
    agent._finish_call(_invoke_result(zero_env), 1, transport="argv")
    usage = agent.last_usage
    assert usage is not None
    assert usage.tokens_used == 0            # a measured zero stays a zero
    assert usage.total_cost_usd == 0.0
    assert usage.duration_ms == 0
    assert usage.num_turns == 0
    assert usage.as_dict()["cost_estimate_usd"] == 0.0


def test_integral_floats_survive_as_the_integer_they_are():
    """A JSON float round-trip (`18.0`) is the count 18 — rejecting it would lose real data."""
    env = _success_env(duration_ms=1200.0)
    env["usage"]["input_tokens"] = 18.0
    agent = _Agent()

    agent._finish_call(_invoke_result(env), 1, transport="argv")

    usage = agent.last_usage
    assert usage is not None
    assert usage.input_tokens == 18 and isinstance(usage.input_tokens, int)
    assert usage.duration_ms == 1200 and isinstance(usage.duration_ms, int)


def test_non_integral_floats_are_unmeasured_never_truncated():
    """12.5 tokens does not exist; `int(12.5)` would invent the count 12 (R20 decision)."""
    env = _success_env(duration_ms=12.5, num_turns=2.5)
    agent = _Agent()

    raw = agent._finish_call(_invoke_result(env), 1, transport="argv")

    assert raw.text == "ok"
    usage = agent.last_usage
    assert usage is not None
    assert usage.duration_ms is None
    assert usage.num_turns is None


def test_non_finite_floats_are_unmeasured_not_written():
    """NaN/inf would ride `round(x, 6)` into `cost_estimate_usd` and break the DB write."""
    env = _success_env(total_cost_usd=float("inf"), duration_ms=float("nan"))
    agent = _Agent()

    raw = agent._finish_call(_invoke_result(env), 1, transport="argv")

    assert raw.text == "ok"
    usage = agent.last_usage
    assert usage is not None
    assert usage.total_cost_usd is None
    assert usage.duration_ms is None
    assert usage.as_dict()["cost_estimate_usd"] is None

    env = _success_env(total_cost_usd=float("-inf"))
    agent = _Agent()
    agent._finish_call(_invoke_result(env), 1, transport="argv")
    usage = agent.last_usage
    assert usage is not None
    assert usage.total_cost_usd is None


def test_junk_usage_in_a_failure_envelope_still_raises_the_named_failure():
    """The guard must not convert a NAMED failure into a raw ValueError on the way out."""
    env = _success_env(subtype="error_max_tokens", is_error=True, result="truncated")
    env["usage"]["input_tokens"] = "n/a"

    with pytest.raises(PolsiaError) as excinfo:
        _Agent()._finish_call(_invoke_result(env, exit_code=1), 1, transport="argv")

    assert excinfo.value.code == E_CLI_EXIT
    assert "error_max_tokens" in str(excinfo.value)
