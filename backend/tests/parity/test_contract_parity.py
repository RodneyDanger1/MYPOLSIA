"""Structural parity suite — the drift gate.

The shipped unit suite proves behaviour; this file proves the *contract* cannot quietly drift:
routes, the agent registry, the declared-but-must-also-be-loaded schedule, the ORM-vs-migration
table set, the frozen settings surface, and the named-refusal shape.

Why structural: lane 02 showed a naive implementation can pass all 60 shipped tests and still be
wrong in production, and lane 03 measured two "declared but never wired" registry defects
(5 beat entries declared / 0 loaded; 7 tasks declared / 0 registered). Those classes of defect are
invisible to behaviour tests and obvious to these.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]  # backend/
APP = BACKEND / "app"
MIGRATION = BACKEND / "alembic" / "versions" / "0001_initial_schema.py"

# ── the documented HTTP surface (README + the frontend's api client + the shipped tests) ──────
DOCUMENTED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks"),
    ("GET", "/api/v1/tasks/{task_id}"),
    ("GET", "/api/v1/agents/status"),
    ("POST", "/api/v1/agents/{agent_type}/trigger"),
    ("GET", "/api/v1/config"),
    ("PUT", "/api/v1/config"),
    ("GET", "/api/v1/dashboard/summary"),
    ("GET", "/api/v1/dashboard/activity"),
    ("GET", "/api/v1/dashboard/reports/daily"),
    ("GET", "/api/v1/memory"),
    ("POST", "/api/v1/memory"),
    ("GET", "/api/v1/finance/summary"),
    ("GET", "/api/v1/finance/revenue"),
    ("GET", "/api/v1/finance/expenses"),
    ("GET", "/api/v1/finance/events"),
    ("POST", "/api/v1/finance/stripe/webhook"),
    ("GET", "/api/v1/social/posts"),
    ("GET", "/api/v1/outreach/prospects"),
    ("GET", "/api/v1/ads/campaigns"),
]

DOCUMENTED_AGENTS = {
    "orchestrator",
    "business_planning",
    "competitor_research",
    "social_media",
    "email_outreach",
    "customer_support",
    "ads_management",
    "code_generation",
    "finance",
}

# README's agent table: Orchestrator 06:00/20:00, Social every 2h, Email every 3h, Ads+Stripe
# every 6h — plus the maintenance job that the upload declared and never scheduled, and the
# task-lease sweep T1 added (its README row reads "Every 5 min").
DOCUMENTED_SCHEDULE_ENTRIES = {
    "morning-cycle",
    "evening-cycle",
    "social-mentions-sweep",
    "email-inbox-sweep",
    "ads-stripe-sync",
    "task-lease-sweep",
}

# The upload DECLARED this task and never scheduled it (the maintenance module ships in the upload
# with no beat entry, so the sweep never ran). Scheduling it is required by the build brief, but its
# KEY NAME is the implementer's choice — so it is matched by task PATH, never by a name this test
# invented. The first revision of this file pinned the invented key "activity-retention" and failed
# the shipped build for choosing "activity-retention-cleanup". That was the test being wrong: a test
# must not pin a name the source never carried. The lease sweep T1 added is pinned the same
# way — by task path.
REQUIRED_SCHEDULED_TASKS = {
    "celery_app.tasks.maintenance.cleanup_old_activity",
    "celery_app.tasks.maintenance.requeue_stale_task_claims",
}

# Every field the parent froze for build wave 1 (see polsia-work/BUILD-BRIEF.md §3).
FROZEN_SETTINGS = {
    "api_key", "database_url", "redis_url", "celery_broker_url", "celery_result_backend",
    "morning_cycle_hour", "evening_cycle_hour", "company_timezone", "stripe_webhook_secret",
    "claude_cli_path", "claude_cli_mock", "claude_timeout_seconds", "claude_max_concurrency",
    "claude_max_argv_chars", "claude_prompt_via_stdin", "claude_repair_attempts",
    "agent_max_retries", "agent_retry_backoff_seconds", "agent_run_cost_cap_usd",
    "agent_daily_cost_cap_usd", "task_claim_lease_seconds", "task_max_attempts",
    "task_retry_backoff_seconds", "task_batch_size", "activity_retention_days",
    "activity_bulk_flush", "celery_task_time_limit", "celery_soft_time_limit",
    "celery_max_tasks_per_child", "db_pool_size", "db_max_overflow", "db_pool_recycle_seconds",
    "db_statement_timeout_ms", "planner_max_nodes", "planner_context_token_budget", "log_json",
}


def _routes() -> set[tuple[str, str]]:
    from app.main import app

    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path))
    return found


# ── 1. HTTP surface ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path", DOCUMENTED_ROUTES)
def test_documented_route_exists(method: str, path: str) -> None:
    assert (method, path) in _routes(), f"{method} {path} is documented but not routed"


def test_websocket_route_exists() -> None:
    paths = {r.path for r in __import__("app.main", fromlist=["app"]).app.routes}
    assert "/ws/activity" in paths, "/ws/activity is consumed by the dashboard but not routed"
    # nginx also proxies /ws/ — a rename on either side silently breaks the live feed.
    nginx = (BACKEND.parent / "nginx" / "nginx.conf").read_text(encoding="utf-8", errors="replace")
    assert "/ws/" in nginx, "nginx no longer proxies the websocket path"


# ── 2. agent registry ─────────────────────────────────────────────────────────────────────────
def test_agent_map_has_exactly_the_documented_nine() -> None:
    from app.agents.crew_factory import AGENT_MAP

    assert set(AGENT_MAP) == DOCUMENTED_AGENTS, (
        "GET /agents/status asserts exactly 9 agents; the registry drifted"
    )


@pytest.mark.parametrize("agent_type", sorted(DOCUMENTED_AGENTS))
def test_each_agent_is_importable_and_declares_its_type(agent_type: str) -> None:
    import importlib

    from app.agents.base_agent import BasePolsiaAgent
    from app.agents.crew_factory import AGENT_MAP

    module_path, class_name = AGENT_MAP[agent_type]
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    assert inspect.isclass(cls) and issubclass(cls, BasePolsiaAgent)
    assert cls.agent_type == agent_type, "agent_type must match the registry key"


def test_unknown_agent_raises_the_pinned_value_error() -> None:
    from app.agents.crew_factory import run_agent_for_task

    with pytest.raises(ValueError, match="Unknown agent"):
        run_agent_for_task("nonexistent_agent", {}, {})


# ── 3. declared-vs-loadable registries (the defect class lane 03 measured) ────────────────────
def test_beat_schedule_declares_every_documented_entry() -> None:
    from celery_app.beat_schedule import beat_schedule

    assert set(beat_schedule) >= DOCUMENTED_SCHEDULE_ENTRIES, (
        f"missing schedule entries: {DOCUMENTED_SCHEDULE_ENTRIES - set(beat_schedule)}"
    )
    scheduled_tasks = {entry.get("task") for entry in beat_schedule.values()}
    assert scheduled_tasks >= REQUIRED_SCHEDULED_TASKS, (
        "declared but never scheduled: "
        f"{REQUIRED_SCHEDULED_TASKS - scheduled_tasks} — the task exists and would never run"
    )
    for name, entry in beat_schedule.items():
        assert entry.get("task"), f"{name} has no task"
        assert entry.get("schedule") is not None, f"{name} has no schedule"
        assert "expires" in entry.get("options", {}), (
            f"{name} has no expiry: on a backlog redis would deliver a stale sweep"
        )


def test_beat_schedule_is_actually_loaded_by_the_app() -> None:
    """The measured original defect: 5 declared, 0 effective."""
    import sys

    sys.path.insert(0, str(BACKEND))
    try:
        from celery_app.worker import app as celery_app

        effective = dict(celery_app.conf.beat_schedule or {})
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        pytest.skip(f"celery app not importable in this environment: {type(exc).__name__}: {exc}")
    else:
        assert set(effective) >= DOCUMENTED_SCHEDULE_ENTRIES, (
            "celery beat would start with an incomplete schedule — the declared entries are not wired"
        )
        effective_tasks = {entry.get("task") for entry in effective.values()}
        assert effective_tasks >= REQUIRED_SCHEDULED_TASKS, (
            f"beat would start without: {REQUIRED_SCHEDULED_TASKS - effective_tasks}"
        )


def test_every_declared_celery_task_is_reachable() -> None:
    """Every @app.task name in celery_app/tasks must be registered on the app object."""
    declared = set()
    for path in (BACKEND / "celery_app" / "tasks").glob("*.py"):
        declared |= set(re.findall(r'@app\.task\(\s*name="([^"]+)"', path.read_text(encoding="utf-8")))

    assert len(declared) >= 7, f"expected the 7 documented tasks, found {len(declared)}"

    import sys

    sys.path.insert(0, str(BACKEND))
    import importlib

    for name in declared:
        module_path, _, func = name.rpartition(".")
        importlib.import_module(module_path)
    try:
        from celery_app.worker import app as celery_app

        registered = set(celery_app.tasks)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"celery app not importable in this environment: {type(exc).__name__}: {exc}")
    missing = declared - registered
    assert not missing, f"declared but never registered: {sorted(missing)}"


# ── 4. ORM ↔ migration table parity ───────────────────────────────────────────────────────────
def test_orm_tables_match_the_migration_exactly() -> None:
    import app.models  # noqa: F401  (registers every model)
    from app.core.database import Base

    sql = MIGRATION.read_text(encoding="utf-8")
    migration_tables = set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', sql))
    orm_tables = set(Base.metadata.tables)

    assert migration_tables, "could not parse the migration — the check would be vacuous"
    assert migration_tables == orm_tables, (
        f"ORM-only: {sorted(orm_tables - migration_tables)} | "
        f"migration-only: {sorted(migration_tables - orm_tables)}"
    )


def test_the_reserved_metadata_column_is_not_exposed_under_the_reserved_name() -> None:
    """`metadata` is reserved by Declarative for the MetaData object.

    A mapped class therefore ALWAYS has a `.metadata` attribute — it *is* the MetaData. The
    invariant that matters is that the **column** is not reachable through it: `Task.metadata` must
    still be a MetaData instance (not an InstrumentedAttribute) and the column must be mapped under
    a safe attribute name. The first version of this test asserted `not hasattr(Task, "metadata")`,
    which can never hold; the suite caught the bad assertion, not a bad model.
    """
    from sqlalchemy import MetaData

    from app.models.task import Task

    assert "metadata" in Task.__table__.columns, "the DB column 'metadata' must still exist"
    assert isinstance(Task.metadata, MetaData), (
        "Task.metadata must remain Declarative's MetaData, not an InstrumentedAttribute"
    )
    assert hasattr(Task, "metadata_json"), "the column must be mapped under a safe attribute name"


def test_no_undocumented_api_routes_were_invented() -> None:
    """The reverse direction of the route check: every /api/v1 route must be a documented one.

    This is the guard against the failure mode this very file committed first: three routes that no
    artifact demanded (`/social/engagements`, `/outreach/campaigns`, `/ads/metrics`) appeared in the
    expected list. Inventing a capability in a parity test is the same sin as inventing one in code.
    """
    documented = {(m, p) for m, p in DOCUMENTED_ROUTES}
    extras = sorted(
        (m, p) for m, p in _routes() if p.startswith("/api/v1") and (m, p) not in documented
    )
    assert not extras, f"routes exist that no artifact documents: {extras}"


def test_claim_and_accounting_columns_exist() -> None:
    from app.models.task import AgentRun, Task

    for col in ("claimed_at", "claimed_by", "heartbeat_at", "attempts", "dedupe_key"):
        assert col in Task.__table__.columns, f"tasks.{col} is required for claim/lease/retry"
    for col in ("cli_session_id", "tokens_input", "tokens_output", "usage_json"):
        assert col in AgentRun.__table__.columns, f"agent_runs.{col} is required for cost accounting"


# ── 5. frozen settings surface ────────────────────────────────────────────────────────────────
def test_frozen_settings_are_present() -> None:
    from app.config import settings

    missing = {name for name in FROZEN_SETTINGS if not hasattr(settings, name)}
    assert not missing, f"frozen settings removed: {sorted(missing)}"


# ── 6. named refusal shape ────────────────────────────────────────────────────────────────────
def test_named_refusal_shape_and_status_mapping() -> None:
    from app.core.errors import (
        E_AUTH,
        E_TASK_CLAIMED,
        E_TASK_NOT_RUNNABLE,
        PolsiaError,
        http_status_for,
        to_http_detail,
    )

    detail = to_http_detail(PolsiaError(E_TASK_NOT_RUNNABLE, "nope", how_to_fix="reset it"))
    assert detail["ok"] is False
    assert detail["error"]["code"] == E_TASK_NOT_RUNNABLE
    assert detail["error"]["how_to_fix"] == "reset it"
    assert http_status_for(E_AUTH) == 401
    assert http_status_for(E_TASK_CLAIMED) == 409
    assert http_status_for("E_SOMETHING_NEW") == 500, "an unmapped code must not become a 4xx"


def test_timeutil_day_window_is_dialect_bound() -> None:
    from datetime import date

    from app.core.timeutil import day_window

    start_sqlite, end_sqlite = day_window("sqlite", date(2026, 10, 11), "Asia/Tokyo")
    assert start_sqlite.tzinfo is None, "SQLite stores naive UTC text; an aware bound never matches"
    start_pg, _ = day_window("postgresql", date(2026, 10, 11), "Asia/Tokyo")
    assert start_pg.tzinfo is not None, "PostgreSQL must receive an aware instant"
    assert start_sqlite == start_pg.replace(tzinfo=None)
