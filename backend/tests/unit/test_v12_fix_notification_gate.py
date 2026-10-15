"""V12/F2 — D2: owner notifications route through the SHARED side-effect gate (sandbox + caps).

RED-first (measured on the pre-fix tree; lane raw/red-d2.txt): `notification_service` called
`integrations.send_email` directly — with `SANDBOX_MODE=true` (the shipped default) a configured
install put REAL mail on the wire AND skipped the email-channel caps.

Contract pinned here (post-fix), for BOTH egresses (`notify_owner_for_terminal_outcome` and
`notify_owner_cycle_report`):
  * sandbox ON  -> the provider is never called; ONE named row (`owner_notification_failed` /
    `cycle_report_email_failed`) carries `E_SANDBOX_BLOCKED` plus the house draft markers
    (`sandbox: true`, `drafted_not_sent: true`) — a labelled draft, never a silent skip;
  * sandbox OFF -> the mail is sent (the adapter recorder sees exactly one message);
  * the REAL `channel_service` counts are consulted; a capped channel refuses BY NAME
    (`E_RATE_LIMITED`) and nothing reaches the provider.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import select

from app.agents import integrations
from app.config import settings
from app.core.errors import E_SANDBOX_BLOCKED, PolsiaError
from app.models.company import CompanyConfig
from app.models.report import ActivityLog
from app.models.task import Task
from app.services import channel_service, notification_service

OWNER = "owner@example.com"


def _provider(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-f2-d2"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    return sent


async def _seed_task(db: Any) -> Task:
    task = Task(title="ship the thing", agent_type="finance", status="failed")
    db.add(task)
    await db.commit()
    return task


async def _rows(db: Any) -> list[ActivityLog]:
    result = await db.execute(select(ActivityLog).order_by(ActivityLog.id))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_sandbox_on_refuses_by_name_and_never_calls_the_provider(
    async_db_session, monkeypatch, mock_redis
):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    sent = _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email=OWNER, notify_on_failed=True)
    )
    task = await _seed_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "failed"
    )

    assert sent == [], "SANDBOX_MODE=true but the owner-notification egress reached the provider"
    (row,) = await _rows(async_db_session)
    assert row.action == "owner_notification_failed"
    assert row.agent_type == "orchestrator"
    error = row.detail["error"]
    assert error["code"] == E_SANDBOX_BLOCKED
    assert error["sandbox"] is True
    assert error["drafted_not_sent"] is True
    assert f"#{task.id}" in row.summary


@pytest.mark.asyncio
async def test_sandbox_off_sends_exactly_once(async_db_session, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", False)
    sent = _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email=OWNER, notify_on_failed=True)
    )
    task = await _seed_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "failed"
    )

    (call,) = sent
    assert call["to"] == OWNER
    assert await _rows(async_db_session) == []


@pytest.mark.asyncio
async def test_the_real_channel_counts_are_consulted(async_db_session, monkeypatch):
    """The gate must read the MEASURED counts — a notification is email-channel egress."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    seen: list[str] = []
    real_today, real_hour = (
        channel_service.channel_sends_today,
        channel_service.channel_sends_this_hour,
    )

    async def spy_today(db, channel, **kw):
        seen.append(f"today:{channel}")
        return await real_today(db, channel, **kw)

    async def spy_hour(db, channel, **kw):
        seen.append(f"hour:{channel}")
        return await real_hour(db, channel, **kw)

    monkeypatch.setattr(channel_service, "channel_sends_today", spy_today)
    monkeypatch.setattr(channel_service, "channel_sends_this_hour", spy_hour)
    _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email=OWNER, notify_on_completed=True)
    )
    task = await _seed_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "completed"
    )

    assert seen == ["today:email", "hour:email"]


@pytest.mark.asyncio
async def test_a_capped_channel_refuses_by_name_and_sends_nothing(
    async_db_session, monkeypatch, mock_redis
):
    monkeypatch.setattr(settings, "sandbox_mode", False)

    async def capped(db, channel, **kw):
        return 50  # RateCaps().per_channel_day["email"] — at the day cap

    monkeypatch.setattr(channel_service, "channel_sends_today", capped)
    sent = _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email=OWNER, notify_on_failed=True)
    )
    task = await _seed_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "failed"
    )

    assert sent == []
    (row,) = await _rows(async_db_session)
    assert row.detail["error"]["code"] == "E_RATE_LIMITED"


@pytest.mark.asyncio
async def test_the_cycle_report_leg_is_gated_the_same_way(
    async_db_session, monkeypatch, mock_redis
):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    sent = _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(
            name="Acme", notification_email=OWNER, cycle_report_email=True
        )
    )

    await notification_service.notify_owner_cycle_report(
        async_db_session, kind="morning", report_date=date(2026, 10, 15), summary="all good"
    )

    assert sent == []
    (row,) = await _rows(async_db_session)
    assert row.action == "cycle_report_email_failed"
    assert row.detail["error"]["code"] == E_SANDBOX_BLOCKED
    assert row.detail["error"]["drafted_not_sent"] is True


@pytest.mark.asyncio
async def test_the_cycle_report_still_sends_with_the_sandbox_off(
    async_db_session, monkeypatch
):
    monkeypatch.setattr(settings, "sandbox_mode", False)
    sent = _provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(
            name="Acme", notification_email=OWNER, cycle_report_email=True
        )
    )

    await notification_service.notify_owner_cycle_report(
        async_db_session, kind="morning", report_date=date(2026, 10, 15), summary="all good"
    )

    (call,) = sent
    assert call["to"] == OWNER
    assert "Morning" in call["subject"]


def test_a_cap_or_policy_denial_is_a_named_polsia_error():
    """Sanity: the refusal vocabulary this file keys on exists and is stable."""
    assert E_SANDBOX_BLOCKED == "E_SANDBOX_BLOCKED"
    assert issubclass(PolsiaError, Exception)
