"""F-05 (visuals, P1): a run's tool evidence must be the UNION over its dispatches.

Live evidence (`visuals/raw`, run 299): the worker log shows a first execution that really called
`web_search`/`web_fetch` (3 provider calls), then a later, tool-less execution (1 provider call)
inside the SAME run; the ledger recorded `tools_used: []`, `num_turns: 1`, and `/runs/299` rendered
"No tools were called in this run." — a silent falsehood in the only audit trail.

Cause (this lane's file): `usage_metadata()` read ONLY the last dispatch's envelope, so a retried
attempt or a repair re-prompt overwrote the evidence of the dispatch that did the work.  The run's
evidence is now merged per dispatch (`_absorb_dispatch_evidence`) and emitted as one run record;
`num_turns` is the sum of the provider calls the run really made.

RED leg (forge receipt): with the merge removed, test 1 fails by name
(`assert [] == ['web_search', 'web_fetch']`).
"""
from __future__ import annotations

import json

from app.agents import claude_cli, deepseek_runtime
from app.agents.base_agent import BasePolsiaAgent
from app.config import settings
from app.schemas.agent_outputs import CompetitorReport

VALID_PAYLOAD = {
    "summary": "candle market research",
    "status": "ok",
    "competitors": [],
    "findings": [],
    "search_queries": [],
}


class _ProbeAgent(BasePolsiaAgent):
    """A real BasePolsiaAgent whose registry tools (competitor_research) are web_search/web_fetch."""

    agent_type = "competitor_research"
    output_schema = CompetitorReport
    role_prompt = "probe"

    def run(self, task, context):  # noqa: ANN001 - the frozen abstract seam
        return {}


def _envelope(
    *,
    tools: list[str] | None,
    turns: int,
    error: bool = False,
    structured: dict | None = None,
    text: str = "",
) -> claude_cli.InvokeResult:
    env: dict = {
        "type": "result",
        "subtype": "error_rate_limit" if error else "success",
        "is_error": error,
        "result": text or ("rate limited" if error else json.dumps(structured or VALID_PAYLOAD)),
        "session_id": "s-f05",
        "num_turns": turns,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": "deepseek-flash",
        "provider": "deepseek",
    }
    if tools is not None:
        env["tools_used"] = list(tools)
    if structured is not None:
        env["structured_output"] = structured
    return claude_cli.InvokeResult(
        stdout=json.dumps(env), stderr="", exit_code=0, wall_seconds=0.01, transport="http"
    )


def _collect(agent: BasePolsiaAgent) -> dict:
    payload, assembled = agent.collect_structured({"title": "Research the US candle market"}, {})
    return agent.attach_run_meta(payload, assembled=assembled)


def _install(monkeypatch, envelopes: list[claude_cli.InvokeResult]) -> list[dict]:
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    monkeypatch.setattr(settings, "agent_retry_backoff_seconds", 0.0)
    # The autouse CLAUDE_CLI_MOCK fixture must not short-circuit the dispatch under test.
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: False)
    calls: list[dict] = []

    def fake_invoke(invocation, **kwargs):  # noqa: ANN001 - mirrors deepseek_runtime.invoke
        calls.append(kwargs)
        return envelopes[min(len(calls) - 1, len(envelopes) - 1)]

    monkeypatch.setattr(deepseek_runtime, "invoke", fake_invoke)
    return calls


def test_tool_evidence_survives_a_retried_dispatch(monkeypatch):
    calls = _install(
        monkeypatch,
        [
            # The dispatch that DID the work, then hit a transient provider error: retried.
            _envelope(tools=["web_search", "web_fetch"], turns=3, error=True),
            # The dispatch that answered — tool-less, and therefore the only envelope the old
            # code ever looked at.
            _envelope(tools=[], turns=1, structured=VALID_PAYLOAD),
        ],
    )

    result = _collect(_ProbeAgent())

    assert len(calls) == 2, "the first dispatch must be retried for this test to mean anything"
    assert result["tools_used"] == ["web_search", "web_fetch"], (
        "the run's tool record lost the dispatch that actually called the tools"
    )
    assert result["num_turns"] == 4, (
        "the run made 3 + 1 provider calls; its record must say so, not 1"
    )
    # The persisted shape keeps the names (already bounded by accounting.bounded_tools_used).
    assert isinstance(result["tools_used"], list)


def test_a_tool_offered_run_with_no_calls_keeps_the_measured_zero(monkeypatch):
    _install(
        monkeypatch,
        [
            _envelope(tools=[], turns=2, error=True),
            _envelope(tools=[], turns=1, structured=VALID_PAYLOAD),
        ],
    )

    result = _collect(_ProbeAgent())

    assert result["tools_used"] == [], "`[]` is a measured zero (tools were offered, none ran)"
    assert result["num_turns"] == 3


def test_a_run_with_no_tools_keeps_the_key_absent(monkeypatch):
    _install(monkeypatch, [_envelope(tools=None, turns=1, structured=VALID_PAYLOAD)])

    result = _collect(_ProbeAgent())

    assert "tools_used" not in result, (
        "an envelope with no tools_used key means NOT RECORDED — never a fabricated []"
    )


def test_run_evidence_is_reset_between_runs_in_one_process(monkeypatch):
    """A worker process runs many tasks: run #2 must not inherit run #1's tool names."""
    agent = _ProbeAgent()
    _install(monkeypatch, [_envelope(tools=["web_search"], turns=1, structured=VALID_PAYLOAD)])
    first = _collect(agent)
    assert first["tools_used"] == ["web_search"]

    # Second logical run on the SAME agent instance, tool-less this time.
    _install(monkeypatch, [_envelope(tools=[], turns=1, structured=VALID_PAYLOAD)])
    second = _collect(agent)
    assert second["tools_used"] == [], (
        "the previous run's tool names leaked into this run's audit record"
    )
