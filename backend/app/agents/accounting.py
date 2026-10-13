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
from typing import Any

from app.core.errors import E_BUDGET_EXCEEDED, E_POLICY_DENIED, PolsiaError

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
        """
        return {
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
    """

    def __init__(self, reason: str, code: str = E_BUDGET_EXCEEDED, **context) -> None:
        super().__init__(code, reason, context=context)
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
    "BudgetPolicy",
    "BudgetSnapshot",
    "BudgetStop",
    "RunUsage",
    "authorize",
    "budget_policy_from_settings",
    "burn_rate",
    "cli_budget_flag",
    "compute_run_delta",
    "spend_summary",
    "unit_economics",
    "usage_from_envelope",
]
