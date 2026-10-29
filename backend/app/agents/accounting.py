"""Polsia agent runtime — cost/token accounting and the pre-dispatch spend gate.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/accounting.py`) with the ledger
section dropped: persistence belongs to the queue/API layer (`agent_runs` + `spend_ledger` via
`app.services`), while this module owns the *measurement* and the *gate*.

The defect this fixes (brief §5, D-level evidence in lane 05): `agent_runs.tokens_used` /
`cost_usd` existed from the first migration and were NEVER populated — every row NULL — because
`finish_agent_run(db, run.id, status, output=result, duration_secs=duration)` never saw the
envelope. `usage_from_envelope()` below turns the CLI envelope into those columns, and
`BasePolsiaAgent` attaches the result under documented keys so the queue can write them.

Two documented caveats drive the design (CLI-CONTRACT.md §2.1, D1/D4):

1. `total_cost_usd` is a CLIENT-SIDE ESTIMATE ("not authoritative billing data") → it is stored
   as an estimate and is only ever used for budgeting.
2. On `--resume`/`--continue` the result's total covers the WHOLE session ("Read the latest
   result for the session total; summing results double-counts the restored spend") → we keep a
   per-session high-water mark and account only the delta.
3. **Missing is not zero (F-10/M2).** Every measurement below is `int | None` / `float | None`:
   an envelope that OMITS a key (or sends `null`) reports `None`, and the queue leaves the
   `agent_runs` column NULL; a key present with `0` is a measured zero and is written as `0`.
   `tokens_used` is None whenever either side is unknown — a one-sided sum is fabrication.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents import pricing
from app.core.errors import E_BUDGET_EXCEEDED, E_POLICY_DENIED, PolsiaError

# ---------------------------------------------------------------------------
# Tool evidence (V22/L1)
# ---------------------------------------------------------------------------

#: How many entries one run record's `tools_used` list may carry.  The envelope is
#: provider-controlled, so the persistence boundary bounds it even though the DeepSeek runtime
#: already caps its own list (32 unique names): a future/foreign producer must not be able to
#: inflate a ledger row.  A list that OVERFLOWS keeps its first `TOOLS_USED_MAX - 1` names and
#: spends the last slot on `TOOLS_USED_TRUNCATION`, so "capped" is auditable and never silent.
TOOLS_USED_MAX = 50

#: The final entry of a capped list.  It is deliberately NOT a plausible tool name: the leading
#: ellipsis and the "more; capped at" wording exist so a consumer renders it as a NOTE, not as a
#: tool that was called.
TOOLS_USED_TRUNCATION = "… (+{omitted} more; capped at {limit})"


def bounded_tools_used(value: Any) -> list[str] | None:
    """One `tools_used` value as the list the ledger stores, or None.

    THE THREE STATES this lane exists to keep distinct:

      * `None` — NOT RECORDED: the key was absent/null, or the shape is junk (a string, a
        mapping, a list with no string entries).  Never coerced into `[]`.
      * `[]`   — RECORDED AND NONE: tools were available and the model called none (a measured
        zero, exactly like `tokens_used == 0`).
      * list   — the recorded names, order preserved, at most `TOOLS_USED_MAX` entries.  An
        overflowing list keeps the first `TOOLS_USED_MAX - 1` names and appends the truncation
        marker as its last entry.

    Non-string entries are DROPPED, never stringified: `7` is not a tool name, and inventing
    `"7"` would present junk as a recorded fact.  A list whose entries are ALL junk is not a
    measurement at all -> None.
    """
    if not isinstance(value, list):
        return None
    names = [item for item in value if isinstance(item, str) and item]
    if not names:
        return [] if not value else None
    if len(names) <= TOOLS_USED_MAX:
        return names
    kept = names[: TOOLS_USED_MAX - 1]
    return [*kept, TOOLS_USED_TRUNCATION.format(omitted=len(names) - len(kept), limit=TOOLS_USED_MAX)]


# ---------------------------------------------------------------------------
# Zero-provider-call settlement (F-W4-TM-1)
# ---------------------------------------------------------------------------
#
# MEASURED DEFECT (live, 2026-10-28): run 318 (task 324, the approval-replay drill) finished with
# ZERO model provider calls — the replay performs the STORED approved actions and never asks the
# model — so nothing was measured and `agent_runs.cost_usd` stayed NULL.  The day-spend doctrine
# cannot tell "no provider call -> $0.0000" from "a call happened and its cost was lost", so it
# treated the whole UTC day as UNMEASURED and `planner_service._gate_planner_budget` refused every
# paid planner dispatch for the rest of the day.  The refusal was SAFE but operationally wrong:
# one model-free run blocked the cap.
#
# THE RULE THIS CONTRACT ENFORCES: a NULL cost may only become a measured 0 when the run's own
# path DECLARES zero calls.  Absence of usage is NOT such a declaration — that is exactly the
# F-19/F-20 shape (a provider call whose measurement was lost), and those runs must keep refusing
# by name.  A declaration is `provider_calls: 0` (+ the originating path in
# `provider_calls_source`) on the run's outcome dict, or on a refusal's `context` for a path that
# refuses before it can produce an outcome.  Only a non-negative, non-bool `int` counts; a string
# "0", a float, a negative or a missing key is UNKNOWN and settles nothing.
#
# `agent_runs.cost_usd` measures MODEL provider spend (the column the day cap sums).  A replay may
# still perform a real SIDE-EFFECT send through a non-model provider; that is not model spend and
# is recorded by the artifact ledger, not here.

#: The outcome/refusal key a run path uses to DECLARE its own model-provider call count.
PROVIDER_CALLS_KEY = "provider_calls"
#: The companion key naming WHICH path declared the count (`approval_replay`,
#: `dispatch_preflight`, …) — the audit trail behind a settlement that is a derivation, not a
#: provider report.
PROVIDER_CALLS_SOURCE_KEY = "provider_calls_source"
#: `usage_json.cost_source` label for a settled measured zero (the F-W4-TM-1 fix).  A stored cost
#: is always attributable; this label says "$0.0000 because the run made zero model provider
#: calls", never "we looked and found nothing".
NO_PROVIDER_CALLS_COST_SOURCE = "no_provider_calls"


def provider_call_declaration(payload: Any) -> tuple[int, str] | None:
    """The model-provider call count a run path DECLARED in `payload`, or None (unknown).

    POSITIVE declarations only.  The caller passes the run's outcome dict, or a refusal's
    `context` for a path that refuses before it can produce an outcome (the dispatch preflight,
    which raises before the agent exists — zero calls by construction).

    Absence is UNKNOWN, never zero: a run whose provider call happened but whose usage never
    reached the ledger must keep its NULL cost (and keep refusing the day cap), which is the whole
    reason this function returns None instead of defaulting.  Junk (a string, a float, a bool, a
    negative) is UNKNOWN too — never coerced into a number a settlement could act on.
    """
    if not isinstance(payload, dict):
        return None
    raw_calls = payload.get(PROVIDER_CALLS_KEY)
    if isinstance(raw_calls, bool) or not isinstance(raw_calls, int) or raw_calls < 0:
        return None
    raw_source = payload.get(PROVIDER_CALLS_SOURCE_KEY)
    source = raw_source.strip() if isinstance(raw_source, str) and raw_source.strip() else ""
    return raw_calls, source or "declared"


# ---------------------------------------------------------------------------
# Envelope -> usage record
# ---------------------------------------------------------------------------

@dataclass
class RunUsage:
    """One run's measured usage. Every MEASUREMENT is `int | None` / `float | None`: `None` is
    "the CLI did not report this", and a measured `0` is a real zero — the two must never collapse
    into one representation (F-10/M2: `or 0` used to turn every absent key into a fake zero).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_cost_usd: float | None = None   # the envelope's cumulative-for-session estimate
    #: The provider's TOKEN-DERIVED cost estimate (L3-04; DeepSeek emits `cost_estimate_usd`,
    #: never `total_cost_usd`). Kept as its own field so the two can never be confused: this is
    #: arithmetic over measured tokens using a pinned price table, not a provider bill.
    token_cost_estimate_usd: float | None = None
    #: Which measurement `cost_usd` would come from: "cli_estimate" (the CLI's own money),
    #: "token_estimate" (the pinned table over measured tokens) or "" (nothing measured). It is
    #: persisted inside `usage_json` so a stored cost is always attributable.
    cost_source: str = ""
    #: The price-table pin behind a token estimate (`pricing.PRICING_VERSION`), "" when none.
    pricing_version: str = ""
    duration_ms: int | None = None
    num_turns: int | None = None
    #: V22/L1 — the runtime's tool evidence, names only.  `None` = the envelope carried no
    #: `tools_used` key (never recorded); `[]` = tools were available and none ran (measured
    #: zero); a list = the names, already bounded by `bounded_tools_used`.  The distinction is
    #: the whole point of the column: `[]` must never stand for "unknown".
    tools_used: list[str] | None = None
    is_error: bool = False
    subtype: str = ""
    session_id: str = ""
    models: dict[str, dict] = field(default_factory=dict)
    permission_denials: int = 0
    terminal_reason: str = ""

    @property
    def tokens_used(self) -> int | None:
        """input + output, or None when EITHER side is unmeasured.

        A one-sided sum is fabrication-precision (F-10/R8): adding a real count to a fabricated
        `0` invents a total the CLI never reported. One unknown side -> the column stays NULL.
        """
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        """The documented result keys the queue persists into `agent_runs`.

        Unmeasured values pass through as None so `_apply_run_usage` leaves those columns NULL;
        `cost_estimate_usd` is None when `total_cost_usd` was never reported (never `round(None)`,
        never `0.0`).

        V22/L1: `tools_used` is included ONLY when the envelope actually carried it.  A tool-less
        envelope must not grow a `tools_used: None` key — that key would make "never recorded"
        look like a recorded value, and `_apply_run_usage` reads presence to decide the column.
        """
        payload: dict[str, Any] = {
            "cli_session_id": self.session_id,
            "tokens_input": self.input_tokens,
            "tokens_output": self.output_tokens,
            "tokens_used": self.tokens_used,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_estimate_usd": None if self.total_cost_usd is None else round(self.total_cost_usd, 6),
            "num_turns": self.num_turns,
            "model": ",".join(sorted(self.models)),
            "is_error": self.is_error,
            "terminal_reason": self.terminal_reason,
            "usage_json": {
                "usage": {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "cache_read_input_tokens": self.cache_read_tokens,
                    "cache_creation_input_tokens": self.cache_creation_tokens,
                },
                "modelUsage": self.models,
                "total_cost_usd": self.total_cost_usd,
                # L3-04: the token-derived estimate rides under its OWN key (never
                # `cost_estimate_usd`, which means the CLI's own money in this schema), stamped
                # with the source and the price-table pin that produced it.
                "token_cost_estimate_usd": self.token_cost_estimate_usd,
                "cost_source": self.cost_source or None,
                "pricing_version": self.pricing_version or None,
                "duration_ms": self.duration_ms,
                "num_turns": self.num_turns,
                "session_id": self.session_id,
                "subtype": self.subtype,
            },
        }
        if self.tools_used is not None:
            payload["tools_used"] = list(self.tools_used)
        return payload


def _int_or_none(value: Any) -> int | None:
    """Envelope integer: an absent key OR a JSON `null` is NOT MEASURED (None); a present `0` is
    a measured zero and stays 0. The old `int(usage.get(k) or 0)` collapsed the two (F-10).

    TOTAL by construction (R20 item 1): a value whose TYPE cannot be a count is UNMEASURED —
    the same state as an absent key — never a raise and never a coerced number. RD13 sanitized
    the DeepSeek producer (`deepseek_runtime._usage_int`) but every other envelope producer
    (the real CLI, a proxy re-typing JSON numbers, a future provider seam) still reached this
    function raw, and `int(value)` raised ValueError/TypeError inside `base_agent._finish_call`
    AFTER a successful model call — losing the run. `bool` is a flag, not a count (its `int()`
    is 1/0 — a fabricated measurement); a non-integral float is never silently truncated.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)  # JSON `18.0` through a float round-trip is still the count 18
    return None


def _float_or_none(value: Any) -> float | None:
    """Same contract as `_int_or_none`, for the money/duration fields (R20 item 1).

    Only finite real numbers are measurements: NaN/inf must stay None — a non-finite cost would
    ride `round(x, 6)` into `cost_estimate_usd` and break the DB write, which is strictly worse
    than the honest "unmeasured".
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def usage_from_envelope(env: dict) -> RunUsage:
    """Extract the documented usage fields. `usage` undercounts with subagents; `modelUsage`
    and `total_cost_usd` are the accounting authority (D4).

    Every measurement is read with an explicit `is None` guard — no `or 0`: an envelope that
    omits the key (or sends `null`) reports `None` ("unknown"), while a key present with value 0
    reports a measured zero.

    L3-12: the DeepSeek envelope carries the SERVED model as a single `model` string and no
    `modelUsage`; a run must be attributable to the model that answered, so a measured non-empty
    string is recorded as a one-entry map (absent/empty stays absent — never invented).

    L3-04: the DeepSeek envelope's `cost_estimate_usd` (token-derived, distinct from
    `total_cost_usd`) is read into its own field and its source is stamped.

    V22/L1: the DeepSeek envelope's `tools_used` is read through `bounded_tools_used` — present
    but empty stays `[]` (a measured zero), absent stays None (never recorded), and a junk shape
    stays None rather than being coerced.
    """
    usage = env.get("usage") or {}
    model_usage = env.get("modelUsage") or env.get("model_usage") or {}
    models = dict(model_usage)
    if not models:
        served_model = env.get("model")
        if isinstance(served_model, str) and served_model.strip():
            models = {served_model.strip(): {}}
    total_cost = _float_or_none(env.get("total_cost_usd"))
    token_cost = _float_or_none(env.get("cost_estimate_usd"))
    if total_cost is not None:
        cost_source = "cli_estimate"
    elif token_cost is not None:
        cost_source = "token_estimate"
    else:
        cost_source = ""
    return RunUsage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        cache_read_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
        total_cost_usd=total_cost,
        token_cost_estimate_usd=token_cost,
        cost_source=cost_source,
        pricing_version=str(env.get("pricing_version") or ""),
        duration_ms=_int_or_none(env.get("duration_ms")),
        num_turns=_int_or_none(env.get("num_turns")),
        tools_used=bounded_tools_used(env.get("tools_used")),
        is_error=bool(env.get("is_error")),
        subtype=str(env.get("subtype") or ""),
        session_id=str(env.get("session_id") or ""),
        models=models,
        permission_denials=len(env.get("permission_denials") or []),
        terminal_reason=str(env.get("terminal_reason") or ""),
    )


def compute_run_delta(env_total_usd: float, session_id: str, session_totals: dict[str, float]) -> float:
    """Delta accounting for resumed sessions.

    The envelope's `total_cost_usd` is cumulative per session (documented). We keep a high-water
    mark per session; the ledger records only the growth. A session whose total DROPS (transcript
    loss / new session id collision) records the full value and resets the mark. Never negative.
    """
    if not session_id:
        return max(0.0, env_total_usd)
    prior = session_totals.get(session_id, 0.0)
    delta = env_total_usd - prior
    if delta < 0:                      # documented crash-recovery case: totals reset
        session_totals[session_id] = env_total_usd
        return max(0.0, env_total_usd)
    session_totals[session_id] = env_total_usd
    return delta


# ---------------------------------------------------------------------------
# Budget guard: hard stop BEFORE dispatch
# ---------------------------------------------------------------------------

class BudgetStop(PolsiaError):
    """Raised before any process is spawned when the spend envelope is exhausted.

    It IS a `PolsiaError` (code `E_BUDGET_EXCEEDED`, or `E_POLICY_DENIED` for the kill switch)
    so the API boundary and the activity log report the same name the gate decided with.

    `how_to_fix` is optional and empty for the dispatch-gate refusals, whose remediation is the
    generic `E_BUDGET_EXCEEDED` hint; the in-run dollar guard (`RunCostGuard`) names the exact knob
    and arithmetic because its refusal is the only one a spending run can hit mid-flight.
    """

    def __init__(self, reason: str, code: str = E_BUDGET_EXCEEDED, how_to_fix: str = "", **context) -> None:
        super().__init__(code, reason, how_to_fix=how_to_fix, context=context)
        self.reason = reason


@dataclass
class BudgetPolicy:
    day_limit_usd: float = 25.0
    per_agent_day_limit_usd: dict[str, float] = field(default_factory=dict)
    per_run_cap_usd: float = 2.0
    kill_switch: bool = False
    reserve_usd: float = 1.0          # never spend the last dollar: overnight/emergency reserve


def budget_policy_from_settings(*, kill_switch: bool = False) -> BudgetPolicy:
    """The policy the settings surface actually declares (`agent_daily_cost_cap_usd`,
    `agent_run_cost_cap_usd`)."""
    from app.config import settings

    return BudgetPolicy(
        day_limit_usd=float(settings.agent_daily_cost_cap_usd),
        per_run_cap_usd=float(settings.agent_run_cost_cap_usd),
        kill_switch=bool(kill_switch),
    )


@dataclass
class BudgetSnapshot:
    day_spent_usd: float
    day_limit_usd: float
    agent_spent_usd: float
    agent_limit_usd: float
    remaining_day_usd: float
    remaining_agent_usd: float

    @property
    def allowance_usd(self) -> float:
        return max(0.0, min(self.remaining_day_usd, self.remaining_agent_usd))


def authorize(
    policy: BudgetPolicy,
    *,
    agent_type: str,
    est_cost_usd: float,
    day_spent_usd: float,
    agent_spent_usd: float,
) -> tuple[float, BudgetSnapshot]:
    """Return (allowance_for_this_run, snapshot) or raise BudgetStop.

    allowance = min(remaining_day - reserve, remaining_agent, per_run_cap)
    The caller passes `--max-budget-usd = allowance * 0.9` (the docs say spend can pass the cap,
    so leave headroom: https://code.claude.com/docs/en/cli-reference).
    """
    if policy.kill_switch:
        raise BudgetStop("kill switch active: dispatch denied", code=E_POLICY_DENIED)
    agent_limit = policy.per_agent_day_limit_usd.get(agent_type, policy.day_limit_usd)
    remaining_day = policy.day_limit_usd - day_spent_usd - policy.reserve_usd
    remaining_agent = agent_limit - agent_spent_usd
    snap = BudgetSnapshot(day_spent_usd, policy.day_limit_usd, agent_spent_usd,
                          agent_limit, max(0.0, remaining_day), max(0.0, remaining_agent))
    if est_cost_usd > snap.allowance_usd:
        raise BudgetStop(
            f"insufficient budget: est=${est_cost_usd:.4f} allowance=${snap.allowance_usd:.4f} "
            f"(agent={agent_type})",
            est_cost_usd=est_cost_usd,
            allowance_usd=round(snap.allowance_usd, 6),
            agent_type=agent_type,
        )
    allowance = min(snap.allowance_usd, policy.per_run_cap_usd)
    return allowance, snap


def cli_budget_flag(allowance_usd: float, headroom: float = 0.9) -> str:
    """The `--max-budget-usd` value to pass: allowance minus headroom."""
    return f"{max(0.01, allowance_usd * headroom):.4f}"


# ---------------------------------------------------------------------------
# IN-RUN dollar guard (REQUESTS-2 / taskmaster §13.2)
# ---------------------------------------------------------------------------
#
# WHAT WAS WRONG: `AGENT_RUN_COST_CAP_USD` was consumed in exactly two places — the pre-dispatch
# ESTIMATE (`authorize`, which compares `est = per_run_cap` to the day's allowance) and the Claude
# CLI's own `--max-budget-usd` flag.  The DeepSeek runtime — the provider the live key selects —
# received NO dollar bound at all: its only per-run stop was the OUTPUT-TOKEN ceiling
# (`deepseek_max_tokens`), and its cost was measured AFTER the fact.  So "the per-run cap refuses by
# name when exceeded" was true at dispatch and false DURING a DeepSeek run (taskmaster §6.3,
# measured by code read and a live no-spend probe).
#
# WHAT THIS ADDS: a live guard the runtime consults around EVERY provider call, so the configured
# cap is enforced in dollars, not tokens:
#
#   * BEFORE a call: refuse when `priced_usd() + upper_bound(next call) > cap`.  The bound is a
#     TRUE upper bound over the rate table (`pricing.PRICING_VERSION`): the prompt's UTF-8 BYTE
#     count billed at the cache-MISS rate (tokens <= bytes for every tokenizer: a token decodes to
#     at least one byte) plus the request's own `max_tokens` output ceiling at the output rate.
#     Peak rates are used whenever `pricing.is_peak_pricing()` says the call may be billed at them
#     (the safe direction: the bound can only OVER-state a call).
#   * AFTER a call: record the MEASURED cost (token arithmetic from the provider's own usage) and
#     refuse before the next turn when the run has already passed its cap.  This is the backstop
#     for a call whose input side cannot be bounded up front (a vision request — the provider's
#     image tokenization is not pinned in this repo, so no honest bound exists; see
#     `deepseek_runtime._prompt_tokens_bound`).
#
# The guarantee this gives the queue: no provider call is made when a PROVABLE worst case for it
# would take the run over the cap, so a text-only run cannot spend past its cap by more than
# rounding.  A run whose input side is unprovable (vision) can overrun by at most ONE call's input
# cost — which the ledger then measures — and the next turn is refused.  Both directions are
# recorded in the refusal's context, never inferred from prose.
#
# A refusal is a `BudgetStop` (`E_BUDGET_EXCEEDED`, a `PolsiaError`): `claude_cli.run_with_retries`
# re-raises every code except `E_CLI_TIMEOUT`, so a capped run is NOT retried — the identical prompt
# would spend again, which is the opposite of a cap.

#: How much of a cap the pre-call check may round past (float summation slack only, not headroom).
COST_GUARD_EPSILON_USD = 1e-9


def per_run_cost_cap_usd() -> float:
    """The configured per-run cap (`AGENT_RUN_COST_CAP_USD`) as the budget policy declares it.

    Read through `budget_policy_from_settings` so this guard, the dispatch gate and the CLI flag
    can never disagree about which number "the per-run cap" means.
    """
    return float(budget_policy_from_settings().per_run_cap_usd)


def _most_expensive_rates() -> pricing.ModelRates:
    """The most expensive pinned rate of each component — an UPPER BOUND over the whole table.

    Used only when a call cannot be priced for its own model (`pricing.rates_for` -> None).  This
    is not a guessed rate for that model: it is the worst case the PINNED table carries, the same
    safe-direction rule `pricing.is_peak_pricing` applies to the peak window.  The run's COST stays
    unmeasured (`pricing.estimate_cost_usd` still returns None), so no measurement is fabricated —
    only the bound the guard enforces against.
    """
    table = list(pricing.MODEL_RATES.values())
    return pricing.ModelRates(
        cache_hit_per_mtok_offpeak=max(r.cache_hit_per_mtok_offpeak for r in table),
        cache_hit_per_mtok_peak=max(r.cache_hit_per_mtok_peak for r in table),
        cache_miss_per_mtok_offpeak=max(r.cache_miss_per_mtok_offpeak for r in table),
        cache_miss_per_mtok_peak=max(r.cache_miss_per_mtok_peak for r in table),
        output_per_mtok_offpeak=max(r.output_per_mtok_offpeak for r in table),
        output_per_mtok_peak=max(r.output_per_mtok_peak for r in table),
    )


def call_cost_bound_usd(
    model: str,
    *,
    prompt_tokens_bound: int | None,
    output_tokens_bound: int,
    now: datetime | None = None,
) -> float | None:
    """An UPPER BOUND on what ONE provider call can cost — or None when no bound can be priced.

    `prompt_tokens_bound` is a bound on the PROMPT's token count (the caller's UTF-8 byte count for
    text; `None` when a part of the request cannot be bounded here, e.g. inline images, in which
    case only the output ceiling is priced — a PARTIAL bound, never a complete one).
    `output_tokens_bound` is the request's own `max_tokens`.

    The model's own pinned rates are used when it has them; an UNKNOWN model is bounded at the most
    expensive pinned rate of each component (see `_most_expensive_rates`) instead of being exempted
    from the cap — a run nobody can price must still not be allowed to spend without a ceiling.
    None is returned only for an unusable output bound (0/negative/bool/non-integer), which would
    otherwise price a call at $0 and make the guard meaningless.
    """
    rates = pricing.rates_for(model) or _most_expensive_rates()
    try:
        out_bound = int(output_tokens_bound)
    except (TypeError, ValueError):
        return None
    if isinstance(output_tokens_bound, bool) or out_bound <= 0:
        return None
    peak = pricing.is_peak_pricing(now)
    miss_rate = rates.cache_miss_per_mtok_peak if peak else rates.cache_miss_per_mtok_offpeak
    out_rate = rates.output_per_mtok_peak if peak else rates.output_per_mtok_offpeak
    prompt_bound = 0
    if prompt_tokens_bound is not None:
        try:
            prompt_bound = max(0, int(prompt_tokens_bound))
        except (TypeError, ValueError):
            return None
    # The prompt is billed at the MISS rate (the expensive side): the bound can only over-state.
    return prompt_bound / 1_000_000 * miss_rate + out_bound / 1_000_000 * out_rate


@dataclass
class RunCostGuard:
    """One agent run's live dollar guard — see the section comment above.

    State is per RUN, never per process: `deepseek_runtime.invoke` builds one for each logical run
    (the same lifetime as its tool-loop bound) and reads `cap_usd` ONCE, so a setting changed
    mid-run cannot move the finish line under a spending run.
    """

    cap_usd: float
    model: str
    #: Σ of the MEASURED per-call costs (token arithmetic over the provider's own usage).
    measured_usd: float = 0.0
    #: Σ of the per-call UPPER BOUNDS the guard was given before each call.
    upper_bound_usd: float = 0.0
    #: Calls whose cost the provider did not report enough to price (`unmeasured_calls > 0`).
    unmeasured_calls: int = 0
    calls: int = 0

    @classmethod
    def from_settings(cls, model: str) -> RunCostGuard:
        """The guard for a run of `model` under the configured `AGENT_RUN_COST_CAP_USD`."""
        return cls(cap_usd=per_run_cost_cap_usd(), model=str(model or ""))

    @property
    def priced_usd(self) -> float:
        """What this run has provably cost so far.

        When EVERY call was priced this is the measured total (the true best estimate).  When any
        call left the provider's usage unpriceable, its cost is UNKNOWN, so the guarded quantity
        becomes the sum of the per-call upper bounds — an upper limit, never the measured part
        presented as the whole (the same NULL-vs-zero rule the rest of this module applies).
        """
        if self.unmeasured_calls:
            return max(self.measured_usd, self.upper_bound_usd)
        return self.measured_usd

    @property
    def cost_basis(self) -> str:
        """What `priced_usd` is: `"measured"` (every call priced) or `"upper_bound"`."""
        return "upper_bound" if self.unmeasured_calls else "measured"

    def check_before_call(self, next_call_bound_usd: float | None, *, provider_calls: int) -> None:
        """Refuse BEFORE a provider call whose provable worst case would pass the cap.

        Raises `BudgetStop` (`E_BUDGET_EXCEEDED`) and nothing has been spent on that call.  An
        unusable cap (0, negative, NaN/inf) fails CLOSED here rather than meaning "unlimited": a
        non-positive cap in the CLI path becomes a one-cent `--max-budget-usd`, i.e. the same
        refusal — and a cap nobody can honour must not silently become no cap at all.
        """
        if not math.isfinite(self.cap_usd) or self.cap_usd <= 0:
            raise self._refusal(
                f"per-run dollar cap is not usable: ${self.cap_usd!r} is not a positive, finite "
                "number, so no provider call may be made under it",
                provider_calls=provider_calls,
                how_to_fix=(
                    "set AGENT_RUN_COST_CAP_USD (Settings: agent_run_cost_cap_usd) to a positive "
                    "dollar amount; a zero cap would otherwise mean an unbounded run"
                ),
            )
        bound = 0.0 if next_call_bound_usd is None else max(0.0, float(next_call_bound_usd))
        projected = self.priced_usd + bound
        if projected > self.cap_usd + COST_GUARD_EPSILON_USD:
            raise self._refusal(
                f"per-run dollar cap: the next provider call could take this run over its "
                f"${self.cap_usd:.4f} cap — refusing before the call "
                f"(committed ${self.priced_usd:.6f} [{self.cost_basis}] + worst case "
                f"${bound:.6f} > ${self.cap_usd:.4f})",
                provider_calls=provider_calls,
                next_call_bound_usd=round(bound, 8),
                projected_usd=round(projected, 8),
                how_to_fix=(
                    "raise AGENT_RUN_COST_CAP_USD for a run this large, or split the task; the "
                    "cap is enforced in dollars against the pinned price table"
                ),
            )

    def record_call(
        self,
        *,
        cost_usd: float | None,
        call_bound_usd: float | None,
        provider_calls: int,
    ) -> None:
        """Record ONE finished provider call and refuse if the run is already past its cap.

        `cost_usd` is the measured cost of that call (`pricing.estimate_cost_usd` over the usage
        the provider reported) or None when the provider's usage was not enough to price it.
        Called BEFORE the loop decides whether to continue, so the refusal also covers a run whose
        LAST call already overspent: a run that spent past its cap must not report success.
        """
        self.calls += 1
        if call_bound_usd is not None and math.isfinite(float(call_bound_usd)):
            self.upper_bound_usd += max(0.0, float(call_bound_usd))
        if cost_usd is None or not math.isfinite(float(cost_usd)):
            self.unmeasured_calls += 1
        else:
            self.measured_usd += max(0.0, float(cost_usd))
        if self.priced_usd > self.cap_usd + COST_GUARD_EPSILON_USD:
            raise self._refusal(
                f"per-run dollar cap exceeded after {provider_calls} provider call(s): this run "
                f"has spent ${self.priced_usd:.6f} [{self.cost_basis}] of its "
                f"${self.cap_usd:.4f} cap — refusing before any further turn",
                provider_calls=provider_calls,
                how_to_fix=(
                    "raise AGENT_RUN_COST_CAP_USD or split the task; the spend already made is "
                    "recorded on this refusal's `usage` so the ledger accounts for it"
                ),
            )

    def _refusal(self, reason: str, *, provider_calls: int, how_to_fix: str, **extra: Any) -> BudgetStop:
        """The named `E_BUDGET_EXCEEDED` refusal, with the arithmetic that produced it."""
        return BudgetStop(
            reason,
            how_to_fix=how_to_fix,
            cap_usd=round(self.cap_usd, 8),
            run_cost_usd=round(self.priced_usd, 8),
            measured_cost_usd=round(self.measured_usd, 8),
            upper_bound_usd=round(self.upper_bound_usd, 8),
            cost_basis=self.cost_basis,
            model=self.model,
            provider_calls=provider_calls,
            calls_recorded=self.calls,
            unmeasured_calls=self.unmeasured_calls,
            pricing_version=pricing.PRICING_VERSION,
            runtime="deepseek",
            **extra,
        )


def spend_summary(day_spent_usd: float | None, cap_usd: float) -> dict[str, float | None]:
    """The ONE honest spend summary (T8 / A2 F-08).

    `day_spent_usd is None` means the day's spend was NOT MEASURED: `spent`/`remaining` stay None
    so the caller renders "unmeasured" — never `$0.00`. The deleted `base_agent.layers_for`
    budget block did the opposite (`or 0.0`), inventing a zero spend (and a full remaining
    budget) for a day nobody had measured. A measured `0.0` is a real zero and stays `0.0`, and
    `remaining` is clamped at 0 so an over-cap day never reports negative headroom.
    """
    return {
        "limit": cap_usd,
        "spent": day_spent_usd,
        "remaining": None if day_spent_usd is None else max(0.0, cap_usd - day_spent_usd),
    }


def burn_rate(*, spilled_usd: float, day_limit_usd: float, elapsed_fraction: float) -> dict:
    """Burn-rate view for the dashboard, computed from ledger totals the caller supplies."""
    rate = spilled_usd / max(0.05, elapsed_fraction)
    projection: float | None = None if elapsed_fraction <= 0 else rate
    status = "ok"
    if projection is not None and projection > day_limit_usd:
        status = "over_budget_projection"
    elif elapsed_fraction > 0 and day_limit_usd > 0 and spilled_usd / day_limit_usd > elapsed_fraction * 1.3:
        status = "burning_fast"
    return {
        "spent_usd": round(spilled_usd, 6),
        "day_limit_usd": day_limit_usd,
        "elapsed_fraction": round(elapsed_fraction, 4),
        "projected_day_total_usd": None if projection is None else round(projection, 6),
        "status": status,
    }


def unit_economics(spend_by_agent: dict[str, float], completed_by_agent: dict[str, int]) -> dict:
    """Cost per completed task, per agent and for the fleet.

    A metric nobody measured is not a metric: if a spend row exists but the completion count is
    0, `cost_per_completed_task_usd` is None (renders "unmeasured"), NOT 0.00.
    """
    per_agent: dict[str, dict] = {}
    total_spend = 0.0
    total_completed = 0
    for agent in sorted(set(spend_by_agent) | set(completed_by_agent)):
        s = float(spend_by_agent.get(agent, 0.0))
        c = int(completed_by_agent.get(agent, 0))
        total_spend += s
        total_completed += c
        per_agent[agent] = {
            "spend_usd": round(s, 6),
            "completed": c,
            "cost_per_completed_task_usd": None if c == 0 else round(s / c, 6),
        }
    return {
        "total_spend_usd": round(total_spend, 6),
        "total_completed": total_completed,
        "cost_per_completed_task_usd": None if total_completed == 0 else round(total_spend / total_completed, 6),
        "per_agent": per_agent,
        "note": "None means no completions measured; it must render as 'unmeasured', never as $0.00",
    }


__all__ = [
    "COST_GUARD_EPSILON_USD",
    "NO_PROVIDER_CALLS_COST_SOURCE",
    "PROVIDER_CALLS_KEY",
    "PROVIDER_CALLS_SOURCE_KEY",
    "TOOLS_USED_MAX",
    "TOOLS_USED_TRUNCATION",
    "BudgetPolicy",
    "BudgetSnapshot",
    "BudgetStop",
    "RunCostGuard",
    "RunUsage",
    "authorize",
    "bounded_tools_used",
    "budget_policy_from_settings",
    "burn_rate",
    "call_cost_bound_usd",
    "cli_budget_flag",
    "compute_run_delta",
    "per_run_cost_cap_usd",
    "provider_call_declaration",
    "spend_summary",
    "unit_economics",
    "usage_from_envelope",
]
