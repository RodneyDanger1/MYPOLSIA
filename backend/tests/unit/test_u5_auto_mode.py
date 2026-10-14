"""Wave U5 (P2/B22) — Auto Mode: the bounded window, its three routes, its worker hooks.

The contract this file measures (stated identically in `app/services/auto_mode_service.py`,
`app/api/v1/auto_mode.py` and the wave-U5 plan):

    POST   /api/v1/auto-mode  {"duration_hours": 1..120} -> 201 {"started": true, "session": V}
    GET    /api/v1/auto-mode                             -> 200 {"active": bool, "session": V|null}
    DELETE /api/v1/auto-mode                             -> 200 {"stopped": true, "session": V}

    V = {id, started_at, ends_at, stopped_at, stop_reason, tasks_run, spend_used, time_left_seconds}

What each block exists to catch:
  * the window — one active session; a second start refuses E_VALIDATION and adds NO row;
  * duration bounds — 1 hour..5 days; outside is the named E_VALIDATION refusal;
  * honest counters — `spend_used` is None when nothing was measured (never 0.00), a measured
    sum when there is one; `tasks_run` only moves when the worker's terminal write lands inside
    a covering window;
  * lazy expiry — an expired open row is closed on the next read with `duration_end` (and its
    final spend snapshotted);
  * the gates — a scheduled dispatch denied by the day budget closes the live window with
    `day_cap`; a MANUAL override (T04) does NOT close it (the gate interrupts, the owner's own
    trigger proceeds — nothing here bypasses a gate); approval parking is untouched;
  * refusals — named E_VALIDATION envelopes (422 + X-Polsia-Error-Code), never silent successes;
  * the HTTP surface — POST/GET/DELETE exist behind the API key; no key -> 401.

Worker legs run on the file-backed `queue_db` (the celery conftest) so the UPDATE is an
assertion about the row on disk, and `_execute_agent_task` is driven through its real path —
the terminal branch's notification leg (`notify_owner_for_terminal_outcome`, P5) runs UNPATCHED
as the parent's probe requires; this file must not (and does not) test P5's module.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import utcnow
from app.models.auto_mode import AutoModeSession
from app.models.task import AgentRun


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """The same seam as `tests/unit/celery/conftest.py`'s `quiet_redis`, mirrored here.

    The file's worker legs run the real `_execute_agent_task`, which logs activity; the root
    conftest resets the publish breaker around EVERY test, so without this mock each worker leg
    would pay a real Redis connect attempt (measured here: a connect timeout per test, with the
    traceback noise polluting the output even though the DB write is unaffected).
    """
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _seed_session(
    queue_db,
    *,
    starts_in: timedelta = timedelta(hours=-1),
    ends_in: timedelta = timedelta(hours=4),
) -> int:
    """A session window around now, written through the app's own factory/loop."""

    async def _seed() -> int:
        async with queue_db.factory() as db:
            now = utcnow()
            row = AutoModeSession(started_at=now + starts_in, ends_at=now + ends_in)
            db.add(row)
            await db.commit()
            return int(row.id)

    return queue_db.run(_seed())


def _session_rows(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT id, started_at, ends_at, stopped_at, stop_reason, tasks_run, spend_usd "
        "FROM auto_mode_sessions ORDER BY id"
    )


def _create_task_with_source(queue_db, *, title: str, agent_type: str, source: str) -> int:
    """The T04 helper shape: the real `source` values the gate keys on."""

    async def _make() -> int:
        from app.services.task_service import create_task

        async with queue_db.factory() as db:
            task = await create_task(db, title=title, agent_type=agent_type, source=source)
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _fill_the_day(queue_db, agent_type: str = "social_media") -> float:
    """Seed measured spend for today at the declared day limit (the policy's own number)."""
    from app.agents.accounting import budget_policy_from_settings

    policy = budget_policy_from_settings()

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(
                AgentRun(
                    agent_type=agent_type,
                    cost_usd=Decimal(str(policy.day_limit_usd)),
                    started_at=utcnow(),
                )
            )
            await db.commit()

    queue_db.run(_seed())
    return float(policy.day_limit_usd)


# ── 1. the HTTP surface: start / status / stop ───────────────────────────────────────────────


async def test_start_status_stop_round_trip(api_client, auth_headers):
    started = await api_client.post(
        "/api/v1/auto-mode", json={"duration_hours": 3}, headers=auth_headers
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["started"] is True
    session = body["session"]
    assert session["stop_reason"] is None and session["stopped_at"] is None
    assert session["tasks_run"] == 0
    assert session["spend_used"] is None, "nothing ran yet — unmeasured, never 0.00"
    # time_left counts down from 3 h (never above the requested window, never negative)
    assert 0 < session["time_left_seconds"] <= 3 * 3600

    status = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert status.status_code == 200, status.text
    status_body = status.json()
    assert status_body["active"] is True
    assert status_body["session"]["id"] == session["id"]
    assert status_body["session"]["time_left_seconds"] <= session["time_left_seconds"]

    stopped = await api_client.delete("/api/v1/auto-mode", headers=auth_headers)
    assert stopped.status_code == 200, stopped.text
    stop_body = stopped.json()
    assert stop_body["stopped"] is True
    assert stop_body["session"]["stop_reason"] == "user_stop"
    assert stop_body["session"]["stopped_at"] is not None
    assert stop_body["session"]["time_left_seconds"] is None, "a closed window has no time left"

    after = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert after.json() == {"active": False, "session": None}


async def test_second_start_refuses_e_validation_and_writes_no_row(api_client, auth_headers, async_db_session):
    first = await api_client.post(
        "/api/v1/auto-mode", json={"duration_hours": 1}, headers=auth_headers
    )
    assert first.status_code == 201

    second = await api_client.post(
        "/api/v1/auto-mode", json={"duration_hours": 1}, headers=auth_headers
    )
    assert second.status_code == 422, second.text
    refusal = second.json()
    assert refusal["ok"] is False
    assert refusal["error"]["code"] == "E_VALIDATION"
    assert "already active" in refusal["error"]["message"]
    assert second.headers["x-polsia-error-code"] == "E_VALIDATION"

    rows = (await async_db_session.execute(select(AutoModeSession))).scalars().all()
    assert len(rows) == 1, "a refused start must never create a second window"


async def test_duration_outside_one_hour_to_five_days_refuses(api_client, auth_headers, async_db_session):
    for hours in (0.5, 121, 0):
        resp = await api_client.post(
            "/api/v1/auto-mode", json={"duration_hours": hours}, headers=auth_headers
        )
        assert resp.status_code == 422, f"duration_hours={hours} must refuse"
        assert resp.json()["error"]["code"] == "E_VALIDATION"
    rows = (await async_db_session.execute(select(AutoModeSession))).scalars().all()
    assert rows == [], "no refused duration may leave a row behind"


async def test_status_with_nothing_active_is_explicit(api_client, auth_headers):
    resp = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"active": False, "session": None}


async def test_stop_with_nothing_active_refuses_by_name(api_client, auth_headers):
    resp = await api_client.delete("/api/v1/auto-mode", headers=auth_headers)
    assert resp.status_code == 422, resp.text
    refusal = resp.json()
    assert refusal["ok"] is False
    assert refusal["error"]["code"] == "E_VALIDATION"
    assert "no Auto Mode session is active" in refusal["error"]["message"]


async def test_the_route_requires_the_api_key(api_client):
    for method, kwargs in (("get", {}), ("delete", {})):
        resp = await getattr(api_client, method)("/api/v1/auto-mode", **kwargs)
        assert resp.status_code == 401, resp.text
    resp = await api_client.post("/api/v1/auto-mode", json={"duration_hours": 1})
    assert resp.status_code == 401, resp.text


# ── 2. the live counters ─────────────────────────────────────────────────────────────────────


async def test_spend_used_is_null_until_measured_then_the_real_sum(api_client, auth_headers, async_db_session):
    resp = await api_client.post(
        "/api/v1/auto-mode", json={"duration_hours": 2}, headers=auth_headers
    )
    assert resp.status_code == 201
    assert resp.json()["session"]["spend_used"] is None

    # a run whose cost was NOT measured (NULL cost) keeps the counter unmeasured — never 0.00
    async_db_session.add(AgentRun(agent_type="social_media", cost_usd=None, started_at=utcnow()))
    await async_db_session.commit()
    unmeasured = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert unmeasured.json()["session"]["spend_used"] is None, "NULL cost is not a measured zero"

    # a measured run inside the window: the live sum is exact Decimal arithmetic on the wire
    async_db_session.add(
        AgentRun(agent_type="social_media", cost_usd=Decimal("1.2500"), started_at=utcnow())
    )
    await async_db_session.commit()
    measured = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert measured.json()["session"]["spend_used"] == 1.25


async def test_expired_window_closes_lazily_on_read_with_duration_end(api_client, auth_headers, async_db_session):
    now = utcnow()
    row = AutoModeSession(started_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1))
    async_db_session.add(row)
    async_db_session.add(
        AgentRun(agent_type="social_media", cost_usd=Decimal("2.5000"),
                 started_at=now - timedelta(hours=1, minutes=30))
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/auto-mode", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"active": False, "session": None}

    closed = (
        await async_db_session.execute(select(AutoModeSession).where(AutoModeSession.id == row.id))
    ).scalar_one()
    assert closed.stopped_at is not None, "the read must close the expired window"
    assert closed.stop_reason == "duration_end"
    assert closed.spend_usd == Decimal("2.5000"), "the close snapshots the final measured spend"


# ── 3. the worker hooks (file-backed queue_db; the real _execute_agent_task path) ─────────────


def test_a_finished_run_inside_the_window_increments_tasks_run_once(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    session_id = _seed_session(queue_db)
    task_id = _create_task_with_source(queue_db, title="U5 auto-mode count", agent_type="social_media", source="scheduler")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed", outcome
    rows = _session_rows(queue_db)
    assert rows[0]["id"] == session_id
    assert rows[0]["tasks_run"] == 1, "the terminal write inside the window counts once"
    assert rows[0]["stopped_at"] is None, "a covering window stays open"


def test_a_run_finishing_outside_the_window_does_not_increment(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    # an EXPIRED window, still open in bookkeeping terms (nobody has read status since)
    _seed_session(queue_db, starts_in=timedelta(hours=-10), ends_in=timedelta(hours=-5))
    task_id = _create_task_with_source(queue_db, title="U5 outside window", agent_type="social_media", source="scheduler")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed", outcome
    rows = _session_rows(queue_db)
    assert rows[0]["tasks_run"] == 0, "a terminal write outside the window must not count"
    assert rows[0]["stopped_at"] is None, "the worker never labels an expiry — the read does"


def test_budget_denial_closes_the_live_window_with_day_cap(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    _seed_session(queue_db)
    day_limit = _fill_the_day(queue_db)
    task_id = _create_task_with_source(queue_db, title="U5 day_cap close", agent_type="finance", source="scheduler")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "skipped" and outcome["task_status"] == "rejected", outcome
    rows = _session_rows(queue_db)
    assert rows[0]["stopped_at"] is not None, "the denial closes the live window"
    assert rows[0]["stop_reason"] == "day_cap"
    assert rows[0]["spend_usd"] is not None
    assert abs(float(rows[0]["spend_usd"]) - day_limit) < 1e-6, "final spend snapshotted at close"


def test_budget_denial_leaves_an_expired_window_alone(queue_db, install_run_spy):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    _seed_session(queue_db, starts_in=timedelta(hours=-10), ends_in=timedelta(hours=-5))
    _fill_the_day(queue_db)
    task_id = _create_task_with_source(queue_db, title="U5 day_cap stale", agent_type="finance", source="scheduler")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["task_status"] == "rejected", outcome
    rows = _session_rows(queue_db)
    assert rows[0]["stopped_at"] is None, "an expired window is not relabelled day_cap"
    assert rows[0]["stop_reason"] is None


def test_manual_override_does_not_close_the_window_but_still_counts(queue_db, install_run_spy):
    """T04 rides through: the owner's manual trigger proceeds past the cap — the gate must not
    close the window on that path, and the run that really finished inside the window counts."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(delegate=False)
    _seed_session(queue_db)
    _fill_the_day(queue_db)
    task_id = _create_task_with_source(queue_db, title="U5 manual over cap", agent_type="finance", source="manual")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed" and outcome["manual_override"] is True, outcome
    rows = _session_rows(queue_db)
    assert rows[0]["stopped_at"] is None, "the recorded override path never closes the session"
    assert rows[0]["tasks_run"] == 1, "the override run DID finish inside the window"


def test_a_parked_run_still_counts_and_keeps_its_parking(queue_db, install_run_spy):
    """Approval parking is untouched (gates park, never bypass): the task lands
    `awaiting_approval` and the window counts the terminal write."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    install_run_spy(
        delegate=False,
        result={
            "summary": "drafted an external action",
            "side_effects": [
                {
                    "decision": "require_approval",
                    "fingerprint": "fp-u5-test",
                    "action": "post",
                    "channel": "linkedin",
                    "reason": "external action needs the owner's green light",
                }
            ],
        },
    )
    _seed_session(queue_db)
    task_id = _create_task_with_source(queue_db, title="U5 parked run", agent_type="social_media", source="scheduler")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval", outcome
    assert queue_db.row(task_id)["status"] == "awaiting_approval"
    rows = _session_rows(queue_db)
    assert rows[0]["tasks_run"] == 1
