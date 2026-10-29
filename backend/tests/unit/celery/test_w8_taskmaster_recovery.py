"""W8 TASKMASTER — the two live dispatch defects the resilience drill measured.

DEFECT 1 (F-W8-TM-1, live-blocking): `agent_runs.input_context` is a JSON column and the company
context carries `company.owner_budget_usd` as a `Decimal` (`NUMERIC(10,2)` at rest).  SQLAlchemy's
JSON serializer is `json.dumps`, so EVERY dispatch on PostgreSQL died at `create_agent_run` with
`TypeError: Object of type Decimal is not JSON serializable` — measured on the live stack
2026-10-29 (`raw/w8/d1-result.txt`, worker log).  The worker now stores a JSON-safe copy and logs
each converted path; the agent still receives the original value.

DEFECT 2 (F-W8-TM-2, wedge): that exception happened AFTER the claim and BEFORE any work, so the
delivery died leaving the task row `in_progress` with one attempt spent, NO `agent_runs` row and
NO settlement.  A Celery exception never reaches `self.retry`'s ladder, and with Beat off only a
lease sweep could recover it.  `_settle_pre_run_failure` now puts the claim on the same ladder a
failed run uses: `failed` with the real exception, requeued `pending` + `rearmed_at` when the
failure is retryable and the ladder has room, and ONE activity row that names the delivery death.

RED legs (raw/w8/junit-w8-RED-*.xml): removing `json_safe` from the context write reddens the two
storage tests with the exact live TypeError; removing the `except` from the run-creation block
reddens the two settlement tests with the row left `in_progress`.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

import pytest

from app.core.errors import E_DB, E_VALIDATION, PolsiaError
from celery_app.tasks import agent_tasks
from celery_app.tasks._common import json_safe

UTC_STAMP = datetime(2026, 10, 29, 7, 0, tzinfo=UTC)


# ── the helper itself ─────────────────────────────────────────────────────────────────────────
def test_json_safe_converts_only_the_leaves_json_cannot_hold():
    value = {
        "company": {"owner_budget_usd": Decimal("125.00"), "hours": 8},
        "when": UTC_STAMP,
        "day": date(2026, 10, 29),
        "at": time(7, 0),
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "ok": True,
        "nothing": None,
        "ratio": 0.5,
    }
    converted, paths = json_safe(value)
    assert converted["company"]["owner_budget_usd"] == "125.00"
    assert converted["company"]["hours"] == 8
    assert converted["when"].startswith("2026-10-29")
    assert converted["day"] == "2026-10-29"
    assert converted["at"] == "07:00:00"
    assert converted["id"] == "00000000-0000-0000-0000-000000000001"
    assert converted["ok"] is True and converted["nothing"] is None and converted["ratio"] == 0.5
    assert sorted(paths) == [
        "at",
        "company.owner_budget_usd",
        "day",
        "id",
        "when",
    ]
    # the whole copy is storable by the serializer the JSON columns actually use
    json.dumps(converted)


def test_json_safe_returns_the_same_object_when_nothing_needs_converting():
    value = {"a": 1, "b": ["x", {"c": None}]}
    converted, paths = json_safe(value)
    assert converted is value
    assert paths == []


# ── DEFECT 1: a Decimal context is stored, not lost ──────────────────────────────────────────
@pytest.fixture
def decimal_context(monkeypatch):
    """The live context's shape: the company budget arrives as a Decimal (NUMERIC at rest)."""

    async def _fake(_db, **_kwargs):
        return {
            "company": {"id": 1, "name": "Cinder & Lye", "owner_budget_usd": Decimal("125.00")},
            "todays_tasks": [],
        }

    monkeypatch.setattr("app.services.company_service.get_full_context", _fake)
    return _fake


def test_a_decimal_context_is_stored_as_text_and_the_run_is_created(
    queue_db, install_run_spy, execute_body, decimal_context, caplog
):
    install_run_spy(delegate=False, result={"summary": "w8 context drill"})
    task_id = queue_db.create_task(agent_type="orchestrator")

    with caplog.at_level(logging.WARNING):
        outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1, "the run row must exist — the live defect lost it entirely"
    stored = json.loads(runs[0]["input_context"])
    assert stored["company"]["owner_budget_usd"] == "125.00"
    assert any(
        "not JSON-encodable" in record.message and "company.owner_budget_usd" in record.message
        for record in caplog.records
    ), "the conversion must be LOGGED, never silent"


def test_a_decimal_in_the_run_output_is_stored_as_text(
    queue_db, install_run_spy, execute_body, decimal_context
):
    install_run_spy(
        delegate=False,
        result={"summary": "w8 output drill", "measured_total_usd": Decimal("0.0032")},
    )
    task_id = queue_db.create_task(agent_type="orchestrator")

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed"
    run = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))[0]
    output = json.loads(run["output"])
    assert output["measured_total_usd"] == "0.0032"
    assert output["summary"] == "w8 output drill"


# ── DEFECT 2: an exception before the run row settles the claim ─────────────────────────────
def _fail_create_run(monkeypatch, error: BaseException) -> None:
    async def _boom(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("app.services.task_service.create_agent_run", _boom)


def _activities(queue_db, action: str) -> list[dict]:
    return queue_db.query("SELECT * FROM activity_log WHERE action = ? ORDER BY id", (action,))


def test_a_retryable_pre_run_failure_requeues_instead_of_wedging(queue_db, install_run_spy, execute_body, monkeypatch):
    spy = install_run_spy(delegate=False)
    _fail_create_run(monkeypatch, PolsiaError(E_DB, "the ledger is unreachable", how_to_fix="retry"))
    task_id = queue_db.create_task(agent_type="orchestrator")

    outcome = execute_body(task_id)

    assert outcome["status"] == "failed"
    assert outcome["retryable"] is True
    assert outcome["requeued"] is True
    row = queue_db.row(task_id)
    assert row["status"] == "pending", "a retryable pre-run failure must stay queued"
    assert row["claimed_by"] is None and row["claimed_at"] is None and row["heartbeat_at"] is None
    assert row["attempts"] == 1, "the claim's execution counts; it is not refunded"
    assert "E_DB" in (row["error_message"] or "")
    metadata = json.loads(row["metadata"] or "{}")
    assert "rearmed_at" in metadata, "the reconciler's second net must be able to see this row"
    assert spy.calls == [], "nothing may have run"
    assert queue_db.count("agent_runs") == 0, "no run row may be fabricated"
    activity = _activities(queue_db, agent_tasks.PRE_RUN_FAILURE_ACTION)
    assert len(activity) == 1
    assert json.loads(activity[0]["detail"])["requeued"] is True


def test_a_terminal_pre_run_failure_is_failed_by_name(queue_db, install_run_spy, execute_body, monkeypatch):
    install_run_spy(delegate=False)
    _fail_create_run(
        monkeypatch, PolsiaError(E_VALIDATION, "the context is malformed", how_to_fix="fix it")
    )
    task_id = queue_db.create_task(agent_type="orchestrator")

    outcome = execute_body(task_id)

    assert outcome["status"] == "failed"
    assert outcome["retryable"] is False
    assert outcome["requeued"] is False
    row = queue_db.row(task_id)
    assert row["status"] == "failed"
    assert row["claimed_by"] is None
    assert "E_VALIDATION" in (row["error_message"] or "")
    assert "rearmed_at" not in (json.loads(row["metadata"] or "{}") or {})
    assert queue_db.count("agent_runs") == 0
