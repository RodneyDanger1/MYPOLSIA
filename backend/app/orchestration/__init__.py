"""Polsia orchestration — multi-agent planning above the agent layer.

`planner` turns the orchestrator's proposed task list into a validated, scheduled, budget-gated,
idempotent day plan. Nothing in this package runs an agent itself: the queue/dispatcher consumes
a `ScheduleResult` (the same way the original's morning cycle consumed a flat list), which keeps
the planner pure and exhaustively testable without a database.
"""
from __future__ import annotations

from app.orchestration.planner import (
    FAILURE_STATES,
    LAYER_ORDER_NOTE,
    NodeState,
    Plan,
    PlanError,
    PlanNode,
    PlanRunSummary,
    ScheduleResult,
    SideEffect,
    build_plan,
    classify_new_work,
    critical_path,
    existing_from_results,
    flat_list_rerun,
    plan_rerun,
    plan_summary,
    schedule,
    simulate_serial_wall_time,
    simulate_wave_wall_time,
    stable_id,
)

__all__ = [
    "FAILURE_STATES",
    "LAYER_ORDER_NOTE",
    "NodeState",
    "Plan",
    "PlanError",
    "PlanNode",
    "PlanRunSummary",
    "ScheduleResult",
    "SideEffect",
    "build_plan",
    "classify_new_work",
    "critical_path",
    "existing_from_results",
    "flat_list_rerun",
    "plan_rerun",
    "plan_summary",
    "schedule",
    "simulate_serial_wall_time",
    "simulate_wave_wall_time",
    "stable_id",
]
