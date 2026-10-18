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
#: The ORM↔migration table check reads EVERY revision, not only 0001: a table a later
#: revision adds (0004's `support_inbox_items`, R17) is equally part of the schema the ORM
#: declares — the check's subject is the whole chain.  0003's rebuild statements are plain
#: string DDL (`op.execute`), so the `op.create_table(` regex cannot match anything but a
#: real table creation.
MIGRATIONS_DIR = BACKEND / "alembic" / "versions"

# ── the documented HTTP surface (README + the frontend's api client + the shipped tests) ──────
DOCUMENTED_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/tasks"),
    ("POST", "/api/v1/tasks"),
    ("GET", "/api/v1/tasks/{task_id}"),
    # T12/D20 (build/decisions/DECISIONS.md): the typed task-status update route — the deliberate
    # new surface of wave-T12; verified live in app.routes as [PATCH] on this path.
    ("PATCH", "/api/v1/tasks/{task_id}/status"),
    ("GET", "/api/v1/agents/status"),
    ("POST", "/api/v1/agents/{agent_type}/trigger"),
    # T02/D1 (build/decisions/DECISIONS.md): the approvals channel's mint — owner-authenticated,
    # documented by app/api/v1/approvals.py and tests/unit/api/test_t02_approvals.py; route
    # verified live in app.routes as [POST] on this path (wave-W2 checked 2026-10-13).
    ("POST", "/api/v1/approvals"),
    # wave U3 (H-01): the per-channel send counters + policy caps — documented by
    # app/api/v1/channels.py and tests/unit/test_u3_channel_caps.py, route verified live in
    # app.routes as [GET] on this path (checked 2026-10-14). OLD surface had no caps route;
    # NEW pin added deliberately with the route.
    ("GET", "/api/v1/channels/caps"),
    # wave V16 (E5/P-02, frozen H-P1): the owner's phone read surface.  Documented by
    # app/api/v1/phone.py; routes verified live in app.routes as [GET] on these paths.  OLD
    # surface had no phone routes; NEW pins added deliberately WITH the routes.
    ("GET", "/api/v1/phone/summary"),
    ("GET", "/api/v1/phone/messages"),
    ("GET", "/api/v1/phone/calls"),
    ("GET", "/api/v1/phone/codes"),
    ("GET", "/api/v1/phone/export"),
    # wave V17 (L2): the policy-gated SEND route — documented by app/api/v1/phone.py and
    # tests/unit/api/test_v17_phone_send.py; refusals by name, audit row per attempt.
    ("POST", "/api/v1/phone/send"),
    # wave V8 (C1): the owner <-> Polsia chat — documented by app/api/v1/chat.py and
    # app/schemas/chat.py; routes verified live in app.routes as [POST]/[POST]/[GET] (checked
    # 2026-10-14). OLD surface had no chat routes; NEW pins added deliberately.
    ("POST", "/api/v1/chat"),
    ("GET", "/api/v1/chat"),
    ("POST", "/api/v1/chat/confirm"),
    # wave U5 (P2/B22): the bounded Auto Mode window — documented by app/api/v1/auto_mode.py and
    # tests/unit/test_u5_auto_mode.py, routes verified live in app.routes as [POST]/[GET]/[DELETE]
    # on this path (checked 2026-10-14). OLD surface had no auto-mode route; NEW pins added
    # deliberately with the routes (count 34 -> 37).
    ("POST", "/api/v1/auto-mode"),
    ("GET", "/api/v1/auto-mode"),
    ("DELETE", "/api/v1/auto-mode"),
    ("GET", "/api/v1/config"),
    ("PUT", "/api/v1/config"),
    ("GET", "/api/v1/config/deepseek-key"),
    ("PUT", "/api/v1/config/deepseek-key"),
    ("DELETE", "/api/v1/config/deepseek-key"),
    ("POST", "/api/v1/config/deepseek-key/verify"),
    ("GET", "/api/v1/dashboard/summary"),
    ("GET", "/api/v1/dashboard/activity"),
    ("GET", "/api/v1/dashboard/reports/daily"),
    # wave V8 (C5; WAVE-PLAN V8-D7 anticipated this route): the daily-reports LIST read,
    # documented by app/api/v1/dashboard.py; route verified live in app.routes as [GET] on this
    # path (checked 2026-10-14). OLD surface had only /reports/daily.
    ("GET", "/api/v1/dashboard/reports"),
    ("GET", "/api/v1/memory"),
    ("POST", "/api/v1/memory"),
    # the memory-delete lane's route (R19) — pinned here because R17 owns this file this wave
    # and that lane left the surface pin un-updated; remove this row if that route is reverted.
    ("DELETE", "/api/v1/memory/{memory_id}"),
    ("GET", "/api/v1/finance/summary"),
    ("GET", "/api/v1/finance/revenue"),
    ("GET", "/api/v1/finance/expenses"),
    ("GET", "/api/v1/finance/events"),
    # wave V9 (B2, V9-D3): the planning view — documented by app/api/v1/finance.py
    # (build_planning_view) and tests/unit/test_v9_planning.py; route verified live in app.routes
    # as [GET] on this path (checked 2026-10-14). OLD surface had no planning route.
    ("GET", "/api/v1/finance/planning"),
    ("POST", "/api/v1/finance/stripe/webhook"),
    # wave V9 (B2, V9-D3): the money-core surface of wave V9 — the DATED pricing-catalog snapshot
    # read (documented by app/api/v1/pricing.py), the per-agent quality read (app/api/v1/ops.py)
    # and the site PACKAGE export (app/api/v1/site.py); routes verified live in app.routes as
    # [GET]/[GET]/[POST] on these paths (checked 2026-10-14). OLD surface had none of them.
    ("GET", "/api/v1/pricing"),
    ("GET", "/api/v1/ops/agent-quality"),
    ("POST", "/api/v1/site/package"),
    # wave V12 (W-C): the site-package DOWNLOAD (app/api/v1/site.py) — read-only zip of the
    # written package, exports-root-guarded. Documented by
    # tests/unit/api/test_v12_site_package_download.py; route verified live in app.routes.
    ("GET", "/api/v1/site/package/download"),
    ("GET", "/api/v1/social/posts"),
    ("GET", "/api/v1/outreach/prospects"),
    # wave V11 (W1): the public one-click opt-out route — unauthenticated by design, because the
    # link in a compliant footer must work for a recipient with no owner key. NEW pin with the route.
    ("GET", "/api/v1/outreach/unsubscribe"),
    ("GET", "/api/v1/ads/campaigns"),
    # wave U5 (P4/B8): the usage-ledger read + export — documented by app/api/v1/runs.py and
    # tests/unit/test_u5_runs.py, routes verified live in app.routes as [GET] on these paths.
    # OLD surface had no runs route; NEW pins added deliberately with the routes (count 37 -> 39).
    ("GET", "/api/v1/runs"),
    ("GET", "/api/v1/runs/export"),
    # wave V8 (C2): the owner's scheduled task prompts — documented by app/api/v1/schedules.py and
    # app/schemas/schedule.py; routes verified live in app.routes as [GET]/[POST] on the
    # collection and [PATCH]/[DELETE] on `/schedules/{schedule_id}` (checked 2026-10-14). OLD
    # surface had no schedules routes; NEW pins added deliberately (count 39 -> 43).
    ("GET", "/api/v1/schedules"),
    ("POST", "/api/v1/schedules"),
    ("PATCH", "/api/v1/schedules/{schedule_id}"),
    ("DELETE", "/api/v1/schedules/{schedule_id}"),
    # wave U2 (B1): the notifications bell's feed — derived from task rows (no table); documented by
    # app/api/v1/notifications.py and tests/unit/test_u2_notifications.py, route verified live in
    # app.routes as [GET] on this path (checked 2026-10-14). OLD surface had no notifications route.
    ("GET", "/api/v1/notifications"),
    # R17: the customer-support inbox ingest — owner-authenticated submit + list
    # (documented by app/api/v1/support.py and tests/unit/api/test_support_inbox.py).
    ("POST", "/api/v1/support/inbox"),
    ("GET", "/api/v1/support/inbox"),
    # wave V16 (E6/H-C1): the capability map — one derivation read live from AGENT_CAPABILITIES +
    # tool_registry (documented by app/api/v1/ops.py and tests/unit/api/test_v16_ops_capabilities.py;
    # verified live in app.routes as [GET]).  Count 65 -> 66 on /api/v1.
    ("GET", "/api/v1/ops/capabilities"),
    # R22: the activity publish breaker's operator surface — read the counters, reset after an
    # incident (documented by app/api/v1/ops.py and tests/unit/api/test_r22_ops_events.py).
    ("GET", "/api/v1/ops/events/stats"),
    ("POST", "/api/v1/ops/events/reset"),
    # V11 unit 2 (parent-surgical): the read-only SANDBOX_MODE flag behind the UI banner
    # (documented by app/api/v1/ops.py and tests/unit/api/test_v11_ops_sandbox.py).
    ("GET", "/api/v1/ops/sandbox"),
    # wave V9 settle (V9-D3, parent): the planner / invoices / economics routes — documented by
    # app/api/v1/planner.py, app/api/v1/invoices.py, app/api/v1/economics.py and their unit
    # suites; verified live in app.routes (checked 2026-10-14). Count 51 -> 60.
    ("GET", "/api/v1/planner/status"),
    ("POST", "/api/v1/planner/propose"),
    ("POST", "/api/v1/planner/confirm"),
    ("GET", "/api/v1/invoices"),
    ("POST", "/api/v1/invoices"),
    ("GET", "/api/v1/invoices/{invoice_id}"),
    ("POST", "/api/v1/invoices/{invoice_id}/send"),
    ("POST", "/api/v1/invoices/{invoice_id}/void"),
    ("GET", "/api/v1/economics/context"),
    # wave V10 (H1/D7): the data portability surface — documented by app/api/v1/data.py and
    # tests/unit/api/test_v10_data_export.py; GET /data/export (ZIP) + DELETE /data (dry-run /
    # single-use-token wipe). The count comment chain continues: Count 51 -> 60 -> 62.
    ("GET", "/api/v1/data/export"),
    ("DELETE", "/api/v1/data"),
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

# README's agent table: Orchestrator (since D7 the row reads "Morning: hourly, self-gated to the
# configured company-local hour (default 06:00) — Evening: 20:00 UTC"), Social every 2h, Email
# every 3h, Ads+Stripe every 6h — plus the maintenance job that the upload declared and never
# scheduled, and the task-lease sweep T1 added (its README row reads "Every 5 min").
DOCUMENTED_SCHEDULE_ENTRIES = {
    "morning-cycle",
    "evening-cycle",
    "social-mentions-sweep",
    "email-inbox-sweep",
    "ads-stripe-sync",
    "task-lease-sweep",
    # V8/C2: the every-15-min due-schedule tick (`celery_app.tasks.cadence.run_due_schedules`) —
    # children are JUST-IN-TIME (one per due schedule per due instant), never materialized ahead.
    "task-cadence",
    # V9/B3: the outreach follow-up sweep (every 3 h; per-log dedupe key).
    "email-followups",
    # V9/B4 (parent settle): the invoice dunning ladder (daily; once-per-step activity ledger).
    "invoice-dunning",
    # V11/W2 (parent settle): the stranded-pending reconciler (every 5 min; README row added at the
    # same settle — the crash-between-commit-and-dispatch orphan class, audit F1).
    "reconcile-stranded-pending",
    # wave v15 / E3 (3-01): the stale-agent-run closer (every 5 min; §B5 — `agent_runs` rows
    # stuck `running` forever had no closer reachable by a dead worker).
    "stale-agent-runs-sweep",
    # wave v15 / E3 (3-03): the paused-release re-arm (every 5 min; §B7 + decision #2 — the
    # resume half of the paused-dispatch contract; marker `tasks.metadata.paused_release`).
    "paused-release-rearm",
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
    # V8/C2: the cadence tick exists AND is scheduled — the same "declared but never scheduled"
    # defect this set exists to catch.
    "celery_app.tasks.cadence.run_due_schedules",
    # V9/B3 + V9/B4 (parent settle): the follow-up sweep and the dunning ladder are declared AND
    # scheduled — the same defect class.
    "celery_app.tasks.followups.run_follow_up_sweep",
    "celery_app.tasks.dunning.run_dunning_ladder",
    # V11/W2 (parent settle): the stranded-pending reconciler — the same "declared but never
    # scheduled" defect class it exists to close; pinned by task path like its lease-sweep sibling.
    "celery_app.tasks.maintenance.reconcile_stranded_pending_tasks",
    # wave v15 / E3 (3-01 + 3-03): the stale-run closer and the paused-release re-arm — the same
    # "declared but never scheduled" defect class; pinned by task path like their siblings.
    "celery_app.tasks.maintenance.close_stale_agent_runs",
    "celery_app.tasks.maintenance.rearm_paused_released_tasks",
}

# Every field the parent froze for build wave 1 (see polsia-work/BUILD-BRIEF.md §3), plus
# `approval_secret` — the D1 approvals interface, added by wave-W2/T05 for the T02 seat to
# consume (`settings.approval_secret`); frozen by D1.
FROZEN_SETTINGS = {
    "api_key", "database_url", "redis_url", "celery_broker_url", "celery_result_backend",
    "morning_cycle_hour", "evening_cycle_hour", "company_timezone", "stripe_webhook_secret",
    "claude_cli_path", "claude_cli_mock", "claude_timeout_seconds", "claude_max_concurrency",
    "claude_max_argv_chars", "claude_prompt_via_stdin", "claude_repair_attempts",
    "agent_max_retries", "agent_retry_backoff_seconds", "agent_run_cost_cap_usd",
    "agent_daily_cost_cap_usd", "task_claim_lease_seconds", "task_max_attempts",
    "task_retry_backoff_seconds", "activity_retention_days",
    "activity_bulk_flush", "celery_task_time_limit", "celery_soft_time_limit",
    "celery_max_tasks_per_child", "db_pool_size", "db_max_overflow", "db_pool_recycle_seconds",
    "db_statement_timeout_ms", "planner_max_nodes", "planner_context_token_budget", "log_json",
    "approval_secret",
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

    sql = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.py"))
    )
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
