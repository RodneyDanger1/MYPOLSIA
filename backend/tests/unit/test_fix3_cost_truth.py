"""L3-04 / L3-12 — DeepSeek cost truth: the pinned price table, the four-rate token estimate,
the persisted `cost_usd` + `cost_source` stamp, and the measured model key.

What this file pins (findings lane-3 L3-04/L3-12 + research-C):

* the price table is DATA with a version (`PRICING_VERSION`) and the documented peak window
  (01:00-04:00 / 06:00-10:00 UTC Mon-Fri; weekends off-peak; the Chinese-holiday exception is
  deliberately not modelled -> documented upper bound);
* `estimate_cost_usd` = hit/1e6*hit_rate + miss/1e6*miss_rate + out/1e6*out_rate; unknown model
  or any unmeasured count -> None (never a guess); zero counts -> a measured 0.0;
* the runtime emits `cost_estimate_usd` (+ `pricing_version`) — a DISTINCT key, never
  `total_cost_usd` — and completes an unreported cache split the CONSERVATIVE way;
* `usage_from_envelope` reads it into `token_cost_estimate_usd` and stamps `cost_source`;
* `base_agent.usage_metadata` writes it as `cost_usd` (the column the D3 gate sums) and
  `_apply_run_usage` persists both the column and the `usage_json.cost_source` stamp;
* the measured served `model` survives into `RunUsage`/`usage_json` for the DeepSeek shape
  (single `model` string, no `modelUsage`), and absent stays absent.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.agents import accounting, claude_cli, deepseek_runtime, pricing
from app.agents.base_agent import BasePolsiaAgent

SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                  "required": ["summary"]}


def _completion(*, usage: dict) -> dict:
    return {
        "id": "chat-cost", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": '{"summary": "done"}'}}],
        "usage": usage,
    }


def _invoke(usage: dict) -> dict:
    """Drive the REAL runtime over an in-memory transport; return the parsed envelope."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(usage=usage))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    inv = claude_cli.build_invocation(prompt="price me", json_schema=SUMMARY_SCHEMA,
                                      max_turns=1, system_prompt_static="probe")
    result = deepseek_runtime.invoke(inv, client=client, api_key="sk-probe-key")
    return json.loads(result.stdout)


class _Probe(BasePolsiaAgent):
    agent_type = "finance"

    def run(self, task, context):  # pragma: no cover
        return {}


# ───────────────────────────────────────────────────────────────────── pricing table


def test_the_price_table_is_pinned_with_a_version():
    assert pricing.PRICING_VERSION == "deepseek-pricing-2026-10-13"
    flash = pricing.MODEL_RATES["deepseek-flash"]
    assert (flash.cache_hit_per_mtok_offpeak, flash.cache_hit_per_mtok_peak) == (0.003, 0.006)
    assert (flash.cache_miss_per_mtok_offpeak, flash.cache_miss_per_mtok_peak) == (0.15, 0.30)
    assert (flash.output_per_mtok_offpeak, flash.output_per_mtok_peak) == (0.60, 1.20)
    pro = pricing.MODEL_RATES["deepseek-v4-pro"]
    assert (pro.cache_hit_per_mtok_offpeak, pro.cache_hit_per_mtok_peak) == (0.022, 0.044)
    assert (pro.cache_miss_per_mtok_offpeak, pro.cache_miss_per_mtok_peak) == (0.66, 1.32)
    assert (pro.output_per_mtok_offpeak, pro.output_per_mtok_peak) == (1.98, 3.96)


@pytest.mark.parametrize("moment,peak", [
    (datetime(2026, 10, 14, 0, 59, tzinfo=UTC), False),   # Wed 00:59 -> before the first window
    (datetime(2026, 10, 14, 1, 0, tzinfo=UTC), True),     # Wed 01:00 -> peak begins
    (datetime(2026, 10, 14, 3, 59, tzinfo=UTC), True),
    (datetime(2026, 10, 14, 4, 0, tzinfo=UTC), False),    # gap between the two windows
    (datetime(2026, 10, 14, 6, 0, tzinfo=UTC), True),     # second window
    (datetime(2026, 10, 14, 9, 59, tzinfo=UTC), True),
    (datetime(2026, 10, 14, 10, 0, tzinfo=UTC), False),
    (datetime(2026, 10, 17, 2, 0, tzinfo=UTC), False),    # Saturday -> never peak
    (datetime(2026, 10, 18, 7, 0, tzinfo=UTC), False),    # Sunday
])
def test_the_peak_window_boundaries_are_the_documented_ones(moment, peak):
    assert pricing.is_peak_pricing(moment) is peak


def test_estimate_is_the_four_rate_sum_and_unknown_inputs_stay_unmeasured():
    off_peak = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)   # Wed, off-peak
    peak = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)        # Wed, peak

    got = pricing.estimate_cost_usd(
        "deepseek-flash", cache_hit_tokens=700_000, cache_miss_tokens=0,
        output_tokens=0, now=off_peak,
    )
    assert got == pytest.approx(700_000 / 1e6 * 0.003)

    got_peak = pricing.estimate_cost_usd(
        "deepseek-flash", cache_hit_tokens=700_000, cache_miss_tokens=0,
        output_tokens=0, now=peak,
    )
    assert got_peak == pytest.approx(700_000 / 1e6 * 0.006)

    assert pricing.estimate_cost_usd(
        "deepseek-unknown", cache_hit_tokens=1, cache_miss_tokens=1, output_tokens=1, now=off_peak
    ) is None
    assert pricing.estimate_cost_usd(
        "deepseek-flash", cache_hit_tokens=None, cache_miss_tokens=1, output_tokens=1,
        now=off_peak,
    ) is None
    assert pricing.estimate_cost_usd(
        "deepseek-flash", cache_hit_tokens=0, cache_miss_tokens=0, output_tokens=0,
        now=off_peak,
    ) == 0.0, "measured zeroes are a REAL zero estimate"


# ───────────────────────────────────────────────────────────── runtime envelope


def test_the_envelope_carries_a_distinct_cost_estimate_key(monkeypatch):
    monkeypatch.setattr(pricing, "is_peak_pricing", lambda now=None: False)  # deterministic
    envelope = _invoke({"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
                        "prompt_cache_hit_tokens": 3})

    expected = (3 / 1e6 * 0.003) + ((1234 - 3) / 1e6 * 0.15) + (567 / 1e6 * 0.60)
    assert envelope["cost_estimate_usd"] == pytest.approx(round(expected, 8))
    assert envelope["pricing_version"] == pricing.PRICING_VERSION
    assert "total_cost_usd" not in envelope, "the CLI's money key is never fabricated"
    # the measured usage fields are untouched (None stays None)
    assert envelope["usage"] == {"input_tokens": 1234, "output_tokens": 567,
                                 "cache_read_input_tokens": 3}


def test_an_unreported_cache_split_is_priced_conservatively(monkeypatch):
    """No hit side reported -> the whole prompt is billed at the MISS rate (the pricier side)."""
    monkeypatch.setattr(pricing, "is_peak_pricing", lambda now=None: False)
    envelope = _invoke({"prompt_tokens": 100_000, "completion_tokens": 50})

    expected = (100_000 / 1e6 * 0.15) + (50 / 1e6 * 0.60)
    assert envelope["cost_estimate_usd"] == pytest.approx(round(expected, 8))
    assert envelope["usage"]["cache_read_input_tokens"] is None, "the envelope stays measured-or-None"


def test_a_missing_completion_count_leaves_the_cost_unmeasured(monkeypatch):
    monkeypatch.setattr(pricing, "is_peak_pricing", lambda now=None: False)
    envelope = _invoke({"prompt_tokens": 100_000})
    assert "cost_estimate_usd" not in envelope
    assert "pricing_version" not in envelope


def test_the_request_body_carries_the_max_tokens_bound(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(usage={"prompt_tokens": 1, "completion_tokens": 1}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    inv = claude_cli.build_invocation(prompt="bound me", json_schema=SUMMARY_SCHEMA, max_turns=1)
    deepseek_runtime.invoke(inv, client=client, api_key="sk-probe-key")

    assert captured["body"]["max_tokens"] == deepseek_runtime.DEEPSEEK_DEFAULT_MAX_TOKENS == 8192


# ──────────────────────────────────────────────────── accounting + persistence chain


def test_usage_from_envelope_reads_the_estimate_and_stamps_the_source():
    env = {
        "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40},
        "model": "deepseek-flash",
        "cost_estimate_usd": 0.000123,
        "pricing_version": pricing.PRICING_VERSION,
    }
    usage = accounting.usage_from_envelope(env)

    assert usage.token_cost_estimate_usd == pytest.approx(0.000123)
    assert usage.cost_source == "token_estimate"
    assert usage.pricing_version == pricing.PRICING_VERSION
    as_dict = usage.as_dict()
    assert as_dict["model"] == "deepseek-flash", "the measured served model is persisted (L3-12)"
    assert as_dict["usage_json"]["cost_source"] == "token_estimate"
    assert as_dict["usage_json"]["token_cost_estimate_usd"] == pytest.approx(0.000123)
    assert as_dict["usage_json"]["modelUsage"] == {"deepseek-flash": {}}


def test_a_cli_envelope_keeps_the_cli_money_path_and_joins_models():
    env = {
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {"claude-a": {}, "claude-b": {}},
        "total_cost_usd": 0.25,
    }
    usage = accounting.usage_from_envelope(env)

    assert usage.cost_source == "cli_estimate"
    assert usage.as_dict()["model"] == "claude-a,claude-b"
    assert usage.token_cost_estimate_usd is None


def test_no_model_reported_stays_absent_distinguishable_from_empty():
    usage = accounting.usage_from_envelope({"usage": {"input_tokens": 1}})
    assert usage.models == {}
    assert usage.as_dict()["model"] == ""


def test_the_estimate_reaches_the_column_and_the_usage_json_stamp(monkeypatch):
    """The full DeepSeek chain: envelope -> RunUsage -> meta -> `_apply_run_usage` -> column."""
    monkeypatch.setattr(pricing, "is_peak_pricing", lambda now=None: False)
    envelope = _invoke({"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
                        "prompt_cache_hit_tokens": 3})

    agent = _Probe()
    agent.last_usage = accounting.usage_from_envelope(envelope)
    meta = agent.usage_metadata()

    # the meta path rounds to 6 decimals like every other money write in this module
    assert meta["cost_usd"] == pytest.approx(round(envelope["cost_estimate_usd"], 6))
    assert meta["model"] == "deepseek-flash"

    from celery_app.tasks.agent_tasks import _apply_run_usage

    row = SimpleNamespace(tokens_input=None, tokens_output=None, tokens_used=None,
                          cli_session_id=None, cost_usd=None, usage_json=None)
    _apply_run_usage(row, meta)

    assert float(row.cost_usd) == pytest.approx(round(envelope["cost_estimate_usd"], 6))
    assert row.usage_json["cost_source"] == "token_estimate"
    assert row.usage_json["pricing_version"] == pricing.PRICING_VERSION
    assert row.usage_json["model"] == "deepseek-flash"
