"""Test BasePolsiaAgent.call_claude() — subprocess mock.

Extended for wave W3-refusals (T9/T10): the accounting metadata must distinguish "unmeasured"
(absent key -> NULL column) from "measured zero", and the two sites that used to fall back
silently (S-01 retry classification, S-02 schema emission) must record a slug-coded warning.
"""
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

import app.agents.base_agent as base_agent_module
from app.agents import accounting, claude_cli, structured_output
from app.agents.base_agent import BasePolsiaAgent


class ConcreteAgent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):
        return {"summary": "test"}


def test_call_claude_returns_mock_when_env_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": "hello from mock"}))

    agent = ConcreteAgent()
    result = agent.call_claude("test prompt")

    assert result == "hello from mock"


def test_call_claude_does_not_invoke_subprocess_in_mock_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = ConcreteAgent()

    with patch("subprocess.run") as mock_run:
        agent.call_claude("test prompt")
        mock_run.assert_not_called()


def test_call_claude_json_parses_result(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": '{"key": "value", "number": 42}'}),
    )

    agent = ConcreteAgent()
    result = agent.call_claude_json("test")
    assert result == {"key": "value", "number": 42}


def test_timed_run_adds_duration():
    agent = ConcreteAgent()
    result = agent.timed_run({"title": "test"}, {})
    assert "duration_secs" in result
    assert isinstance(result["duration_secs"], float)


# ─────────────────────────────────────────────────────────────────────────────
# T9 — usage_metadata keeps "unmeasured" (absent key) apart from "measured zero"
# ─────────────────────────────────────────────────────────────────────────────
def _usage_row() -> SimpleNamespace:
    """A stand-in for the `AgentRun` row: an untouched accounting column reads as NULL."""
    return SimpleNamespace(
        tokens_input=None,
        tokens_output=None,
        tokens_used=None,
        cli_session_id=None,
        cost_usd=None,
        usage_json=None,
    )


def test_usage_metadata_without_usage_omits_every_measurement_key():
    """No accounting envelope -> the keys are ABSENT, so the columns stay NULL (RED leg 2)."""
    agent = ConcreteAgent()

    meta = agent.usage_metadata()

    for key in (
        "tokens_input",
        "tokens_output",
        "tokens_used",
        "cost_usd",
        "cost_estimate_usd",
        "usage_json",
        "num_turns",
        "model",
    ):
        assert key not in meta, f"{key} must be omitted, not defaulted to zero"
    # The non-measurement keys stay (they describe the run, not a quantity nobody measured).
    assert meta["cli_session_id"] is None
    assert meta["cli_attempts"] == 0
    assert meta["repaired_by"] == ""
    assert meta["context_tokens"] == 0
    assert meta["stable_prefix_hash"] == ""
    assert meta["is_error"] is False  # payload state: no error envelope was seen


def test_usage_metadata_without_total_cost_omits_cost_usd():
    """A usage record with counters but no cost must not write `cost_usd` (F-11/R9)."""
    agent = ConcreteAgent()
    agent.last_usage = accounting.RunUsage(
        session_id="s1", input_tokens=0, output_tokens=0, num_turns=0
    )

    meta = agent.usage_metadata()

    assert "cost_usd" not in meta
    assert meta["tokens_used"] == 0        # a measured zero IS carried
    assert meta["num_turns"] == 0
    assert meta["cli_session_id"] == "s1"

    from celery_app.tasks.agent_tasks import _apply_run_usage

    row = _usage_row()
    _apply_run_usage(row, meta)
    assert row.cost_usd is None


def test_usage_metadata_with_measured_cost_writes_the_delta():
    agent = ConcreteAgent()
    agent.last_usage = accounting.RunUsage(
        session_id="s1", input_tokens=10, output_tokens=5, total_cost_usd=0.25, num_turns=2
    )

    meta = agent.usage_metadata()

    assert meta["cost_usd"] == 0.25
    assert meta["cost_estimate_usd"] == 0.25
    assert meta["tokens_used"] == 15
    assert meta["cli_session_id"] == "s1"


# ─────────────────────────────────────────────────────────────────────────────
# T10 S-01 — the retry classifier records an unparseable envelope
# ─────────────────────────────────────────────────────────────────────────────
def test_retryable_records_a_missing_envelope(caplog):
    """RED leg 4: with the recording line removed, the caplog assertion is what fails."""
    agent = ConcreteAgent()
    result = claude_cli.InvokeResult(
        stdout="not json at all", stderr="hook noise", exit_code=1, wall_seconds=0.1
    )

    with caplog.at_level(logging.WARNING, logger="app.agents.base_agent"):
        retryable = agent._retryable(result)

    # Unchanged classification: an empty envelope, exactly what the exit-code logic implies.
    expected = claude_cli.is_retryable_failure(
        structured_output.e_code_for(structured_output.FailureCode.CLI_NONZERO_EXIT),
        subtype="",
        api_status=None,
        exit_code=1,
    )
    assert retryable is expected
    assert len([r for r in caplog.records if "code=envelope_extract_failed" in r.getMessage()]) == 1


def test_retryable_uses_the_parsed_envelope_when_it_is_json(caplog):
    """The recording only fires on the unparseable path — a good envelope is still used."""
    agent = ConcreteAgent()
    envelope = json.dumps(
        {"is_error": True, "subtype": "error_during_execution", "api_error_status": 429}
    )
    result = claude_cli.InvokeResult(stdout=envelope, stderr="", exit_code=1, wall_seconds=0.1)

    with caplog.at_level(logging.WARNING, logger="app.agents.base_agent"):
        retryable = agent._retryable(result)

    assert retryable is True  # 429 is transient — read out of the envelope, not guessed
    assert not [r for r in caplog.records if "code=envelope_extract_failed" in r.getMessage()]


# ─────────────────────────────────────────────────────────────────────────────
# T10 S-02 — schema emission failure is recorded and the run continues
# ─────────────────────────────────────────────────────────────────────────────
class _TinySchema(BaseModel):
    summary: str = ""


def test_schema_emission_failure_is_recorded_and_the_run_continues(monkeypatch, caplog):
    """RED leg 5: without the recording line the caplog assertion is what fails."""
    import app.schemas.agent_outputs as agent_outputs

    def broken_emission(model):
        raise RuntimeError("emission broke")

    monkeypatch.setattr(agent_outputs, "to_cli_schema", broken_emission)
    monkeypatch.setattr(base_agent_module, "is_mock_mode", lambda: False)  # exercise the real path

    agent = ConcreteAgent()
    captured: dict = {}
    payload = {"summary": "ok from the real path"}

    def fake_real_call(
        prompt, *, json_schema=None, system_prompt_static="", resume=False, tool_less=False
    ):
        captured["json_schema"] = json_schema
        return base_agent_module._RawCall(
            text=json.dumps(payload),
            envelope={"result": json.dumps(payload)},
            exit_code=0,
            attempts=1,
        )

    monkeypatch.setattr(agent, "_real_call", fake_real_call)

    with caplog.at_level(logging.WARNING, logger="app.agents.base_agent"):
        out = agent.call_claude_structured("do the thing", schema=_TinySchema, schema_name="tiny")

    assert out == payload                                     # the run continued
    assert captured["json_schema"] is None                    # ... without --json-schema
    assert len([r for r in caplog.records if "code=schema_emission_failed" in r.getMessage()]) == 1

