"""W3 / OR-09 — the evening report can never present a clipped digest as the whole day.

The defect (orchestration-hunter `BUGS.json` OR-09, P2): `report_service.build_day_digest` reads
at most `DAY_DIGEST_MAX_ROWS` (25) outcome rows and every count in the report is derived from that
PAGE — `len(digest.completed)` — with no total and no truncation field.  A 35-outcome day produced
`"25 completed, 0 failed, 0 cancelled today"`, `status="measured"`, and `daily_reports.
tasks_completed = 25`: a clipped set presented as measured (F-43's own contract: a report that
cannot see its data says so).

What this file pins, entirely through the PRODUCTION evening writer (`daily_cycle._evening_cycle`):
  * the report's `tasks_completed`/`tasks_failed` are the EXACT aggregate over the digest's own
    window, not the length of the bounded page;
  * the clip is stated: `day_digest.truncation = {truncated: True, counted: 30, shown: 25, ...}`
    in the persisted `daily_reports.metrics_snapshot`, in the cycle's activity detail, in the
    returned outcome, and in a persisted report insight;
  * the evening agent's BRIEF carries the same completeness statement (the model reads the page);
  * a day under the bound says `truncated: False` and adds no statement (no crying wolf).

`report_service.DayDigest` (and its `headline()`) belongs to another seat's territory; the
additive field lives in the report writer (`daily_cycle`), which is this seat's file.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from app.config import settings
from celery_app.tasks import daily_cycle

DAY = "2026-10-14"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _seed_company(queue_db) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with queue_db.factory() as db:
            db.add(CompanyConfig(name="W3 Co", timezone="UTC", daily_cycle_hour=6))
            await db.commit()

    queue_db.run(_go())


def _seed_outcomes(queue_db, count: int, *, status: str = "completed") -> None:
    """`count` outcome rows whose `updated_at` sits inside the report's day window."""
    from app.models.task import TaskStatus
    from app.services.task_service import create_task

    stamp = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    async def _go() -> None:
        async with queue_db.factory() as db:
            for index in range(count):
                task = await create_task(
                    db,
                    title=f"Outcome {index}",
                    agent_type="business_planning",
                    source="orchestrator",
                )
                task.status = TaskStatus(status)
                task.result_summary = f"result {index}"
                # The digest selects `updated_at` (the moment the row reached its outcome); pin it
                # inside the day the cycle files its report against.
                task.updated_at = stamp
                task.created_at = stamp
            await db.commit()

    queue_db.run(_go())


def _run_evening(queue_db, monkeypatch, *, model_result: dict | None = None) -> tuple[dict, list[dict]]:
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "evening_cycle_hour", 20)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 14, 20, 30, tzinfo=UTC))
    _seed_company(queue_db)
    briefs: list[dict] = []
    import app.agents.crew_factory as crew_factory

    def _spy(agent_type, task_dict, context):  # noqa: ANN001
        briefs.append({"agent_type": agent_type, "task": dict(task_dict or {})})
        if model_result is not None:
            return dict(model_result)
        return {"summary": "the day went well", "insights": [], "tasks": []}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    result = queue_db.run(daily_cycle._evening_cycle())
    return result, briefs


def _report_row(queue_db) -> dict:
    rows = queue_db.query(
        "SELECT tasks_completed, tasks_failed, insights, metrics_snapshot, evening_summary "
        "FROM daily_reports ORDER BY id DESC LIMIT 1"
    )
    assert rows, "the evening cycle wrote no report"
    row = dict(rows[0])
    row["insights"] = json.loads(row["insights"]) if row["insights"] else []
    row["metrics_snapshot"] = (
        json.loads(row["metrics_snapshot"]) if row["metrics_snapshot"] else {}
    )
    return row


# ── 1. over the bound: the clip is measured and stated everywhere the report is read ──────────


def test_a_day_over_the_digest_bound_states_the_truncation(queue_db, monkeypatch):
    """RED (pre-fix): `tasks_completed` was 25 for 30 real outcomes, and nothing said so."""
    _seed_outcomes(queue_db, 30)

    result, briefs = _run_evening(queue_db, monkeypatch)

    assert result["status"] == "completed", result
    # (a) the REPORT COUNTS are the exact aggregate, not `len(page)`.
    assert result["tasks_completed"] == 30, result
    assert result["tasks_failed"] == 0, result
    # (b) the clip is a measured field on the outcome...
    truncation = result["day_digest_truncation"]
    assert truncation["truncated"] is True, truncation
    assert truncation["counted"] == 30, truncation
    assert truncation["shown"] == 25, truncation
    assert truncation["bound"] == 25, truncation
    assert truncation["by_status"] == {"completed": 30}, truncation
    # (c) ... and the digest the report stores carries it beside the (bounded) rows.
    report = _report_row(queue_db)
    stored = report["metrics_snapshot"]["day_digest"]
    assert stored["truncation"]["truncated"] is True, stored.get("truncation")
    assert stored["truncation"]["counted"] == 30
    assert len(stored["completed"]) == 25, "the stored list stays bounded"
    assert report["tasks_completed"] == 30, (
        "daily_reports.tasks_completed is the page length, not the day's count"
    )
    assert report["tasks_failed"] == 0
    # (d) the persisted report states it in prose too (an insight a reader sees).
    joined = " ".join(report["insights"])
    assert "day digest clipped" in joined, report["insights"]
    assert "25 of 30" in joined, report["insights"]
    # (e) the evening agent's brief is not presented as the whole day.
    (brief,) = briefs
    description = brief["task"]["description"]
    assert "DIGEST COMPLETENESS" in description, description
    assert "25 of 30" in description, description
    # (f) the cycle's own activity row carries the measured flag.
    detail = json.loads(
        queue_db.query(
            "SELECT detail FROM activity_log WHERE action = 'evening_summary_complete' "
            "ORDER BY id DESC LIMIT 1"
        )[0]["detail"]
    )
    assert detail["day_digest_truncated"] is True, detail
    assert detail["day_digest_counted"] == 30, detail
    assert detail["day_digest_shown"] == 25, detail
    assert detail["tasks_cancelled"] == 0, detail


# ── 2. under the bound: no false alarm, and the exact counts still agree ─────────────────────


def test_a_day_under_the_bound_reports_no_truncation(queue_db, monkeypatch):
    _seed_outcomes(queue_db, 5)

    result, briefs = _run_evening(queue_db, monkeypatch)

    truncation = result["day_digest_truncation"]
    assert truncation["truncated"] is False, truncation
    assert truncation["counted"] == 5, truncation
    assert truncation["shown"] == 5, truncation
    assert result["tasks_completed"] == 5
    report = _report_row(queue_db)
    assert report["metrics_snapshot"]["day_digest"]["truncation"]["truncated"] is False
    assert report["tasks_completed"] == 5
    assert report["insights"] == [], "a complete digest must not cry wolf"
    (brief,) = briefs
    assert "DIGEST COMPLETENESS" not in brief["task"]["description"]


# ── 3. an EMPTY day is a measured fact, not an unknown ───────────────────────────────────────


def test_an_empty_day_is_measured_complete_and_states_no_truncation(queue_db, monkeypatch):
    result, briefs = _run_evening(queue_db, monkeypatch)

    assert result["status"] == "completed", result
    truncation = result["day_digest_truncation"]
    assert truncation["truncated"] is False, truncation
    assert truncation["counted"] == 0
    assert truncation["shown"] == 0
    assert result["tasks_completed"] == 0
    (brief,) = briefs
    assert "no task reached an outcome today" in brief["task"]["description"]
    assert "DIGEST COMPLETENESS" not in brief["task"]["description"]


# ── 4. F2 finisher: the fallback sentence uses the EXACT counts when the model says nothing ───


def test_a_clipped_day_with_no_model_summary_states_the_exact_counts(queue_db, monkeypatch):
    """F2 RED (pre-fix): the fallback was `digest.headline()` — the clipped 25 presented as the
    day's count in `daily_reports.evening_summary`, even though the row's other fields were exact."""
    _seed_outcomes(queue_db, 30)

    result, _briefs = _run_evening(queue_db, monkeypatch, model_result={})

    assert result["status"] == "completed", result
    assert result["tasks_completed"] == 30, result
    summary = result["summary"]
    assert "30 completed" in summary, summary
    assert "25 of 30" in summary, summary
    assert "25 completed" not in summary, summary
    assert "returned no summary" in summary, summary
    report = _report_row(queue_db)
    assert report["evening_summary"] == summary, report["evening_summary"]
    assert report["tasks_completed"] == 30, report
