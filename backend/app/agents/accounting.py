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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.errors import E_BUDGET_EXCEEDED, E_POLICY_DENIED, PolsiaError


# ---------------------------------------------------------------------------
# Envelope -> usage record
# ---------------------------------------------------------------------------

@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0      # the envelope's cumulative-for-session estimate
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    subtype: str = ""
    session_id: str = ""
    models: dict[str, dict] = field(default_factory=dict)
    permission_denials: int = 0
    terminal_reason: str = ""

    @property
    def tokens_used(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        """The documented result keys the queue persists into `agent_runs`."""
        return {
            "cli_session_id": self.session_id,
            "tokens_input": self.input_tokens,
            "tokens_output": self.output_tokens,
            "tokens_used": self.tokens_used,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_estimate_usd": round(self.total_cost_usd, 6),
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
                "duration_ms": self.duration_ms,
                "num_turns": self.num_turns,
                "session_id": self.session_id,
                "subtype": self.subtype,
            },
        }


def usage_from_envelope(env: dict) -> RunUsage:
    """Extract the documented usage fields. `usage` undercounts with subagents; `modelUsage`
    and `total_cost_usd` are the accounting authority (D4)."""
    usage = env.get("usage") or {}
    model_usage = env.get("modelUsage") or env.get("model_usage") or {}
    return RunUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        total_cost_usd=float(env.get("total_cost_usd") or 0.0),
        duration_ms=int(env.get("duration_ms") or 0),
        num_turns=int(env.get("num_turns") or 0),
        is_error=bool(env.get("is_error")),
        subtype=str(env.get("subtype") or ""),
        session_id=str(env.get("session_id") or ""),
        models=dict(model_usage),
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


def burn_rate(*, spilled_usd: float, day_limit_usd: float, elapsed_fraction: float) -> dict:
    """Burn-rate view for the dashboard, computed from ledger totals the caller supplies."""
    rate = spilled_usd / max(0.05, elapsed_fraction)
    projection: Optional[float] = None if elapsed_fraction <= 0 else rate
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
    "unit_economics",
    "usage_from_envelope",
]
