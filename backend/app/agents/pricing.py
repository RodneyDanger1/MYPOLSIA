"""Pinned DeepSeek price table + the four-rate token->cost estimator (L3-04).

WHY this module exists: the owner's runtime (`deepseek_runtime`) is an OpenAI-compatible
chat-completions client that returns TOKENS, not money — its completion body carries
`usage.{prompt_tokens, completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens}`
and NO cost field.  Before this module, no path turned those tokens into the money unit the
dispatch budget gate reads (`agent_runs.cost_usd`), so the day's measured spend was NULL ->
0.0 -> the $25/day cap, the per-agent limits and the per-run cap were inert for every DeepSeek
run (L3-04, probe-measured).  The provider documents the pricing rule itself — "expense =
number of tokens x price ... deducted from your topped-up balance" — so measuring the cost from
measured tokens is arithmetic on published rates, not a fabrication.

The table (USD per 1M tokens; api-docs.deepseek.com pricing, fetched 2026-10-13).  Peak pricing
applies 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday; everything else is off-peak.  The
documented exception — Chinese public holidays are billed off-peak — is NOT modelled (there is
no machine-readable calendar in this repo, and inventing one at runtime is forbidden), so during
a holiday inside a peak window the estimate uses the PEAK rate: the estimate can only OVER-state
that call's cost, which is the safe direction for a spend gate.  Documented as an upper bound.

HONESTY RULES (repo-wide):
  * an unknown model is UNMEASURED — `estimate_cost_usd` returns None, never a guessed rate;
  * any unmeasured token count (None) makes the estimate None — a partial sum is fabrication;
  * the estimate is emitted under a DISTINCT key (`cost_estimate_usd`, never
    `total_cost_usd`) so no consumer can mistake it for a provider bill;
  * `PRICING_VERSION` pins the table so every persisted estimate is attributable.

RESIDUAL — aborted-attempt tokens are NOT estimated (L3-05, honest, not silently fixed):
this estimator prices ONE completion body: the one a SUCCESSFUL attempt returned.  Tokens a
provider already billed for an attempt we aborted (a timeout, a killed CLI, an envelope-shaped
failure the ladder declined to retry) never reach a usage call, and earlier retry attempts'
envelopes are not summed either — `base_agent.usage_metadata` prices the LAST, successful
attempt's `usage` block.  So `agent_runs.cost_usd` (when `cost_source="token_estimate"`) is a
LOWER BOUND on the provider's real charge for a run whose ladder had to retry or whose
predecessor attempt died: the estimate UNDER-counts exactly by the aborted attempts' tokens.
Nothing here fabricates a number for them — an unmeasured attempt must not be invented into the
row — and the under-count is bounded by `settings.agent_max_retries` aborted attempts per run.
A future split (persisting each attempt's envelope) is what would close it; until then the
residual is documented rather than hidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

#: The pin: bump when any rate or window below changes. Persisted with every estimate
#: (`usage_json.pricing_version`) so a cost can always be traced to the table that produced it.
PRICING_VERSION = "deepseek-pricing-2026-10-13"


@dataclass(frozen=True)
class ModelRates:
    """One model's four+two rates, USD per 1,000,000 tokens."""

    cache_hit_per_mtok_offpeak: float
    cache_hit_per_mtok_peak: float
    cache_miss_per_mtok_offpeak: float
    cache_miss_per_mtok_peak: float
    output_per_mtok_offpeak: float
    output_per_mtok_peak: float


#: The published rates (the two models the runtime can request; see deepseek_runtime constants).
MODEL_RATES: dict[str, ModelRates] = {
    "deepseek-flash": ModelRates(
        cache_hit_per_mtok_offpeak=0.003,
        cache_hit_per_mtok_peak=0.006,
        cache_miss_per_mtok_offpeak=0.15,
        cache_miss_per_mtok_peak=0.30,
        output_per_mtok_offpeak=0.60,
        output_per_mtok_peak=1.20,
    ),
    "deepseek-v4-pro": ModelRates(
        cache_hit_per_mtok_offpeak=0.022,
        cache_hit_per_mtok_peak=0.044,
        cache_miss_per_mtok_offpeak=0.66,
        cache_miss_per_mtok_peak=1.32,
        output_per_mtok_offpeak=1.98,
        output_per_mtok_peak=3.96,
    ),
}

#: Peak windows, UTC hours, half-open [start, end): 01:00-04:00 and 06:00-10:00, Mon-Fri.
_PEAK_HOUR_WINDOWS: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak_pricing(now: datetime | None = None) -> bool:
    """Is `now` (UTC) inside a documented peak-pricing window?

    Weekends are never peak.  The holiday exception is deliberately not modelled — see the
    module docstring: peak rates are an UPPER BOUND on the estimate, never an under-count.
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    if moment.weekday() >= 5:                      # Sat/Sun -> off-peak
        return False
    return any(start <= moment.hour < end for start, end in _PEAK_HOUR_WINDOWS)


def estimate_cost_usd(
    model: str,
    *,
    cache_hit_tokens: int | None,
    cache_miss_tokens: int | None,
    output_tokens: int | None,
    now: datetime | None = None,
) -> float | None:
    """Token-derived cost estimate for one DeepSeek call — or None (UNMEASURED).

    The four-rate sum the provider's own pricing rule implies:
        cost = hit/1e6 * hit_rate + miss/1e6 * miss_rate + out/1e6 * out_rate
    Any count that is None (unmeasured) makes the whole estimate None: a one-sided sum would
    be fabrication-precision (the same rule as `accounting.tokens_used`).  An unknown model is
    None too — a rate this table does not pin is a rate this code must not guess.
    """
    rates = MODEL_RATES.get(str(model or "").strip())
    if rates is None:
        return None
    if cache_hit_tokens is None or cache_miss_tokens is None or output_tokens is None:
        return None
    peak = is_peak_pricing(now)
    hit_rate = rates.cache_hit_per_mtok_peak if peak else rates.cache_hit_per_mtok_offpeak
    miss_rate = rates.cache_miss_per_mtok_peak if peak else rates.cache_miss_per_mtok_offpeak
    out_rate = rates.output_per_mtok_peak if peak else rates.output_per_mtok_offpeak
    return (
        cache_hit_tokens / 1_000_000 * hit_rate
        + cache_miss_tokens / 1_000_000 * miss_rate
        + output_tokens / 1_000_000 * out_rate
    )


__all__ = [
    "MODEL_RATES",
    "PRICING_VERSION",
    "ModelRates",
    "estimate_cost_usd",
    "is_peak_pricing",
]
