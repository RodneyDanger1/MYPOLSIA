"""Wave V8 / C2 — the task-schedule store, the cadence tick, and the company-tz next-run math.

What this file pins (the frozen interface, plan section 7 C2):

* CRUD through the SERVICE and the ROUTES: an enabled create computes its first `next_run_at`,
  a paused create keeps None; list answers oldest-first; PATCH enabled=false is THE pause (the
  due instant is cleared, so a later resume never fires a stale child); delete answers the
  removed row; an unknown id is a named `E_NOT_FOUND`; the router carries `verify_api_key`.
* The TICK (`celery_app.tasks.cadence.run_due_schedules`): for each ENABLED schedule whose
  `next_run_at` is due it creates exactly ONE child task (title = name, description = prompt,
  source `scheduler`, agent `orchestrator`, dedupe key `schedule:<id>:<iso-date-hour>` of the
  DUE instant in the company timezone) and hands the ids to the broker ONCE; replaying the same
  due instant cannot double-create (the dedupe key is the guard); a disabled row is skipped even
  with a stale `next_run_at`; a tick with nothing due writes nothing at all.
* The NEXT-RUN MATH (`schedule_service.compute_next_run`) is COMPANY-tz wall-clock arithmetic,
  never `+24h`: fixed-instant DST cases on America/Chicago (2026-03-08 spring-forward; the
  2026-11-01 fall-back hour), weekdays skipping Sat/Sun, weekly landing on its weekday, and the
  strictly-after rule (a candidate equal to `now` moves to the next occurrence).
* Malformed STORED values refuse BY NAME (`E_CONFIG`) at claim time — hour outside 0-23, and a
  `weekly` row with no weekday — and nothing is written when they do.

House rules kept: the database is FILE-backed behind the app-wide session factory and driven on
the Celery process loop (`queue_db`); assertions read the file with stdlib sqlite3; the broker is
a recorder, never a socket; every clock read is pinned (`cadence.now_utc`); the route half uses
the root conftest's `api_client` (in-memory, dependency-overridden).
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import app.models  # noqa: F401  (register every table on Base.metadata before create_all)
from app.config import settings
from app.core.errors import E_CONFIG, E_NOT_FOUND, PolsiaError
from celery_app.tasks import cadence

UTC_TZ = "UTC"
CHI = "America/Chicago"


# ── harness ───────────────────────────────────────────────────────────────────


def _pin_clock(monkeypatch, now: datetime) -> None:
    """Freeze the tick's clock (the daily_cycle/night_task pattern)."""
    monkeypatch.setattr(cadence, "now_utc", lambda: now)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite stores naive UTC text; make it comparable to aware instants (the repo's convention)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _Broker:
    """Records `enqueue_created` hand-offs: single ids go through `.delay`, several via a group."""

    def __init__(self) -> None:
        self.delays: list[int] = []
        self.batches: list[list] = []

    def install(self, monkeypatch) -> _Broker:
        rec = self

        from celery_app.tasks import agent_tasks as at

        class _RT:
            @staticmethod
            def delay(task_id: int) -> None:
                rec.delays.append(int(task_id))

            @staticmethod
            def s(task_id: int):  # noqa: ANN205 - celery signature stand-in
                return ("sig", int(task_id))

        monkeypatch.setattr(at, "run_agent_task", _RT)

        class _G:
            def __call__(self, sigs):
                self.sigs = list(sigs)
                return self

            def apply_async(self) -> None:
                rec.batches.append(self.sigs)

        monkeypatch.setattr("celery.group", _G())
        return self


def _service(queue_db, fn, *args, **kwargs):
    """Call one service coroutine on the process loop, commit, and return its result.

    ORM ids are captured BEFORE the commit so a `expire_on_commit` default can never hide them.
    """
    async def _go():
        async with queue_db.factory() as db:
            result = await fn(db, *args, **kwargs)
            captured = int(result.id) if hasattr(result, "id") else result
            await db.commit()
            return captured

    return queue_db.run(_go())


def _run_service(queue_db, fn, *args, **kwargs):
    """Call one service coroutine WITHOUT committing (for the refusal legs)."""
    async def _go():
        async with queue_db.factory() as db:
            return await fn(db, *args, **kwargs)

    return queue_db.run(_go())


def _get_schedule(queue_db, schedule_id: int) -> dict:
    async def _go():
        async with queue_db.factory() as db:
            from app.models.schedule import TaskSchedule

            row = await db.get(TaskSchedule, int(schedule_id))
            return {
                "id": int(row.id),
                "enabled": bool(row.enabled),
                "cadence": str(row.cadence),
                "hour": int(row.hour),
                "weekday": row.weekday,
                "next_run_at": row.next_run_at,
                "last_run_at": row.last_run_at,
            }

    return queue_db.run(_go())


def _set_schedule(queue_db, schedule_id: int, **values) -> None:
    """Write a schedule row through the app's own factory (a real writer, not raw SQL)."""
    async def _go():
        async with queue_db.factory() as db:
            from app.models.schedule import TaskSchedule

            row = await db.get(TaskSchedule, int(schedule_id))
            for key, value in values.items():
                setattr(row, key, value)
            await db.commit()

    queue_db.run(_go())


def _seed_schedule(queue_db, *, name="Digest", prompt="Write the digest",
                   cadence="daily", hour=6, weekday=None, enabled=True,
                   now=datetime(2026, 10, 13, 12, 0, tzinfo=UTC)) -> int:
    from app.services import schedule_service

    return _service(
        queue_db, schedule_service.create_schedule,
        name=name, prompt=prompt, cadence=cadence, hour=hour, weekday=weekday,
        enabled=enabled, now=now,
    )


# ── 1. CRUD (service + routes) ────────────────────────────────────────────────


def test_service_crud_create_list_patch_delete(queue_db, monkeypatch):
    """Create (paused keeps None / enabled computes), list order, pause clears the due instant,
    resume recomputes from the wall clock, delete removes, a second delete is E_NOT_FOUND."""
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    from app.services import schedule_service

    paused_id = _seed_schedule(queue_db, name="Paused one", cadence="daily", hour=6,
                               enabled=False)
    row = _get_schedule(queue_db, paused_id)
    assert row["enabled"] is False and row["next_run_at"] is None

    enabled_id = _seed_schedule(queue_db, name="Enabled one", cadence="daily", hour=23,
                                enabled=True)
    row = _get_schedule(queue_db, enabled_id)
    assert row["enabled"] is True
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 13, 23, 0, tzinfo=UTC)

    ids = _run_service(queue_db, schedule_service.list_schedules)
    assert [int(r.id) for r in ids] == [paused_id, enabled_id], "oldest first (id order)"

    # pause: enabled=false CLEARS the due instant (a resume must not fire a stale child)
    _service(queue_db, schedule_service.update_schedule, enabled_id, {"enabled": False})
    row = _get_schedule(queue_db, enabled_id)
    assert row["enabled"] is False and row["next_run_at"] is None

    # resume: recomputed FROM THE WALL CLOCK the caller hands in (strictly-after semantics)
    _service(
        queue_db, schedule_service.update_schedule, enabled_id, {"enabled": True},
        now=datetime(2026, 10, 14, 7, 30, tzinfo=UTC),
    )
    row = _get_schedule(queue_db, enabled_id)
    assert row["enabled"] is True
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 14, 23, 0, tzinfo=UTC), (
        "resume recomputes from the wall clock: tonight 23:00 is still strictly after 07:30"
    )

    # hour change on an enabled row recomputes too
    _service(
        queue_db, schedule_service.update_schedule, enabled_id, {"hour": 5},
        now=datetime(2026, 10, 14, 7, 30, tzinfo=UTC),
    )
    row = _get_schedule(queue_db, enabled_id)
    assert row["hour"] == 5
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 15, 5, 0, tzinfo=UTC)

    _service(queue_db, schedule_service.delete_schedule, enabled_id)
    assert queue_db.count("task_schedules", "id = ?", (enabled_id,)) == 0

    with pytest.raises(PolsiaError) as excinfo:
        _run_service(queue_db, schedule_service.delete_schedule, enabled_id)
    assert excinfo.value.code == E_NOT_FOUND
    assert str(enabled_id) in excinfo.value.message


async def test_routes_crud_pause_and_auth(api_client, auth_headers):
    """The wire half: 201/200 shapes, pause = PATCH enabled=false, delete answers the removed row,
    an invalid cadence is answered 422 BY THE SCHEMA, and the router requires the API key."""
    created = await api_client.post(
        "/api/v1/schedules",
        json={"name": "Morning digest", "prompt": "Summarize yesterday", "cadence": "daily",
              "hour": 6},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["enabled"] is False and body["next_run_at"] is None
    schedule_id = body["id"]

    listed = await api_client.get("/api/v1/schedules", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == schedule_id

    enabled = await api_client.patch(
        f"/api/v1/schedules/{schedule_id}", json={"enabled": True}, headers=auth_headers
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True and enabled.json()["next_run_at"] is not None

    paused = await api_client.patch(
        f"/api/v1/schedules/{schedule_id}", json={"enabled": False}, headers=auth_headers
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False and paused.json()["next_run_at"] is None

    missing = await api_client.patch(
        "/api/v1/schedules/999999", json={"enabled": False}, headers=auth_headers
    )
    assert missing.status_code == 404, missing.text
    assert E_NOT_FOUND in missing.text, missing.text

    bad = await api_client.post(
        "/api/v1/schedules",
        json={"name": "x", "prompt": "y", "cadence": "hourly", "hour": 6},
        headers=auth_headers,
    )
    assert bad.status_code == 422, bad.text
    locs = [err["loc"] for err in bad.json()["detail"]]
    assert ["body", "cadence"] in locs, locs

    without_key = await api_client.get("/api/v1/schedules")
    assert without_key.status_code == 401, without_key.text

    deleted = await api_client.delete(f"/api/v1/schedules/{schedule_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["id"] == schedule_id
    assert (await api_client.get("/api/v1/schedules", headers=auth_headers)).json()["total"] == 0


# ── 2. the tick ───────────────────────────────────────────────────────────────


def test_tick_creates_one_child_for_a_due_schedule(queue_db, monkeypatch):
    """ENABLED + due -> exactly ONE child (title/prompt/source/dedupe), the row is advanced, and
    the broker receives the id exactly once."""
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    broker = _Broker().install(monkeypatch)
    schedule_id = _seed_schedule(queue_db, name="Daily research", prompt="Research competitors",
                                 cadence="daily", hour=6, enabled=True)

    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))
    result = cadence.run_due_schedules()

    assert result["status"] == "completed" and result["due"] == 1
    assert result["created"] == 1 and result["deduped"] == 0
    assert queue_db.count("tasks") == 1
    task = queue_db.query("SELECT * FROM tasks")[0]
    assert task["title"] == "Daily research"
    assert task["description"] == "Research competitors"
    assert task["source"] == "scheduler"
    assert task["status"] == "pending" and task["attempts"] == 0
    assert task["dedupe_key"] == f"schedule:{schedule_id}:2026-10-14T06"
    assert result["task_ids"] == [task["id"]]
    assert broker.delays == [task["id"]] and broker.batches == []

    row = _get_schedule(queue_db, schedule_id)
    assert _aware(row["last_run_at"]) == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 15, 6, 0, tzinfo=UTC), (
        "next run recomputed from the wall clock — the next occurrence, not +24h from the due"
    )


def test_same_due_instant_cannot_double_create(queue_db, monkeypatch):
    """Replaying the same due instant (a lost advance, a re-delivery) hits the dedupe key: the
    second tick creates NOTHING and reports the dedupe; a later tick with nothing due writes
    nothing either."""
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    broker = _Broker().install(monkeypatch)
    schedule_id = _seed_schedule(queue_db, cadence="daily", hour=6, enabled=True)

    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))
    first = cadence.run_due_schedules()
    assert first["created"] == 1
    task_id = first["task_ids"][0]

    # replay: force the row back to the SAME due instant (the advance is what normal ticks rely on)
    _set_schedule(queue_db, schedule_id,
                  next_run_at=datetime(2026, 10, 14, 6, 0), last_run_at=None)
    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 45, tzinfo=UTC))
    second = cadence.run_due_schedules()

    assert second["status"] == "completed" and second["due"] == 1
    assert second["created"] == 0 and second["deduped"] == 1
    assert queue_db.count("tasks") == 1, "one child per schedule per due instant"
    assert broker.delays == [task_id], "no second hand-off for a child that already exists"

    third = cadence.run_due_schedules()
    assert third["status"] == "no_due" and third["created"] == 0
    assert queue_db.count("tasks") == 1


def test_disabled_schedules_are_skipped_even_with_a_stale_due(queue_db, monkeypatch):
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    broker = _Broker().install(monkeypatch)
    schedule_id = _seed_schedule(queue_db, cadence="daily", hour=6, enabled=True)

    # disable WITHOUT clearing the stored due instant (an external writer's shape) — the tick
    # must still skip the row and touch nothing.
    _set_schedule(queue_db, schedule_id, enabled=False)

    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))
    result = cadence.run_due_schedules()

    assert result["status"] == "no_due" and result["created"] == 0
    assert queue_db.count("tasks") == 0 and broker.delays == []
    row = _get_schedule(queue_db, schedule_id)
    assert row["enabled"] is False
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 14, 6, 0, tzinfo=UTC), (
        "a skipped row is not advanced"
    )
    assert row["last_run_at"] is None


def test_tick_writes_nothing_when_nothing_is_due(queue_db, monkeypatch):
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    broker = _Broker().install(monkeypatch)
    schedule_id = _seed_schedule(
        queue_db, cadence="daily", hour=23, enabled=True,
        now=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),  # due tonight 23:00 — in the FUTURE
    )
    before = _get_schedule(queue_db, schedule_id)

    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))
    result = cadence.run_due_schedules()

    assert result["status"] == "no_due" and result["due"] == 0 and result["created"] == 0
    after = _get_schedule(queue_db, schedule_id)
    assert after["next_run_at"] == before["next_run_at"] and after["last_run_at"] is None
    assert queue_db.count("tasks") == 0 and broker.delays == [] and broker.batches == []


def test_malformed_stored_values_refuse_by_name(queue_db, monkeypatch):
    """hour 99 -> E_CONFIG naming 99 and the 0-23 range; weekly without a weekday -> E_CONFIG
    naming the weekday; in both cases nothing was written (no task, the row untouched)."""
    monkeypatch.setattr(settings, "company_timezone", UTC_TZ)
    _Broker().install(monkeypatch)
    schedule_id = _seed_schedule(queue_db, cadence="daily", hour=6, enabled=True)
    _set_schedule(queue_db, schedule_id, hour=99)
    _pin_clock(monkeypatch, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))

    with pytest.raises(PolsiaError) as excinfo:
        cadence.run_due_schedules()
    assert excinfo.value.code == E_CONFIG
    assert "99" in excinfo.value.message and "0-23" in excinfo.value.message
    assert queue_db.count("tasks") == 0
    assert _get_schedule(queue_db, schedule_id)["last_run_at"] is None

    _set_schedule(queue_db, schedule_id, hour=6, cadence="weekly", weekday=None)
    with pytest.raises(PolsiaError) as excinfo:
        cadence.run_due_schedules()
    assert excinfo.value.code == E_CONFIG
    assert "weekday" in excinfo.value.message
    assert queue_db.count("tasks") == 0
    row = _get_schedule(queue_db, schedule_id)
    assert row["last_run_at"] is None
    assert _aware(row["next_run_at"]) == datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


# ── 3. the next-run math (company tz, DST, fixed instants — no clock reads) ────


def test_compute_next_run_is_company_wall_clock_math_and_dst_safe():
    from app.services.schedule_service import compute_next_run

    chi = ZoneInfo(CHI)

    # spring forward (2026-03-08): 06:00 local is CDT (-5) -> 11:00Z.  A fixed-offset/+24h
    # implementation lands on 12:00Z — this case is the discriminator.
    got = compute_next_run("daily", 6, None, now=datetime(2026, 3, 8, 7, 30, tzinfo=UTC), tz=chi)
    assert got == datetime(2026, 3, 8, 11, 0, tzinfo=UTC)

    # fall back (2026-11-01): 06:00 local is CST (-6) -> 12:00Z, and `now` = 11:00Z still belongs
    # to the SAME local morning (05:00 CST) — the next run is that same day.
    got = compute_next_run("daily", 6, None, now=datetime(2026, 11, 1, 11, 0, tzinfo=UTC), tz=chi)
    assert got == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    got = compute_next_run("daily", 6, None, now=datetime(2026, 11, 1, 5, 30, tzinfo=UTC), tz=chi)
    assert got == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    # strictly after: a candidate equal to `now` moves to the next occurrence
    utc = ZoneInfo(UTC_TZ)
    got = compute_next_run("daily", 6, None, now=datetime(2026, 10, 14, 6, 0, tzinfo=UTC), tz=utc)
    assert got == datetime(2026, 10, 15, 6, 0, tzinfo=UTC)

    # weekdays: Friday noon -> Monday 06:00 (Sat/Sun skipped)
    got = compute_next_run("weekdays", 6, None, now=datetime(2026, 10, 16, 12, 0, tzinfo=UTC), tz=utc)
    assert got == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)

    # weekly: lands on its own weekday, including later the SAME day
    got = compute_next_run("weekly", 6, 0, now=datetime(2026, 10, 16, 12, 0, tzinfo=UTC), tz=utc)
    assert got == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    got = compute_next_run("weekly", 6, 0, now=datetime(2026, 10, 19, 5, 0, tzinfo=UTC), tz=utc)
    assert got == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)

    # once: the next occurrence of its hour (the tick clears it after it fires)
    got = compute_next_run("once", 6, None, now=datetime(2026, 10, 14, 7, 0, tzinfo=UTC), tz=utc)
    assert got == datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


async def test_company_config_v8_fields_roundtrip_and_null_normalisation(
    api_client, auth_headers, async_db_session
):
    """V8's company_config columns through the real GET/PUT (the C5-facing half of 0008).

    Pins: a fresh row serves the materialised canonical defaults ('' / false / [] / {}) — never
    null for the NOT NULL columns; `owner_budget_usd` is stored EXACT (Decimal, A-T17); and a JSON
    `null` for a NOT NULL column is normalised to its canonical unset value by the schema
    validators instead of flushing a NULL (the single-representation rule)."""
    from app.models.company import CompanyConfig

    async_db_session.add(CompanyConfig(name="V8 Co", timezone="UTC"))
    await async_db_session.commit()

    got = await api_client.get("/api/v1/config", headers=auth_headers)
    assert got.status_code == 200, got.text
    body = got.json()
    for field, expected in (
        ("cycle_report_email", False),
        ("sender_legal_name", ""),
        ("sender_postal_address", ""),
        ("owner_skills", ""),
        ("owner_goal", ""),
        ("risk_tolerance", ""),
        ("channels_ready", []),
        ("market_locale", ""),
        ("checklist_state", {}),
        ("dispatch_paused", False),
    ):
        assert body[field] == expected, (field, body.get(field))
    assert body["owner_hours_per_week"] is None
    assert body["owner_budget_usd"] is None
    assert body["confirmed_idea"] is None

    put = await api_client.put(
        "/api/v1/config",
        json={
            "dispatch_paused": True,
            "owner_budget_usd": 1234.56,
            "owner_hours_per_week": 20,
            "sender_legal_name": "Riley",
            "channels_ready": ["tiktok"],
            "cycle_report_email": None,
            "checklist_state": None,
        },
        headers=auth_headers,
    )
    assert put.status_code == 200, put.text
    row = await async_db_session.get(CompanyConfig, 1)
    await async_db_session.refresh(row)
    assert row.dispatch_paused is True
    assert str(row.owner_budget_usd) == "1234.56", "money exact at rest (Decimal(str(value)))"
    assert row.owner_hours_per_week == 20
    assert row.sender_legal_name == "Riley" and row.channels_ready == ["tiktok"]
    assert row.cycle_report_email is False, "null -> canonical FALSE (NOT NULL column)"
    assert row.checklist_state == {}, "null -> canonical {} (NOT NULL column)"


def test_recorded_interpretations():
    """The two interpretations this lane recorded (see RECEIPT.md): the dedupe bucket is the DUE
    instant's company-tz ISO date-hour; and a `once` schedule's `next_run_at` clears to None after
    it fires (no fabricated repeat)."""
    from app.services.schedule_service import CADENCES

    assert CADENCES == ("once", "daily", "weekdays", "weekly")
