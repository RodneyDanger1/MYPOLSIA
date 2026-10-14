"""V8/C5 — the cycle-report email leg (plan §7 C5: after the report write → configured send /
honest skip; never failing the cycle).

Two levels:
  * the never-raising helper (`notification_service.notify_owner_cycle_report`) — configured
    sends exactly once; absent config writes exactly ONE named `cycle_report_email_skipped`
    row; adapter failures are recorded once and swallowed;
  * the daily_cycle call sites — the step is invoked AFTER the report transaction commits
    (file-truth read at call time: reader is not the writer), once per cycle, and an
    unconfigured company still runs the whole cycle.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.agents import integrations
from app.config import settings
from app.models.report import ActivityLog
from app.services import company_service
from app.services.notification_service import notify_owner_cycle_report
from celery_app.tasks import daily_cycle

OWNER = "owner@example.com"
REPORT_DATE = date(2026, 10, 14)


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "name": "Cycle Co",
        "notification_email": None,
        "cycle_report_email": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_company(monkeypatch: pytest.MonkeyPatch, config: Any) -> None:
    async def fake_get_company(_db: Any) -> Any:
        return config

    monkeypatch.setattr(company_service, "get_company", fake_get_company)


def _send_recorder(
    monkeypatch: pytest.MonkeyPatch, *, result: dict[str, Any] | None = None, raises: Exception | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        calls.append({"to": to, "subject": subject, "body": body})
        if raises is not None:
            raise raises
        if result is not None:
            return result
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-cycle-1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    return calls


async def _activity_rows(session: Any) -> list[ActivityLog]:
    return list((await session.execute(select(ActivityLog).order_by(ActivityLog.id))).scalars().all())


# ── configured: sends once, records nothing ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_configured_cycle_report_sends_once_and_records_nothing(
    async_db_session, monkeypatch, mock_redis
):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, cycle_report_email=True))

    assert (
        await notify_owner_cycle_report(
            async_db_session, kind="morning", report_date=REPORT_DATE, summary="Plan: ship it"
        )
        is None
    )

    (call,) = calls
    assert call["to"] == OWNER
    assert "Cycle Co" in call["subject"]
    assert "Morning" in call["subject"] and REPORT_DATE.isoformat() in call["subject"]
    assert "Plan: ship it" in call["body"]
    assert await _activity_rows(async_db_session) == [], "success is not a row"


# ── unconfigured: exactly ONE honest, named skip row ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_real_default_company_row_writes_one_named_skip(async_db_session, monkeypatch, mock_redis):
    from app.models.company import CompanyConfig

    calls = _send_recorder(monkeypatch)
    async_db_session.add(CompanyConfig(name="Cycle Co"))
    await async_db_session.commit()

    assert (
        await notify_owner_cycle_report(
            async_db_session, kind="morning", report_date=REPORT_DATE, summary="s"
        )
        is None
    )

    assert calls == []
    (row,) = await _activity_rows(async_db_session)
    assert row.action == "cycle_report_email_skipped"
    assert row.agent_type == "orchestrator"
    assert row.detail["kind"] == "morning"
    assert row.detail["report_date"] == REPORT_DATE.isoformat()
    assert "cycle_report_email" in row.detail["reason"]


@pytest.mark.asyncio
async def test_toggle_off_names_the_toggle(async_db_session, monkeypatch, mock_redis):
    _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, cycle_report_email=False))

    await notify_owner_cycle_report(
        async_db_session, kind="evening", report_date=REPORT_DATE, summary="s"
    )

    (row,) = await _activity_rows(async_db_session)
    assert row.detail["reason"] == "cycle_report_email is off"


@pytest.mark.asyncio
async def test_toggle_on_without_an_email_names_the_email(async_db_session, monkeypatch, mock_redis):
    _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(cycle_report_email=True))

    await notify_owner_cycle_report(
        async_db_session, kind="morning", report_date=REPORT_DATE, summary="s"
    )

    (row,) = await _activity_rows(async_db_session)
    assert row.detail["reason"] == "notification_email is not set"


@pytest.mark.asyncio
async def test_an_unknown_kind_is_a_named_skip_never_a_crash(async_db_session, monkeypatch, mock_redis):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, cycle_report_email=True))

    assert (
        await notify_owner_cycle_report(
            async_db_session, kind="weekly", report_date=REPORT_DATE, summary="s"
        )
        is None
    )

    assert calls == []
    (row,) = await _activity_rows(async_db_session)
    assert row.action == "cycle_report_email_skipped"
    assert "weekly" in row.detail["reason"]


# ── failures: recorded once, never raised ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_adapter_refusal_records_one_failure_row_and_returns_normally(
    async_db_session, monkeypatch, mock_redis
):
    refusal = {
        "ok": False,
        "error": {"code": "E_INTEGRATION", "message": "SendGrid configuration missing: SENDGRID_API_KEY"},
    }
    _send_recorder(monkeypatch, result=refusal)
    _patch_company(monkeypatch, _config(notification_email=OWNER, cycle_report_email=True))

    assert (
        await notify_owner_cycle_report(
            async_db_session, kind="morning", report_date=REPORT_DATE, summary="s"
        )
        is None
    )

    # COMMITTED (the step runs after the cycle's own commit; nothing else would commit it).
    assert async_db_session.in_transaction() is False
    (row,) = await _activity_rows(async_db_session)
    assert row.action == "cycle_report_email_failed"
    assert row.level == "warning"
    assert row.detail["error"]["code"] == "E_INTEGRATION"


@pytest.mark.asyncio
async def test_a_raising_adapter_is_swallowed_and_recorded_once(async_db_session, monkeypatch, mock_redis):
    _send_recorder(monkeypatch, raises=RuntimeError("smtp exploded"))
    _patch_company(monkeypatch, _config(notification_email=OWNER, cycle_report_email=True))

    assert (
        await notify_owner_cycle_report(
            async_db_session, kind="evening", report_date=REPORT_DATE, summary="s"
        )
        is None
    )

    (row,) = await _activity_rows(async_db_session)
    assert row.action == "cycle_report_email_failed"
    assert row.detail["error"]["code"] == "RuntimeError"


# ── the daily_cycle call sites (file-backed DB; real cycle body, agent+broker spied) ─────────
class _GroupRecorder:
    """Stands in for the `celery.group` the fan-out helper imports at call time."""

    def __init__(self) -> None:
        self.batches: list[list] = []
        self.apply_async_count = 0

    def __call__(self, sigs):
        self.batches.append(list(sigs))
        return self

    def apply_async(self) -> None:
        self.apply_async_count += 1


def _seed_cycle_company(qdb, **over) -> None:
    from app.models.company import CompanyConfig

    async def _go():
        async with qdb.factory() as db:
            db.add(CompanyConfig(name="Cycle Co", timezone="UTC", daily_cycle_hour=6, **over))
            await db.commit()

    qdb.run(_go())


def _install_group_recorder(monkeypatch) -> _GroupRecorder:
    recorder = _GroupRecorder()
    monkeypatch.setattr("celery.group", recorder)
    return recorder


def _install_agent_spy(monkeypatch) -> MagicMock:
    import app.agents.crew_factory as crew_factory

    spy = MagicMock(return_value={"summary": "test plan", "tasks": []})
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return spy


def test_the_morning_cycle_writes_exactly_one_skip_row_when_unconfigured(
    queue_db, monkeypatch, mock_redis
):
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    _seed_cycle_company(queue_db)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 14, 6, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    _install_agent_spy(monkeypatch)

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "completed", "an unconfigured email must never fail the cycle"
    assert queue_db.count("daily_reports") == 1, "the report row is the report's own truth"
    assert queue_db.count("activity_log", "action = 'cycle_report_email_skipped'") == 1


def test_the_email_step_runs_after_the_report_transaction_commits(queue_db, monkeypatch, mock_redis):
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    _seed_cycle_company(queue_db)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 14, 6, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    _install_agent_spy(monkeypatch)

    observed: list[dict[str, Any]] = []

    async def spy(*, kind: str, report_date: date, summary: str) -> None:
        # File truth read at call time: the reader is not the writer.
        observed.append({"kind": kind, "reports_on_file": queue_db.count("daily_reports")})

    monkeypatch.setattr(daily_cycle, "_notify_cycle_report", spy)

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "completed"
    assert observed == [{"kind": "morning", "reports_on_file": 1}], (
        "the report row must be committed before the email step is invoked"
    )


def test_the_evening_cycle_runs_the_same_step_once(queue_db, monkeypatch, mock_redis):
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "evening_cycle_hour", 20)
    _seed_cycle_company(queue_db)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: datetime(2026, 10, 14, 20, 30, tzinfo=UTC))
    _install_group_recorder(monkeypatch)
    _install_agent_spy(monkeypatch)

    result = daily_cycle.run_evening_cycle()

    assert result["status"] == "completed"
    assert queue_db.count("daily_reports") == 1
    assert queue_db.count("activity_log", "action = 'cycle_report_email_skipped'") == 1
