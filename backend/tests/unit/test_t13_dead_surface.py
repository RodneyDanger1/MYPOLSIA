"""T13 / D20 — the dead service surface is gone, and `log_activities` is WIRED into the cycle.

Two halves:

1. **The eight dead functions are REMOVED** (module + `__all__` + their test lines), under the
   ticket's rule: *wire it if a natural consumer exists in the same surface TODAY, else remove
   with posted grep evidence*.  None of the eight had a consumer: `get_tasks_today_summary` was
   also a DUPLICATE of `GET /dashboard/summary`'s own statement; `get_agent_run` /
   `get_latest_revenue_snapshot` served no route; `create_post`/`mark_published`/
   `list_engagements` had no compose/status/engagements surface (and `mark_published` carried a
   recorded cap-bypass RISK if wired as-is); `outreach_service.list_campaigns` duplicated a name
   that belongs to `ads_service.list_campaigns` (`GET /api/v1/ads`); `list_email_logs` had no
   send-log route.  Grep evidence: the lane receipt `§evidence` (zero refs incl. `__all__`).

2. **`log_activities` is the cycle-summary writer**: the morning cycle's per-item records (one
   row per plan entry it SKIPPED, reason named) go through ONE `executemany`, and the cycle keeps
   exactly ONE `log_activity` for its headline announcement — because the batch writer publishes
   no Redis event and `/ws/activity` must still see the cycle.  The evening cycle has no per-item
   records, so it keeps its single `log_activity` (pinned below so the split cannot silently
   become "batch everything and lose the publish").

RED leg (measured, receipt §RED): make the cycle write each skip with a `log_activity` loop
instead of the batch call -> the batch spy sees zero calls and the shared-`batch_at` assertion
fails.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import app.config as app_config
from app.services import (
    activity_service,
    finance_service,
    outreach_service,
    social_service,
    task_service,
)
from celery_app.tasks import daily_cycle

#: OLD public surfaces (pre-T13), kept here as the recorded before-state.
_REMOVED = {
    "task_service": ("get_agent_run", "get_tasks_today_summary"),
    "finance_service": ("get_latest_revenue_snapshot",),
    "social_service": ("create_post", "list_engagements", "mark_published"),
    "outreach_service": ("list_campaigns", "list_email_logs"),
}
_MODULES = {
    "task_service": task_service,
    "finance_service": finance_service,
    "social_service": social_service,
    "outreach_service": outreach_service,
}


# ── 1. the removed surface ─────────────────────────────────────────────────────────────────────


def test_the_eight_dead_functions_are_removed_everywhere():
    for module_name, names in _REMOVED.items():
        module = _MODULES[module_name]
        for name in names:
            assert not hasattr(module, name), f"{module_name}.{name} still exists"
            assert name not in module.__all__, f"{module_name}.__all__ still advertises {name}"


def test_the_surfaces_that_stay_are_exactly_the_pinned_ones():
    assert set(social_service.__all__) == {"get_post", "list_posts", "record_published_posts"}
    assert set(outreach_service.__all__) == {
        "create_prospect", "list_prospects", "record_sent_emails"
    }
    # T02b (wave-W5) DELIBERATELY extended `task_service.__all__` with the approval-token
    # delivery pair — `attach_approval_token` (the re-queue write the mint route calls) and its
    # producer-side key constant (`APPROVAL_TOKEN_METADATA_KEY`, read by
    # `agent_tasks.approval_token_from_metadata`).  This pin moves WITH the shipped surface on
    # purpose: the surface may grow only when the growth is recorded (wave-W1's pin-update
    # doctrine), which is exactly what happened here — the old→new delta is in the wave-W5
    # receipt §"Deliberate pin updates".
    # wave U2/B1 (2026-10-14) extended it again with the task-truth reads the additive
    # TaskResponse fields and the notifications route consume — `task_terminal_at_expr` /
    # `task_completed_at` (the ONE completion-timestamp definition both surfaces share) and
    # `get_task_run_totals` + its `TaskRunTotals` result type (the grouped agent_runs sums).
    # OLD set = the 16 names below without these four; NEW set adds exactly those four; delta
    # recorded in the wave-U2 B1 receipt §"Deliberate pin updates".
    assert {
        "APPROVAL_TOKEN_METADATA_KEY", "VALID_AGENT_TYPES", "VALID_TASK_STATUSES",
        "attach_approval_token", "claim_task", "create_agent_run", "create_task", "finish_agent_run",
        "get_agent_statuses", "get_task", "heartbeat_task", "is_valid_agent_type", "list_tasks",
        "release_claim", "requeue_stale_claims", "update_task_status",
        "TaskRunTotals", "get_task_run_totals", "task_completed_at", "task_terminal_at_expr",
    } == set(task_service.__all__)
    assert "get_latest_revenue_snapshot" not in finance_service.__all__
    assert "recent_failed_payments" in finance_service.__all__


def test_log_activities_is_exported_and_documented_as_wired():
    assert "log_activities" in activity_service.__all__
    doc = activity_service.log_activities.__doc__ or ""
    assert "WIRED" in doc


# ── 2. the cycle-summary wiring (the real morning cycle, file-backed DB) ───────────────────────


@pytest.fixture(autouse=True)
def quiet_redis(monkeypatch):
    """No network: the activity publish goes to a counting mock.

    Same shape as `tests/unit/celery/conftest.py::quiet_redis` — that fixture is autouse only
    inside the celery directory, so this file re-declares it locally (fixture objects do not
    cross directories).
    """
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


def _pin_clock(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: now)


def _install_plan_spy(monkeypatch, tasks: list[dict]) -> None:
    import app.agents.crew_factory as crew_factory

    def spy(agent_type, task_dict, context):
        if agent_type == "orchestrator":
            return {"summary": "plan with skips", "tasks": tasks, "insights": []}
        return {"summary": f"spy result for {agent_type}"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)


def _install_group_recorder(monkeypatch) -> None:
    class _Group:
        def __call__(self, sigs):
            return self

        def apply_async(self) -> None:
            return None

    monkeypatch.setattr("celery.group", _Group())


def _install_batch_spy(monkeypatch) -> list[dict]:
    """Record every `log_activities` call (and delegate — the real writer still runs)."""
    calls: list[dict] = []
    real = activity_service.log_activities

    async def wrapper(db, entries, **kw):
        rows = list(entries)
        calls.append({"count": len(rows), "actions": [row.get("action") for row in rows]})
        return await real(db, entries, **kw)

    monkeypatch.setattr(activity_service, "log_activities", wrapper)
    return calls


def _run_morning(queue_db, monkeypatch, *, tasks: list[dict]) -> list[dict]:
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "morning_cycle_hour", 6)
    _pin_clock(monkeypatch, datetime(2026, 10, 13, 6, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    _install_plan_spy(monkeypatch, tasks)
    calls = _install_batch_spy(monkeypatch)
    result = daily_cycle.run_morning_cycle()
    assert result["status"] == "completed", result
    return calls


def test_skipped_entries_are_written_through_one_log_activities_batch(queue_db, monkeypatch, quiet_redis):
    tasks = [
        {"title": "Unknown agent work", "agent_type": "not_an_agent"},
        {"title": "", "agent_type": "finance"},                       # malformed entry
    ]
    calls = _run_morning(queue_db, monkeypatch, tasks=tasks)

    assert len(calls) == 1, f"expected ONE batch call, saw {calls!r}"
    assert calls[0]["count"] == 2
    assert calls[0]["actions"] == ["plan_entry_skipped", "plan_entry_skipped"]

    # the rows are on disk, each carrying its own named reason
    rows = queue_db.query("SELECT action, level, summary, detail, created_at FROM activity_log")
    skipped_rows = [row for row in rows if row["action"] == "plan_entry_skipped"]
    assert len(skipped_rows) == 2
    assert all(row["level"] == "warning" for row in skipped_rows)
    reasons = " ".join(row["summary"] for row in skipped_rows)
    assert "not_an_agent" in reasons and "malformed" in reasons
    # ONE batch stamp: every row of the batch shares the batch's `created_at` (the executemany's
    # signature — a per-row loop over `log_activity` cannot produce this)
    assert len({row["created_at"] for row in skipped_rows}) == 1

    # …and the cycle still announced itself with exactly ONE publish (the headline row)
    assert quiet_redis.publish.await_count == 1
    headline = [row for row in rows if row["action"] == "morning_plan_complete"]
    assert len(headline) == 1


def test_a_clean_plan_writes_no_batch_and_still_announces_once(queue_db, monkeypatch, quiet_redis):
    calls = _run_morning(queue_db, monkeypatch, tasks=[])
    assert calls == [], "no skips -> no batch call at all"

    rows = queue_db.query("SELECT action FROM activity_log")
    assert [row["action"] for row in rows] == ["morning_plan_complete"]
    assert quiet_redis.publish.await_count == 1


def test_the_evening_cycle_keeps_its_single_announcement(queue_db, monkeypatch, quiet_redis):
    """The documented split: no per-item records in the evening path -> no batch call."""
    calls: list[dict] = []
    real = activity_service.log_activities

    async def wrapper(db, entries, **kw):  # pragma: no cover - must never run
        calls.append({"count": len(list(entries))})
        return await real(db, entries, **kw)

    monkeypatch.setattr(activity_service, "log_activities", wrapper)
    monkeypatch.setattr(app_config.settings, "company_timezone", "Asia/Tokyo")
    monkeypatch.setattr(app_config.settings, "evening_cycle_hour", 20)
    _pin_clock(monkeypatch, datetime(2026, 10, 13, 11, 0, tzinfo=UTC))
    _install_plan_spy(monkeypatch, [])

    result = queue_db.run(daily_cycle._evening_cycle())
    assert result["status"] == "completed", result
    assert calls == []
    assert [row["action"] for row in queue_db.query("SELECT action FROM activity_log")] == [
        "evening_summary_complete"
    ]
    assert quiet_redis.publish.await_count == 1
