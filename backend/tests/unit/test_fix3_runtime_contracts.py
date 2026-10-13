"""L3-06 / L3-08 / L3-09 / L3-11 / L3-13 — runtime contract truths.

What this file pins (findings lane-3 + research-C, phase-3 fixer-3):

* L3-06 — the orchestrator's mode comes from the CALLER's `metadata.cycle` first; the word sniff
  stays only as the ad-hoc fallback. A morning plan whose wording trips the sniff survives, the
  model's contradictory claim is recorded, and a discarded non-empty `tasks[]` is named in notes.
* L3-08 — `_tool_match` implements the documented Claude Code glob semantics (`mcp__*`,
  bare `mcp__<server>`, `mcp__<server>__prefix*`, the mandatory space in `Tool(specifier)`), and
  an unanchored ALLOW glob is skipped with a warning — never auto-approved.
* L3-09 — the CLI_AUTH_FAILED remediation names only routes the spawn path supports; the child
  environment excludes an API key (the reference-backed OAuth route).
* L3-11 — the coercion map is structural: `dict[str, float]` / `dict[str, str]` fields are NOT
  in it, `int | None` scalars still are; a string-valued container survives `coerce_types`.
* L3-13 — the E_CLI_TIMEOUT surfaces agree with the pinned ladder: a RAISED timeout is retried
  (exactly 1 + agent_max_retries attempts), the ENVELOPE exit-143 shape is not, and the docstring
  + hint say so.
"""
from __future__ import annotations

import json
import logging

import pytest
from pydantic import BaseModel

import app.config as app_config
from app.agents import claude_cli
from app.agents.base_agent import _top_level_coercions
from app.agents.orchestrator.agent import OrchestratorAgent
from app.agents.policy_firewall import (
    ActionClass,
    ActionRequest,
    Decision,
    _allow_rule_usable,
    _tool_match,
    decide,
)
from app.agents.structured_output import REMEDIATION, FailureCode, coerce_types
from app.core.errors import E_CLI_TIMEOUT, ERROR_HINTS, PolsiaError
from app.schemas.agent_outputs import AGENT_SCHEMAS

# ─────────────────────────────────────────────────────────────── L3-06 orchestrator mode


def test_the_declared_cycle_beats_a_tripped_sniff(monkeypatch):
    """The probe6-B case: 'Close the pricing loop' on the MORNING cycle keeps its plan."""
    task = {
        "title": "Close the pricing loop",
        "description": "plan the day, then report back",
        "metadata": {"cycle": "morning"},
    }
    assert OrchestratorAgent.plan_mode(task) == "morning"

    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({
        "summary": "plan written", "key_focus": "pricing",
        "tasks": [{"title": "Research pricing pages", "agent_type": "competitor_research"}],
    }))
    out = OrchestratorAgent().run(task, {})
    assert out["mode"] == "morning"
    assert out["planned_task_count"] == 1
    assert out["tasks"], "a morning plan must never be thrown away by a word in its title"


def test_the_declared_cycle_beats_a_morning_sounding_title():
    task = {"title": "Morning planning cycle", "metadata": {"cycle": "evening"}}
    assert OrchestratorAgent.plan_mode(task) == "evening"


def test_the_word_sniff_survives_only_as_the_fallback():
    assert OrchestratorAgent.plan_mode({"title": "Evening reporting cycle"}) == "evening"
    assert OrchestratorAgent.plan_mode({"title": "Morning planning cycle"}) == "morning"
    assert OrchestratorAgent.plan_mode(
        {"title": "Close the pricing loop", "description": "report back"}
    ) == "evening"  # no declaration -> the old heuristic still answers for ad-hoc tasks
    assert OrchestratorAgent.plan_mode({"metadata": "not-a-dict"}) == "morning"


def test_a_model_mode_contradiction_is_recorded_not_silently_overwritten(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({
        "summary": "evening-looking plan", "key_focus": "Focus", "mode": "evening",
        "tasks": [{"title": "ship it", "agent_type": "code_generation"}],
    }))
    out = OrchestratorAgent().run({"title": "Morning planning cycle",
                                   "metadata": {"cycle": "morning"}}, {})
    assert out["mode"] == "morning"
    assert any("model declared mode='evening'" in note for note in out["notes"])


def test_a_discarded_evening_plan_is_named_in_notes(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({
        "summary": "summary", "key_focus": "Focus", "insights": ["a"],
        "tasks": [{"title": "Leftover task", "agent_type": "social_media"}],
    }))
    out = OrchestratorAgent().run({"title": "Evening reporting cycle",
                                   "metadata": {"cycle": "evening"}}, {})
    assert out["tasks"] == []
    assert any("discarded 1 planned task" in note and "Leftover task" in note
               for note in out["notes"])


# ─────────────────────────────────────────────────────────────── L3-08 tool matching


@pytest.mark.parametrize("tool,pattern,expected", [
    ("mcp__x__y", "mcp__*", True),
    ("mcp__github__create_issue", "mcp__github", True),
    ("mcp__github", "mcp__github", True),
    ("mcp__x__y", "mcp__x__get_*", False),
    ("mcp__github__create_issue", "mcp__github__create*", True),
    ("Bash", "B*", True),                                        # deny-rule glob
    ("Bash(git diff HEAD)", "Bash(git diff *)", True),
    ("Bash(git diff-index)", "Bash(git diff *)", False),         # the space is load-bearing
    ("Bash(git diff-index)", "Bash(git diff*)", True),           # no space -> raw prefix
    ("Read", "Read", True),
    ("Edit", "Read", False),
])
def test_tool_match_implements_the_documented_rule_language(tool, pattern, expected):
    assert _tool_match(tool, pattern) is expected


@pytest.mark.parametrize("pattern,usable", [
    ("Read", True),
    ("Bash(git commit *)", True),
    ("mcp__github", True),
    ("mcp__github__create*", True),
    ("*", False),                 # unanchored allow globs never auto-approve
    ("B*", False),
    ("mcp__*", False),
    ("mcp__github__(x)", False),  # any mcp__ rule with parentheses is skipped
])
def test_allow_rules_are_only_usable_when_anchored(pattern, usable):
    assert _allow_rule_usable(pattern) is usable


def test_an_unanchored_allow_glob_is_skipped_with_a_warning(monkeypatch, caplog):
    from dataclasses import replace

    import app.agents.policy_firewall as pf

    monkeypatch.setitem(
        pf.AGENT_CAPABILITIES, "probe_agent",
        replace(pf.AGENT_CAPABILITIES["social_media"], allowed_tools=("*",), denied_tools=()),
    )
    with caplog.at_level(logging.WARNING, logger="app.agents.policy_firewall"):
        decision = decide(ActionRequest(agent_type="probe_agent", action_class=ActionClass.READ,
                                        channel="", tool="Bash"))
    assert decision.decision is Decision.DENY, "an unanchored allow glob must not auto-approve"
    assert any("unanchored_glob" in rec.message for rec in caplog.records)


def test_a_denied_tool_is_denied_by_name():
    decision = decide(ActionRequest(agent_type="code_generation", action_class=ActionClass.READ,
                                    channel="github", tool="mcp__github__create_issue"))
    assert decision.decision is Decision.DENY
    assert decision.code == "POLSIA-E114"


# ─────────────────────────────────────────────────────────────── L3-09 CLI auth hint


def test_the_cli_auth_remediation_names_supported_routes_only():
    hint = REMEDIATION[FailureCode.CLI_AUTH_FAILED]
    assert "claude login" in hint
    assert "CLAUDE_CONFIG_DIR" in hint
    assert "ANTHROPIC_API_KEY" not in hint, "a route the spawn path strips must not be offered"
    assert "NOT passed to the child process" in hint


def test_the_child_environment_excludes_an_api_key_even_when_set(monkeypatch):
    """The hint above is true because of THIS: the allowlist drives the child env build."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-probe-not-a-real-key")
    assert "ANTHROPIC_API_KEY" not in claude_cli.SAFE_ENV_PASSTHROUGH
    captured: dict = {}

    def spawn(argv, stdin_text, env, timeout):
        captured["env"] = dict(env)
        return '{"type":"result","subtype":"success","is_error":false,"result":"{}"}', 0

    inv = claude_cli.build_invocation(prompt="hello", max_turns=1)
    claude_cli.invoke(inv, spawn=spawn)
    assert "ANTHROPIC_API_KEY" not in captured["env"]


# ─────────────────────────────────────────────────────────────── L3-11 coercion map


class _ContainerProbe(BaseModel):
    summary: str = ""
    count: int | None = None
    ratio: float | None = None
    flag: bool = False
    mapping_float: dict[str, float] = {}
    mapping_str: dict[str, str] = {}
    items: list[str] = []
    mode: str = ""


def test_the_coercion_map_is_structural():
    coercions = _top_level_coercions(_ContainerProbe)
    assert coercions == {"count": int, "ratio": float, "flag": bool}


def test_no_container_field_of_any_agent_schema_enters_the_map():
    container_types = (dict, list, tuple, set)
    for name, model in AGENT_SCHEMAS.items():
        for field_name, coercion in _top_level_coercions(model).items():
            annotation = model.model_fields[field_name].annotation
            assert getattr(annotation, "__origin__", None) not in container_types, (
                f"{name}.{field_name} ({annotation!r}) must never be coerced to {coercion}"
            )


def test_a_string_valued_container_survives_coerce_types_unchanged():
    payload = {"summary": "s", "mapping_str": {"a": "42"}, "mapping_float": {"b": "1.5"}}
    out = coerce_types(payload, {"mapping_str": float, "mapping_float": float})
    assert out["mapping_str"] == {"a": "42"}
    assert out["mapping_float"] == {"b": "1.5"}


# ─────────────────────────────────────────────────────────────── L3-13 E_CLI_TIMEOUT


def test_a_raised_timeout_costs_exactly_one_plus_max_retries_attempts():
    calls: list[int] = []

    def dispatch():
        calls.append(1)
        raise PolsiaError(E_CLI_TIMEOUT, "claude CLI exceeded 7s")

    with pytest.raises(PolsiaError) as excinfo:
        claude_cli.run_with_retries(dispatch, attempts=2, sleep=lambda _s: None)

    assert excinfo.value.code == E_CLI_TIMEOUT
    assert len(calls) == 3, "raise + 2 retries — the number the docstring and hint claim"


def test_the_envelope_timeout_shape_is_still_classified_non_retryable():
    assert claude_cli.is_retryable_failure(E_CLI_TIMEOUT, subtype="", api_status=None,
                                           exit_code=143) is False


def test_the_docstring_and_the_hint_state_the_retry_fact():
    doc = claude_cli.is_retryable_failure.__doc__ or ""
    ladder_doc = claude_cli.run_with_retries.__doc__ or ""
    hint = ERROR_HINTS[E_CLI_TIMEOUT]
    assert "RETRIED" in ladder_doc and "agent_max_retries" in ladder_doc
    assert "retried" in doc and "agent_max_retries" in doc
    assert "exit 143" in doc
    assert "retried up to AGENT_MAX_RETRIES" in hint


def test_the_deepseek_timeout_refusal_carries_its_own_hint(monkeypatch):
    """The refusal this runtime raises on a hang points at the DeepSeek-side knob."""
    import httpx

    from app.agents import deepseek_runtime

    def raise_timeout(request):  # noqa: ANN001 - httpx MockTransport handler
        raise httpx.TimeoutException("probe timeout")

    client = httpx.Client(transport=httpx.MockTransport(raise_timeout))
    inv = claude_cli.build_invocation(prompt="timeout probe", max_turns=1)
    with pytest.raises(PolsiaError) as excinfo:
        deepseek_runtime.invoke(inv, client=client, api_key="sk-probe-key")
    assert excinfo.value.code == E_CLI_TIMEOUT
    assert "deepseek_timeout_seconds" in excinfo.value.how_to_fix


def test_settings_pin_the_retry_count_the_docstring_names():
    """The docstring's '(1 + agent_max_retries)' claim reads the real setting."""
    assert isinstance(app_config.settings.agent_max_retries, int)
    assert isinstance(app_config.settings.agent_retry_backoff_seconds, float)
