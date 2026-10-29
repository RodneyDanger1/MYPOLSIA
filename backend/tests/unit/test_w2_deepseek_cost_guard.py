"""REQUESTS-2 (taskmaster §13.2, P2): the per-run DOLLAR cap must exist on the DeepSeek path.

MEASURED DEFECT (taskmaster, code read + live no-spend probe): `AGENT_RUN_COST_CAP_USD` was
consumed in exactly two places — the pre-dispatch ESTIMATE (`accounting.authorize`) and the Claude
CLI's own `--max-budget-usd`.  `deepseek_runtime` — the provider the live key selects — received no
dollar bound at all; its only per-run stop was the OUTPUT-TOKEN ceiling, and its cost was measured
after the fact.  "The per-run cap refuses by name when exceeded" was therefore true at dispatch and
FALSE during a DeepSeek run.

These tests pin the live guard (`accounting.RunCostGuard`) and its wiring into the per-turn loop:

  * BEFORE a call: refuse when `priced + provable worst case(call) > cap` — no spend for that call;
  * AFTER a call: record the MEASURED cost and refuse before the next turn when the run is over;
  * the refusal is the named `E_BUDGET_EXCEEDED` with the arithmetic in its context, it carries the
    measured usage of what was really spent, and the retry ladder does NOT retry it;
  * a normal run under the shipped cap is byte-identical (no new envelope keys);
  * an unpriced model is bounded at the most expensive pinned rate while its MEASUREMENT stays
    unmeasured (the guard never fabricates a cost).

RED leg (forge receipt): with the two guard calls removed from `deepseek_runtime.invoke`, the
"refuses before the call" and "over-cap call fails the run" tests fail by name; restored
byte-identically, they pass.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.agents import accounting, claude_cli, deepseek_runtime, pricing
from app.config import settings
from app.core.errors import E_BUDGET_EXCEEDED, MeridianError

SAFE_TEST_KEY = "sk-test-not-a-real-key"


def _make_inv(prompt: str = "do the thing") -> claude_cli.Invocation:
    return claude_cli.build_invocation(prompt=prompt, timeout_s=30, schema_name="test")


def _completion(content: str, *, usage: dict | None = None, finish: str = "stop") -> dict:
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


def _cap(monkeypatch, value: float) -> None:
    """Set the per-run cap the guard reads (`budget_policy_from_settings`)."""
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", value)


@pytest.fixture(autouse=True)
def _key_and_store(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)
    deepseek_runtime._reset_session_store()
    yield
    deepseek_runtime._reset_session_store()


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the live guard UNIT — the arithmetic the loop relies on
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_guard_refuses_before_the_second_call_when_the_worst_case_no_longer_fits():
    guard = accounting.RunCostGuard(cap_usd=0.005, model="deepseek-flash")

    guard.check_before_call(0.002, provider_calls=1)       # 0 + 0.002 <= 0.005 -> allowed
    guard.record_call(cost_usd=0.0015, call_bound_usd=0.002, provider_calls=1)
    assert guard.priced_usd == pytest.approx(0.0015)

    with pytest.raises(accounting.BudgetStop) as excinfo:
        guard.check_before_call(0.004, provider_calls=2)   # 0.0015 + 0.004 > 0.005

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert "next provider call" in excinfo.value.message
    assert excinfo.value.context["next_call_bound_usd"] == pytest.approx(0.004)
    assert excinfo.value.context["projected_usd"] == pytest.approx(0.0055)
    assert excinfo.value.context["cap_usd"] == pytest.approx(0.005)
    assert excinfo.value.context["provider_calls"] == 2
    assert excinfo.value.context["cost_basis"] == "measured"
    assert excinfo.value.how_to_fix                      # a human is told what to change


def test_an_unmeasured_call_falls_back_to_its_upper_bound_never_to_zero():
    guard = accounting.RunCostGuard(cap_usd=1.0, model="mystery-model")
    guard.record_call(cost_usd=None, call_bound_usd=0.25, provider_calls=1)

    assert guard.cost_basis == "upper_bound"
    assert guard.priced_usd == pytest.approx(0.25)        # the bound, not the measured 0.0
    assert guard.unmeasured_calls == 1


def test_a_non_positive_cap_fails_closed_instead_of_meaning_unlimited():
    guard = accounting.RunCostGuard(cap_usd=0.0, model="deepseek-flash")

    with pytest.raises(accounting.BudgetStop) as excinfo:
        guard.check_before_call(0.0, provider_calls=1)

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert "not usable" in excinfo.value.message
    assert "AGENT_RUN_COST_CAP_USD" in excinfo.value.how_to_fix


def test_an_unpriced_model_is_bounded_at_the_most_expensive_pinned_rate():
    flash = accounting.call_cost_bound_usd(
        "deepseek-flash", prompt_tokens_bound=100_000, output_tokens_bound=8192
    )
    unknown = accounting.call_cost_bound_usd(
        "mystery-model-9", prompt_tokens_bound=100_000, output_tokens_bound=8192
    )

    assert flash is not None and flash > 0
    assert unknown is not None and unknown >= flash
    # ...while the MEASUREMENT stays unmeasured: the guard bounds a spend, it never invents a cost.
    assert pricing.estimate_cost_usd(
        "mystery-model-9", cache_hit_tokens=10, cache_miss_tokens=10, output_tokens=10
    ) is None


def test_a_vision_prompt_is_a_partial_bound_never_a_fabricated_one():
    """An image part has no honest token bound here -> the guard prices the output ceiling only."""
    text_only = deepseek_runtime._prompt_tokens_bound([{"role": "user", "content": "hello"}], None)
    vision = deepseek_runtime._prompt_tokens_bound(
        [{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}],
        None,
    )

    assert isinstance(text_only, int) and text_only > 0
    assert vision is None


def test_a_zero_or_missing_output_bound_cannot_price_a_call():
    assert accounting.call_cost_bound_usd(
        "deepseek-flash", prompt_tokens_bound=10, output_tokens_bound=0
    ) is None
    assert accounting.call_cost_bound_usd(
        "deepseek-flash", prompt_tokens_bound=10, output_tokens_bound=True
    ) is None


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the RUNTIME wiring — a real `invoke()` against an HTTP double
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_guard_refuses_before_a_call_whose_worst_case_passes_the_cap(monkeypatch):
    _cap(monkeypatch, 0.001)
    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(200, json=_completion("{}"))

    with pytest.raises(MeridianError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler))

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert hits == []                                   # nothing was spent on that call
    context = excinfo.value.context
    assert context["cap_usd"] == pytest.approx(0.001)
    assert context["provider_calls"] == 1
    assert context["runtime"] == "deepseek"
    assert context["pricing_version"] == pricing.PRICING_VERSION
    # the retry ladder must NOT re-spend on the identical prompt
    assert claude_cli.is_retryable_failure(E_BUDGET_EXCEEDED) is False


def test_a_call_that_spends_past_the_cap_fails_the_run_and_accounts_for_it(monkeypatch):
    _cap(monkeypatch, 0.01)
    usage = {
        "prompt_tokens": 10_000_000,
        "completion_tokens": 1_000_000,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 10_000_000,
    }
    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(200, json=_completion('{"summary": "done"}', usage=usage))

    with pytest.raises(MeridianError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler))

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert len(hits) == 1                       # the call happened — that spend is REAL
    context = excinfo.value.context
    assert context["run_cost_usd"] > context["cap_usd"]
    assert context["cost_basis"] == "measured"
    # ...and the run's measured usage survives on the refusal, so the ledger can persist it.
    assert context["usage"]["input_tokens"] == 10_000_000
    assert context["usage"]["output_tokens"] == 1_000_000
    assert context["model"] == "deepseek-flash"


def test_a_normal_run_under_the_shipped_cap_is_byte_identical(monkeypatch):
    """The guard must be invisible to a run that stays inside the cap (no new envelope keys)."""
    _cap(monkeypatch, 2.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('{"summary": "real work"}'))

    result = deepseek_runtime.invoke(_make_inv(), client=_client(handler))
    envelope = json.loads(result.stdout)

    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is False
    assert set(envelope) == {
        "type", "subtype", "is_error", "result", "session_id", "num_turns", "duration_ms",
        "usage", "model", "stop_reason", "provider", "cost_estimate_usd", "pricing_version",
    }


def test_the_guard_does_not_retry_a_capped_run(monkeypatch):
    """`run_with_retries` re-raises every code but `E_CLI_TIMEOUT`; a cap must not re-spend."""
    _cap(monkeypatch, 0.001)
    calls: list[int] = []

    def dispatch() -> claude_cli.InvokeResult:
        calls.append(1)
        return deepseek_runtime.invoke(
            _make_inv(), client=_client(lambda request: httpx.Response(200, json=_completion("{}")))
        )

    with pytest.raises(MeridianError):
        claude_cli.run_with_retries(dispatch)

    assert len(calls) == 1


def test_one_guard_spans_a_logical_runs_dispatches(monkeypatch):
    """The per-RUN cap: a guard handed in by `base_agent` accumulates across invocations.

    Without this, `claude_cli.run_with_retries` would give every retry a FRESH guard and the run
    could spend its cap once per attempt (`1 + agent_max_retries` times the cap).
    """
    _cap(monkeypatch, 10.0)
    guard = accounting.RunCostGuard.from_settings("deepseek-flash")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
             "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000}

    for _ in range(2):
        deepseek_runtime.invoke(
            _make_inv(),
            client=_client(lambda request: httpx.Response(200, json=_completion("{}", usage=usage))),
            cost_guard=guard,
        )

    assert guard.calls == 2
    assert guard.measured_usd > 0

    # ...and the SAME guard now refuses the next dispatch before it spends: the cap is per run.
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", guard.cap_usd)
    guard.cap_usd = guard.measured_usd - 0.0001
    hits: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request)
        return httpx.Response(200, json=_completion("{}"))

    with pytest.raises(MeridianError) as excinfo:
        deepseek_runtime.invoke(_make_inv(), client=_client(handler), cost_guard=guard)

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert hits == []


def test_the_agent_hands_the_same_guard_to_every_ladder_dispatch(monkeypatch):
    """`base_agent._real_call` opens ONE guard per logical run and reuses it on a retry."""
    from app.agents import base_agent

    seen: list[object] = []

    def fake_invoke(inv, **kwargs):  # noqa: ANN001 - the frozen invoke seam
        seen.append(kwargs.get("cost_guard"))
        return claude_cli.InvokeResult(
            stdout=json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": '{"summary": "ok"}', "usage": {"input_tokens": 5, "output_tokens": 5},
                "model": "deepseek-flash", "num_turns": 1,
            }),
            stderr="", exit_code=0, wall_seconds=0.01, transport="http",
        )

    def fake_run_with_retries(dispatch, *, classify=None, **kwargs):  # noqa: ANN001
        dispatch()
        result = dispatch()
        return result, 2

    class _Probe(base_agent.BaseMeridianAgent):
        agent_type = "business_planning"
        output_schema = None

        def run(self, task, context):  # pragma: no cover - the seam under test is _real_call
            return {"summary": "ok"}

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    monkeypatch.setattr(deepseek_runtime, "invoke", fake_invoke)
    monkeypatch.setattr(claude_cli, "run_with_retries", fake_run_with_retries)

    _Probe()._real_call("prompt")

    assert len(seen) == 2
    assert all(isinstance(guard, accounting.RunCostGuard) for guard in seen)
    assert seen[0] is seen[1], "a retry must reuse the run's guard, not open a fresh cap"
