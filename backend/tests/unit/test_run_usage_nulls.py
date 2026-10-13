"""T9 (accounting side) — NULL vs measured zero, one state per test.

The defect class this pins: `or 0` / zero-defaults collapsed "the CLI did not report it" into
"it was 0", so `agent_runs` carried fabricated precision (`tokens_used=0` for a run nobody
measured) and a measured zero was indistinguishable from an unknown one. Every test below pins
ONE of the two states; the RED legs (seats/S4-runtime/raw/redleg-*) re-plant the collapse and
show the corresponding test failing.

Covered here:
  * `accounting.spend_summary` (T8/F-08): None spend stays None; a measured 0.0 stays 0.0;
  * `RunUsage`: one-sided/absent measurements are None; `tokens_used` is None unless BOTH sides
    are known; `as_dict()` passes None through (no `round(None)`, no `cost_estimate_usd=0.0`);
  * `usage_from_envelope`: an absent (or `null`) key is None; a present 0 is a measured zero;
  * `_apply_run_usage` (the queue side): measured zero persists as 0; absent keys leave the
    columns NULL; a one-sided alias sum stays NULL; both sides present -> the sum.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.agents.accounting import RunUsage, spend_summary, usage_from_envelope
from celery_app.tasks.agent_tasks import _apply_run_usage


# ─────────────────────────────────────────────────────────────────────────────
# T8 — spend_summary: the one honest spend helper
# ─────────────────────────────────────────────────────────────────────────────
def test_spend_summary_unknown_spend_stays_none():
    """An unmeasured day spend reports None — never `$0.00` (F-08: the deleted base_agent block)."""
    summary = spend_summary(None, 25.0)

    assert summary["spent"] is None
    assert summary["remaining"] is None
    assert summary["limit"] == 25.0


def test_spend_summary_measured_zero_stays_zero():
    """A measured 0.0 is a real zero: it must NOT collapse into None either."""
    summary = spend_summary(0.0, 25.0)

    assert summary["spent"] == 0.0
    assert summary["spent"] is not None
    assert summary["remaining"] == 25.0


def test_spend_summary_partial_spend():
    assert spend_summary(2.5, 10.0) == {"limit": 10.0, "spent": 2.5, "remaining": 7.5}


# ─────────────────────────────────────────────────────────────────────────────
# RunUsage — measurement fields and the one-sided sum
# ─────────────────────────────────────────────────────────────────────────────
def test_run_usage_tokens_used_none_when_either_side_unknown():
    assert RunUsage().tokens_used is None
    assert RunUsage(input_tokens=10).tokens_used is None
    assert RunUsage(output_tokens=10).tokens_used is None


def test_run_usage_tokens_used_sums_when_both_measured():
    assert RunUsage(input_tokens=0, output_tokens=0).tokens_used == 0
    assert RunUsage(input_tokens=10, output_tokens=5).tokens_used == 15


def test_run_usage_as_dict_passes_none_through_without_rounding():
    """`round(None)` used to be impossible only because every field defaulted to 0.0."""
    as_dict = RunUsage(session_id="s1").as_dict()

    assert as_dict["tokens_input"] is None
    assert as_dict["tokens_output"] is None
    assert as_dict["tokens_used"] is None
    assert as_dict["cost_estimate_usd"] is None
    assert as_dict["num_turns"] is None
    assert as_dict["usage_json"]["total_cost_usd"] is None


# ─────────────────────────────────────────────────────────────────────────────
# usage_from_envelope — absent/null vs measured zero
# ─────────────────────────────────────────────────────────────────────────────
def test_usage_from_envelope_absent_keys_are_none():
    usage = usage_from_envelope({"session_id": "s"})

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_creation_tokens is None
    assert usage.total_cost_usd is None
    assert usage.duration_ms is None
    assert usage.num_turns is None
    assert usage.tokens_used is None


def test_usage_from_envelope_null_keys_are_none():
    """A JSON `null` is the same statement as an absent key: unmeasured."""
    usage = usage_from_envelope(
        {"usage": {"input_tokens": None, "output_tokens": None}, "total_cost_usd": None, "num_turns": None}
    )

    assert usage.input_tokens is None
    assert usage.total_cost_usd is None
    assert usage.num_turns is None


def test_usage_from_envelope_measured_zero_stays_zero():
    usage = usage_from_envelope(
        {
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "total_cost_usd": 0.0,
            "duration_ms": 0,
            "num_turns": 0,
        }
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.tokens_used == 0
    assert usage.total_cost_usd == 0.0
    assert usage.num_turns == 0
    assert usage.as_dict()["cost_estimate_usd"] == 0.0


def test_usage_from_envelope_one_sided_measurement_keeps_tokens_used_none():
    usage = usage_from_envelope({"usage": {"input_tokens": 12}})

    assert usage.input_tokens == 12
    assert usage.output_tokens is None
    assert usage.tokens_used is None


# ─────────────────────────────────────────────────────────────────────────────
# _apply_run_usage — the queue side: NULL stays NULL, measured zero is written
# ─────────────────────────────────────────────────────────────────────────────
def _run_row() -> Any:
    """A stand-in for the `AgentRun` row: unset accounting columns read as None (NULL)."""
    return SimpleNamespace(
        tokens_input=None,
        tokens_output=None,
        tokens_used=None,
        cli_session_id=None,
        cost_usd=None,
        usage_json=None,
    )


def test_apply_run_usage_measured_zero_persists_as_zero():
    row = _run_row()

    _apply_run_usage(row, {"tokens_input": 0, "tokens_output": 0, "tokens_used": 0, "cost_usd": 0.0})

    assert row.tokens_input == 0
    assert row.tokens_output == 0
    assert row.tokens_used == 0
    assert row.cost_usd == 0.0
    assert row.cost_usd is not None


def test_apply_run_usage_absent_keys_leave_columns_null():
    row = _run_row()

    _apply_run_usage(row, {"summary": "completed with no measured usage"})

    assert row.tokens_input is None
    assert row.tokens_output is None
    assert row.tokens_used is None
    assert row.cost_usd is None
    assert row.usage_json is None  # nothing was recorded -> nothing invented


def test_apply_run_usage_one_sided_alias_leaves_tokens_used_null():
    """RED leg 3: the old `int(tokens_input or 0) + int(tokens_output or 0)` made this 42."""
    row = _run_row()

    _apply_run_usage(row, {"tokens_input": 42})

    assert row.tokens_input == 42
    assert row.tokens_used is None


def test_apply_run_usage_both_sides_sum():
    row = _run_row()

    _apply_run_usage(row, {"tokens_input": 42, "tokens_output": 8})

    assert row.tokens_input == 42
    assert row.tokens_output == 8
    assert row.tokens_used == 50


def test_apply_run_usage_both_sides_zero_sums_to_measured_zero():
    row = _run_row()

    _apply_run_usage(row, {"tokens_input": 0, "tokens_output": 0})

    assert row.tokens_used == 0
    assert row.tokens_used is not None


def test_apply_run_usage_explicit_none_tokens_used_never_becomes_zero():
    """The runtime's own `tokens_used=None` (one side unknown) must stay NULL — never 0."""
    row = _run_row()

    _apply_run_usage(row, {"tokens_used": None, "tokens_input": 42})

    assert row.tokens_input == 42
    assert row.tokens_used is None


def test_apply_run_usage_an_explicit_none_is_recomputed_when_both_sides_are_known():
    """`elif tokens_input is not None and tokens_output is not None` — both sides measured, so the
    total is a real sum even when the runtime left `tokens_used` unset."""
    row = _run_row()

    _apply_run_usage(row, {"tokens_used": None, "tokens_input": 42, "tokens_output": 8})

    assert row.tokens_used == 50
