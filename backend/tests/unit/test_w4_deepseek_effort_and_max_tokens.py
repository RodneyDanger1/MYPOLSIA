"""SV-W4-06 / SV-W4-07 (wave 4, systems-validator findings in the agent runtime).

Both findings are about the DeepSeek runtime claiming a knob it does not have:

  * **SV-W4-06** — the vendor's documented `reasoning_effort` alias table maps `ultra -> max`
    (`api-docs.deepseek.com/guides/thinking_mode`, retrieved 2026-10-29).  `REASONING_EFFORT_ALIASES`
    omitted it, so `DEEPSEEK_REASONING_EFFORT=ultra` raised a NAMED `E_VALIDATION` refusal instead
    of running at `max`, and the refusal's own "documented aliases" list was incomplete.
  * **SV-W4-07** — `_max_tokens()` reads `deepseek_max_tokens`, but that name was NOT a `Settings`
    field.  `Settings` is `extra="ignore"`, so `DEEPSEEK_MAX_TOKENS=16384` in `.env` was silently
    discarded and every call stayed at the 8,192 default — a bound the operator could not raise.

Pinned here: the documented alias runs as `max`; the refusal names every documented alias; and
`deepseek_max_tokens` is a real field whose environment value reaches the call's bound.
"""
from __future__ import annotations

import pytest

from app.agents import deepseek_runtime
from app.config import Settings, settings


def test_the_documented_ultra_alias_runs_as_max(monkeypatch) -> None:
    """RED at HEAD: `ultra` was refused with E_VALIDATION instead of mapping to max."""
    monkeypatch.setattr(settings, "deepseek_reasoning_effort", "ultra")
    assert deepseek_runtime._reasoning_effort() == "max"


def test_the_refusal_names_every_documented_alias(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deepseek_reasoning_effort", "turbo")
    with pytest.raises(Exception) as excinfo:
        deepseek_runtime._reasoning_effort()
    text = str(excinfo.value)
    assert "ultra" in text, "the refusal must list every alias the vendor documents"
    assert "max" in text


def test_deepseek_max_tokens_is_a_real_declarable_setting(monkeypatch) -> None:
    """RED at HEAD: the name was not a Settings field, so the env var was ignored."""
    assert "deepseek_max_tokens" in Settings.model_fields

    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "16384")
    built = Settings(_env_file=None)
    assert built.deepseek_max_tokens == 16384


def test_the_call_bound_reads_the_configured_max_tokens(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deepseek_max_tokens", 16384)
    assert deepseek_runtime._max_tokens() == 16384
    # A non-positive value is the shipped default bound, never "unbounded".
    monkeypatch.setattr(settings, "deepseek_max_tokens", 0)
    assert deepseek_runtime._max_tokens() == deepseek_runtime.DEEPSEEK_DEFAULT_MAX_TOKENS
