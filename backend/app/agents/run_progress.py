"""In-flight usage reporting for a LIVE agent run (F-26) — the sink the worker installs.

THE DEFECT THIS EXISTS FOR.  A run's accounting used to be written exactly once, when the run
settled (`agent_tasks._execute_agent_task` → `finish_agent_run` + `_apply_run_usage`).  A worker
killed mid-run therefore left `agent_runs.cost_usd` / `tokens_*` NULL although the provider had
already been called and paid for — measured live 2026-10-28 on runs 311 and 314 (three
`POST /chat/completions` calls on 314).  The money was real; the ledger could not see it, and the
day cap reads that same ledger, so every interrupted run made the cap arithmetic under-count.

THE SEAM.  `deepseek_runtime.invoke` measures each provider call as it finishes (`turns` + the
`RunCostGuard`).  After recording a call it reports the CUMULATIVE snapshot through this module.
The snapshot is a plain dict (no ORM, no session) because the runtime must never know how the
ledger is stored; the WORKER owns the database (the same "the worker owns the DB, the agent does
not" rule the channel-count/preflight hooks follow).

WHY A CONTEXTVAR.  The agent runs in a worker thread (`asyncio.to_thread`), which copies the
task's context, so a sink installed by the worker before the thread starts is visible inside the
run and dies with the run — no module-global state that a second run in the same process could
inherit or overwrite (the RUN_TAINT isolation argument, applied to usage).

FAILURE DIRECTION.  Reporting is BEST EFFORT and strictly one-way: a sink that raises, or a
database that is briefly unavailable, must never fail or slow the run's real work into an error.
Loss of one snapshot is bounded (the next call reports a bigger cumulative total, and the settle
write records the final one), while an exception escaping into the provider loop would corrupt a
run that was otherwise fine.  Every failure is logged with the run's id, never swallowed silently.
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: One cumulative usage snapshot for the live run.  Shape (all values measured, never invented):
#: `provider_calls`, `usage` (`input_tokens`/`output_tokens`/`cache_read_input_tokens`),
#: `cost_usd`, `cost_basis` (`measured`|`upper_bound`), `measured_cost_usd`,
#: `upper_bound_usd`, `model`, `pricing_version`, `num_turns`.
UsageSnapshot = dict[str, Any]

#: What the worker installs: a callable that persists ONE snapshot and returns nothing.
UsageSink = Callable[[UsageSnapshot], None]

_SINK: contextvars.ContextVar[UsageSink | None] = contextvars.ContextVar(
    "polsia_run_usage_sink", default=None
)


def set_run_usage_sink(sink: UsageSink | None) -> contextvars.Token:
    """Install the sink for the run about to execute in this context.  Returns the reset token."""
    return _SINK.set(sink)


def reset_run_usage_sink(token: contextvars.Token) -> None:
    """Restore the previous sink (the worker's `finally` — one run's sink must not leak)."""
    try:
        _SINK.reset(token)
    except ValueError:  # pragma: no cover - a token from another context cannot be reset here
        _SINK.set(None)


def current_run_usage_sink() -> UsageSink | None:
    """The sink installed for this context, or None (tests and direct callers)."""
    return _SINK.get()


def report_usage_progress(snapshot: UsageSnapshot) -> bool:
    """Hand ONE cumulative snapshot to the installed sink.

    Returns True when a sink accepted it, False when there is no sink (the runtime's direct
    callers, the CLI path, every unit test that does not install one) or when the sink FAILED.
    Never raises: the provider loop this is called from must not die because the ledger write
    failed (see the module docstring's failure direction).
    """
    sink = _SINK.get()
    if sink is None:
        return False
    if not isinstance(snapshot, dict):  # pragma: no cover - producer is internal
        logger.warning("run usage progress: ignored a non-dict snapshot (%r)", type(snapshot))
        return False
    try:
        sink(snapshot)
    except Exception:  # noqa: BLE001 — best effort by contract; logged, never fatal
        logger.warning(
            "run usage progress: the sink failed; the run continues (cost so far may be "
            "under-recorded until the next call or the settle write)",
            exc_info=True,
        )
        return False
    return True


__all__ = [
    "UsageSink",
    "UsageSnapshot",
    "current_run_usage_sink",
    "report_usage_progress",
    "reset_run_usage_sink",
    "set_run_usage_sink",
]
