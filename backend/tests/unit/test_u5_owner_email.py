"""U5/P5 — owner email notifications for terminal task outcomes (`notification_service`).

Pins the FROZEN U5 contract:
  * `notify_owner_for_terminal_outcome(db, task_id, outcome)` NEVER raises — an owner
    notification must not be able to fail a task run;
  * silent NO-OP unless `company_config.notification_email` is set AND the toggle matching
    the outcome is on (the frozen P3 column names are exercised verbatim);
  * a failure to notify records ONE `owner_notification_failed` activity row and returns
    normally (the `run_artifacts_not_persisted` pattern, `agent_tasks.py:643-661`);
  * the send rides `integrations.send_email` with an EXPLICIT recipient — the adapter is
    monkeypatched in every test here; nothing in this file can touch the network.

Config construction note: P3's columns landed on `CompanyConfig` while this lane ran
(`app/models/company.py:50-62`, migration 0007 — exact frozen names). Two construction
paths are pinned: an object carrying EXACTLY the frozen names with the real accessor
patched at its definition site (the fast per-toggle legs), and REAL company_config rows
built in the test DB — the fully-configured row routes through the REAL accessor
(unpatched), the default row is the no-op.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents import integrations
from app.core.errors import E_DB, PolsiaError
from app.models.company import CompanyConfig
from app.models.report import ActivityLog
from app.models.task import Task
from app.services import company_service, notification_service
from app.services.notification_service import notify_owner_for_terminal_outcome

OUTCOMES = ("completed", "failed", "awaiting_approval")
OWNER = "owner@example.com"
TOGGLES = (
    "notify_on_completed",
    "notify_on_failed",
    "notify_on_awaiting_approval",
    "notify_on_run_requeued",
)


def _config(**overrides: Any) -> SimpleNamespace:
    """A company row carrying EXACTLY the frozen P3 column names (plus `name`)."""
    values: dict[str, Any] = {"name": "Test Co", "notification_email": None}
    values.update(dict.fromkeys(TOGGLES, False))
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_company(monkeypatch: pytest.MonkeyPatch, config: Any) -> None:
    """Patch the real config accessor's definition site (`company_service.get_company`)."""

    async def fake_get_company(_db: Any) -> Any:
        return config

    monkeypatch.setattr(company_service, "get_company", fake_get_company)


def _send_recorder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    raises: Exception | None = None,
) -> list[dict[str, Any]]:
    """Stand in for the SendGrid adapter; record every attempted send."""
    calls: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        calls.append({"to": to, "subject": subject, "body": body})
        if raises is not None:
            raise raises
        if result is not None:
            return result
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-msg-1"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    return calls


async def _make_task(session: Any, **overrides: Any) -> Task:
    values: dict[str, Any] = {"title": "Ship the landing page", "agent_type": "code_generation"}
    values.update(overrides)
    task = Task(**values)
    session.add(task)
    await session.commit()
    return task


async def _activity_rows(session: Any) -> list[ActivityLog]:
    result = await session.execute(select(ActivityLog).order_by(ActivityLog.id))
    return list(result.scalars().all())


# ─────────────────────────────── no-op family (nothing sent, nothing logged) ────────────────


@pytest.mark.asyncio
async def test_no_company_row_is_a_silent_noop(async_db_session, monkeypatch):
    calls = _send_recorder(monkeypatch)
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None
    assert calls == []
    assert await _activity_rows(async_db_session) == []


@pytest.mark.asyncio
async def test_real_company_row_with_default_columns_is_a_noop(async_db_session, monkeypatch):
    """A real row with the frozen columns at their defaults (NULL email, toggles false)."""
    calls = _send_recorder(monkeypatch)
    async_db_session.add(CompanyConfig(name="Real Co"))
    await async_db_session.commit()
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None
    assert calls == []


@pytest.mark.asyncio
async def test_real_config_row_routes_through_the_real_accessor(async_db_session, monkeypatch):
    """The strongest form: a real company_config row built in the test DB (P3's frozen columns,
    migration 0007) read through the REAL accessor — no seam patch — routes the send."""
    calls = _send_recorder(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Real Co", notification_email=OWNER, notify_on_failed=True)
    )
    await async_db_session.commit()
    task = await _make_task(async_db_session, error_message="boom")

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "failed") is None
    (call,) = calls
    assert call["to"] == OWNER
    assert "failed" in call["subject"]
    assert "Error: boom" in call["body"]


@pytest.mark.asyncio
async def test_configured_email_with_all_toggles_off_is_a_noop(async_db_session, monkeypatch):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER))
    task = await _make_task(async_db_session)

    for outcome in OUTCOMES:
        assert await notify_owner_for_terminal_outcome(async_db_session, task.id, outcome) is None
    assert calls == []


@pytest.mark.asyncio
async def test_another_outcomes_toggle_does_not_route_this_outcome(async_db_session, monkeypatch):
    """Only the toggle MATCHING the outcome routes it (notify_on_failed != completed)."""
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None
    assert calls == []


@pytest.mark.asyncio
async def test_whitespace_only_email_is_unconfigured(async_db_session, monkeypatch):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email="   ", notify_on_completed=True))
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_outcome_is_silent_and_reads_nothing(async_db_session, monkeypatch, mock_redis):
    """A string outside the frozen outcome set: no send, no error, not even a config read."""
    calls = _send_recorder(monkeypatch)
    reads: list[int] = []

    async def exploding_get_company(_db: Any) -> Any:
        reads.append(1)
        raise AssertionError("the config must not be read for an unknown outcome")

    monkeypatch.setattr(company_service, "get_company", exploding_get_company)

    assert await notify_owner_for_terminal_outcome(async_db_session, 1, "requeued") is None
    assert reads == []
    assert calls == []


# ─────────────────────────────── send family (explicit recipient, honest content) ────────────


@pytest.mark.parametrize(
    ("outcome", "toggle"),
    (
        ("completed", "notify_on_completed"),
        ("failed", "notify_on_failed"),
        ("awaiting_approval", "notify_on_awaiting_approval"),
    ),
)
@pytest.mark.asyncio
async def test_each_outcome_sends_via_its_own_frozen_toggle(
    async_db_session, monkeypatch, outcome, toggle
):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, **{toggle: True}))
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, outcome) is None

    (call,) = calls
    assert call["to"] == OWNER  # the explicit owner recipient
    assert f"#{task.id}" in call["subject"]
    assert outcome.replace("_", " ") in call["subject"]
    assert "Ship the landing page" in call["subject"]
    assert "Ship the landing page" in call["body"]
    assert str(task.id) in call["body"]
    assert outcome.replace("_", " ") in call["body"]
    assert await _activity_rows(async_db_session) == []  # success is not a failure row


@pytest.mark.asyncio
async def test_failed_outcome_carries_the_recorded_error_line(async_db_session, monkeypatch):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))
    task = await _make_task(async_db_session, error_message="RuntimeError: deploy blew up")

    await notify_owner_for_terminal_outcome(async_db_session, task.id, "failed")

    assert "Error: RuntimeError: deploy blew up" in calls[0]["body"]


@pytest.mark.asyncio
async def test_failed_outcome_without_a_recorded_error_has_no_error_line(
    async_db_session, monkeypatch
):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))
    task = await _make_task(async_db_session, error_message=None)

    await notify_owner_for_terminal_outcome(async_db_session, task.id, "failed")

    assert "Error:" not in calls[0]["body"]


@pytest.mark.asyncio
async def test_completed_outcome_never_reports_a_stale_error_line(async_db_session, monkeypatch):
    """A requeued failure KEEPS `error_message` (agent_tasks.py:730-732); a completed mail must
    not present that stale line as this outcome's story."""
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_completed=True))
    task = await _make_task(async_db_session, error_message="old failure kept by the requeue")

    await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed")

    assert "Error:" not in calls[0]["body"]
    assert "old failure" not in calls[0]["body"]


@pytest.mark.asyncio
async def test_null_title_composes_an_honest_placeholder(async_db_session, monkeypatch):
    """`tasks.title` is NOT NULL in model + DDL, so a NULL-title ROW cannot exist at rest;
    the object-level case is planted on the load seam directly."""
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))

    async def fake_load(_db: Any, task_id: int) -> Any:
        return SimpleNamespace(id=task_id, title=None, error_message=None)

    monkeypatch.setattr(notification_service, "_load_task", fake_load)

    assert await notify_owner_for_terminal_outcome(async_db_session, 77, "failed") is None
    (call,) = calls
    assert "untitled task #77" in call["subject"]
    assert "untitled task #77" in call["body"]
    assert "None" not in call["subject"]


@pytest.mark.asyncio
async def test_empty_title_real_row_uses_the_placeholder(async_db_session, monkeypatch):
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_completed=True))
    task = await _make_task(async_db_session, title="")

    await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed")

    assert f"untitled task #{task.id}" in calls[0]["subject"]


@pytest.mark.asyncio
async def test_missing_task_row_still_notifies_with_the_real_id(async_db_session, monkeypatch):
    """The row is gone (deleted between the status write and the hook): the id and outcome are
    real, only the title is unknown — the notification still goes out, nothing is fabricated."""
    calls = _send_recorder(monkeypatch)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))

    assert await notify_owner_for_terminal_outcome(async_db_session, 999999, "failed") is None
    (call,) = calls
    assert call["to"] == OWNER
    assert "untitled task #999999" in call["subject"]
    assert "Error:" not in call["body"]
    assert await _activity_rows(async_db_session) == []


# ─────────────────────────────── failure family (recorded once, never raised) ────────────────


@pytest.mark.asyncio
async def test_adapter_refusal_records_one_row_and_returns_normally(
    async_db_session, monkeypatch, mock_redis
):
    refusal = {
        "ok": False,
        "error": {
            "code": "E_INTEGRATION",
            "message": "SendGrid configuration missing: SENDGRID_API_KEY",
            "how_to_fix": "set SENDGRID_API_KEY/SENDGRID_FROM_EMAIL",
            "context": {"missing": ["SENDGRID_API_KEY"]},
        },
    }
    _send_recorder(monkeypatch, result=refusal)
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))
    task = await _make_task(async_db_session, error_message="boom")

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "failed") is None

    # COMMITTED, not merely flushed: the hook runs after the run's terminal commit and nothing
    # else commits after it — a flush-only row would be rolled back at session close.
    assert async_db_session.in_transaction() is False

    (row,) = await _activity_rows(async_db_session)
    assert row.action == "owner_notification_failed"
    assert row.agent_type == "orchestrator"
    assert row.level == "warning"
    assert f"#{task.id}" in row.summary
    assert "failed" in row.summary
    assert "E_INTEGRATION" in row.summary
    assert row.detail["task_id"] == task.id
    assert row.detail["outcome"] == "failed"
    assert row.detail["error"]["code"] == "E_INTEGRATION"


@pytest.mark.asyncio
async def test_adapter_raise_is_swallowed_and_recorded_once(async_db_session, monkeypatch, mock_redis):
    """The planted case: the adapter blows up and the run must not notice."""
    _send_recorder(monkeypatch, raises=RuntimeError("smtp exploded"))
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_completed=True))
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None

    (row,) = await _activity_rows(async_db_session)
    assert row.action == "owner_notification_failed"
    assert f"#{task.id}" in row.summary
    assert "RuntimeError" in row.summary
    assert row.detail["error"]["code"] == "RuntimeError"
    assert "smtp exploded" in row.detail["error"]["message"]


@pytest.mark.asyncio
async def test_config_read_failure_is_recorded_and_swallowed(async_db_session, monkeypatch, mock_redis):
    async def exploding(_db: Any) -> Any:
        raise PolsiaError(E_DB, "could not read company config", how_to_fix="check the database")

    monkeypatch.setattr(company_service, "get_company", exploding)
    _send_recorder(monkeypatch)
    task = await _make_task(async_db_session)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "completed") is None

    (row,) = await _activity_rows(async_db_session)
    assert row.detail["error"]["code"] == E_DB


@pytest.mark.asyncio
async def test_recording_failure_is_itself_swallowed(async_db_session, monkeypatch):
    """The deepest layer: even the failure recorder failing must not escape."""
    _send_recorder(monkeypatch, raises=RuntimeError("send exploded"))
    _patch_company(monkeypatch, _config(notification_email=OWNER, notify_on_failed=True))
    task = await _make_task(async_db_session)

    async def exploding_log(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("activity insert exploded")

    monkeypatch.setattr(notification_service, "log_activity", exploding_log)

    assert await notify_owner_for_terminal_outcome(async_db_session, task.id, "failed") is None
