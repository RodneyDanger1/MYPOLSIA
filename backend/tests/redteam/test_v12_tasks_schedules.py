"""V12 red-team — task status writes + schedules CRUD under attack.

Surfaces: `PATCH /api/v1/tasks/{task_id}/status` (`app/api/v1/tasks.py:131`) and
`/api/v1/schedules` CRUD (`app/api/v1/schedules.py` + `app/services/schedule_service.py`).

Attack list — tasks:
  1. unauthenticated status write
  2. nonexistent task id
  3. invalid status VALUE (the guard that exists: `VALID_TASK_STATUSES`)
  4. malformed status payloads (wrong JSON types)
  5. nothing is written by a refused call (row state re-read)

Attack list — schedules:
  6. invalid cadence / hour / weekday payloads (schema AND service belt)
  7. oversized prompt strings vs the shipped store (round-trip integrity)
  8. unknown schedule id (PATCH/DELETE)
  9. delete-during-run semantics (claim -> delete -> no ghost claim)
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import E_NOT_FOUND, E_VALIDATION, PolsiaError
from app.models.schedule import TaskSchedule
from app.models.task import Task

TASKS = "/api/v1/tasks"
SCHEDULES = "/api/v1/schedules"


async def _seed_task(db, status: str = "pending") -> Task:
    task = Task(title="V12 redteam task", agent_type="finance", source="manual", status=status)
    db.add(task)
    await db.commit()
    return task


async def _task_status(db, task_id: int) -> str:
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalars().one()
    return str(row.status)


# ── 1. unauthenticated ───────────────────────────────────────────────────────────────────────


async def test_task_status_write_needs_the_owner_key(api_client, async_db_session):
    """ATTACK: flip a task's status with no X-API-Key at all."""
    task = await _seed_task(async_db_session)
    resp = await api_client.patch(f"{TASKS}/{task.id}/status", json={"status": "completed"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"
    assert await _task_status(async_db_session, task.id) == "pending"


# ── 2. nonexistent task ──────────────────────────────────────────────────────────────────────


async def test_status_write_to_a_nonexistent_task_is_404(api_client, auth_headers):
    resp = await api_client.patch(
        f"{TASKS}/999999/status", json={"status": "completed"}, headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == E_NOT_FOUND


# ── 3. invalid status value ──────────────────────────────────────────────────────────────────


async def test_invalid_status_value_is_refused_by_name_and_writes_nothing(
    api_client, auth_headers, async_db_session,
):
    """ATTACK: set the status to a value the enum does not carry.

    The service's `VALID_TASK_STATUSES` is the authority: E_VALIDATION (422) with the accepted
    set in the context — and the seeded row must still carry its original status afterwards.
    """
    task = await _seed_task(async_db_session)
    resp = await api_client.patch(
        f"{TASKS}/{task.id}/status", json={"status": "burned"}, headers=auth_headers
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["status"] == "burned"
    assert "completed" in body["error"]["context"]["valid"]
    assert await _task_status(async_db_session, task.id) == "pending"


async def test_malformed_status_payloads_are_422(api_client, auth_headers, async_db_session):
    """ATTACK: send a null / list / object as `status` — the boundary must refuse, not coerce."""
    task = await _seed_task(async_db_session)
    for payload in ({"status": None}, {"status": ["pending"]}, {"status": {"$": "completed"}}, {}):
        resp = await api_client.patch(
            f"{TASKS}/{task.id}/status", json=payload, headers=auth_headers
        )
        assert resp.status_code == 422, payload
    assert await _task_status(async_db_session, task.id) == "pending"


async def test_a_valid_status_write_applies_and_omitted_messages_survive(
    api_client, auth_headers, async_db_session,
):
    """CONTROL: the guard must not be an over-refuser — a valid value writes; an omitted
    `error_message` does not wipe a previously recorded one (the service's rule)."""
    task = await _seed_task(async_db_session)
    first = await api_client.patch(
        f"{TASKS}/{task.id}/status",
        json={"status": "failed", "error_message": "provider refused"},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    second = await api_client.patch(
        f"{TASKS}/{task.id}/status", json={"status": "pending"}, headers=auth_headers
    )
    assert second.status_code == 200, second.text
    row = (await async_db_session.execute(select(Task).where(Task.id == task.id))).scalars().one()
    assert str(row.status) == "pending"
    assert row.error_message == "provider refused"


# ── 6. schedules: invalid payloads ───────────────────────────────────────────────────────────


async def test_invalid_cadence_is_refused_and_creates_nothing(api_client, auth_headers, async_db_session):
    for cadence in ("hourly", "every day", "WEEKLY", ""):
        resp = await api_client.post(
            SCHEDULES,
            json={"name": "bad", "prompt": "p", "cadence": cadence, "hour": 9},
            headers=auth_headers,
        )
        assert resp.status_code == 422, cadence
    count = (await async_db_session.execute(select(func.count()).select_from(TaskSchedule))).scalar_one()
    assert count == 0


async def test_invalid_hour_and_weekday_values_are_refused(api_client, auth_headers):
    for payload in (
        {"name": "bad", "prompt": "p", "cadence": "daily", "hour": 24},
        {"name": "bad", "prompt": "p", "cadence": "daily", "hour": -1},
        {"name": "bad", "prompt": "p", "cadence": "daily", "hour": 9, "weekday": 7},
    ):
        resp = await api_client.post(SCHEDULES, json=payload, headers=auth_headers)
        assert resp.status_code == 422, payload


async def test_weekly_without_a_weekday_is_refused_by_the_service_belt(api_client, auth_headers):
    """The wire schema lets `weekday` be null; the service belt refuses the combination BY NAME."""
    resp = await api_client.post(
        SCHEDULES,
        json={"name": "weekly", "prompt": "p", "cadence": "weekly", "hour": 9},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["field"] == "weekday"


async def test_service_belt_refuses_boolean_hour_and_weekday(async_db_session):
    """ATTACK: `True` is an int in Python — the service belt must refuse it as a wall-clock hour."""
    from app.services import schedule_service as schedules

    for kwargs in ({"hour": True}, {"hour": 9, "weekday": True}):
        with pytest.raises(PolsiaError) as refusal:
            await schedules.create_schedule(
                async_db_session, name="bool", prompt="p", cadence="daily", **kwargs
            )
        assert refusal.value.code == E_VALIDATION


# ── 7. oversized prompt strings ──────────────────────────────────────────────────────────────


async def test_oversized_prompt_is_stored_without_truncation_or_a_crash(
    api_client, auth_headers, async_db_session,
):
    """ATTACK: a ~300k-char prompt.

    The tree's shipped posture (measured here): the schema/column carry no length cap (the
    column is TEXT; the 10 MB CLI stdin cap is the downstream named refusal — E_PROMPT_TOO_LARGE
    in `app/agents/claude_cli.py:169`).  What MUST hold either way: no 500, and if the row is
    accepted its prompt round-trips byte-for-byte (a silent truncation would corrupt the
    owner's schedule without any error).
    """
    big = "x" * 300_000
    resp = await api_client.post(
        SCHEDULES,
        json={"name": "big", "prompt": big, "cadence": "daily", "hour": 9},
        headers=auth_headers,
    )
    assert resp.status_code in (201, 422), resp.text
    if resp.status_code == 201:
        row = (
            await async_db_session.execute(select(TaskSchedule).where(TaskSchedule.name == "big"))
        ).scalars().one()
        assert len(row.prompt) == len(big)          # no silent truncation
        assert row.prompt == big


# ── 8. unknown schedule id ───────────────────────────────────────────────────────────────────


async def test_unknown_schedule_id_is_404_for_patch_and_delete(api_client, auth_headers):
    patch = await api_client.patch(SCHEDULES + "/424242", json={"enabled": False}, headers=auth_headers)
    assert patch.status_code == 404
    assert patch.json()["error"]["code"] == E_NOT_FOUND
    delete = await api_client.delete(SCHEDULES + "/424242", headers=auth_headers)
    assert delete.status_code == 404
    assert delete.json()["error"]["code"] == E_NOT_FOUND


# ── 9. delete-during-run ─────────────────────────────────────────────────────────────────────


async def test_delete_after_a_claim_leaves_no_ghost_claim(async_db_session):
    """ATTACK/SEMANTICS: claim a due schedule, delete the row, claim again.

    The tick claim is a DB read; a row deleted after the claim is gone for the NEXT tick (no
    ghost re-fire after delete).  The already-issued claim stays a plain data record — the
    invariant is that a deleted schedule can never be claimed AGAIN.
    """
    from app.services import schedule_service as schedules

    row = await schedules.create_schedule(
        async_db_session, name="due-now", prompt="do it", cadence="daily", hour=9, enabled=True
    )
    row.next_run_at = datetime(2020, 1, 1, 9, 0)     # far past: due
    await async_db_session.flush()

    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    first = await schedules.claim_due_schedules(async_db_session, now=now)
    assert [claim.schedule_id for claim in first] == [row.id]
    assert first[0].dedupe_key.startswith(f"schedule:{row.id}:")

    await schedules.delete_schedule(async_db_session, row.id)
    second = await schedules.claim_due_schedules(async_db_session, now=now)
    assert second == [], "a deleted schedule was claimed again — ghost re-fire"
