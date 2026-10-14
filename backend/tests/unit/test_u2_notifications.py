"""Wave U2 (B1) — GET /api/v1/notifications: the bell's task-derived feed, pinned to the frozen contract.

The contract this file measures (both seats were briefed with the same text):

    GET /api/v1/notifications (same X-API-Key auth as every route)
    200 {"items": [...], "count": N} — items newest first, at most 20, window = last 48 hours.
    each item: id (stable string), kind (task_completed | task_failed | awaiting_approval),
               title (the task's title), task_id (int), created_at (ISO-8601).
    completed -> task_completed with created_at = the completion timestamp; failed -> task_failed;
    awaiting_approval stays awaiting_approval.  Derived ONLY from existing task rows.

What each block exists to catch:
  * auth — the route carries the shipped `verify_api_key` dependency (401, E_AUTH envelope);
  * shape/id — the exact item key set, a string id that is a PURE FUNCTION of the row (two
    requests agree), and an ISO-8601 created_at that parses;
  * kind mapping — completed/failed/awaiting_approval each land their own kind, and the
    non-event statuses (pending/in_progress) produce nothing;
  * window — a task finished 47h59m ago is inside the last 48 h and one at 48h01m is out; the
    window and the ORDER BY run on the SAME expression (task_service.task_terminal_at_expr);
  * limit/order — 25 in-window completions serve exactly the newest 20, newest first, and
    `count` equals the items length.

RED-leg evidence (each planted, observed, restored; raw output in the lane's receipt):
  * LIMIT widened 20 -> 50  => `test_limit_is_20_and_newest_first` fails (25 items served);
  * the 48 h predicate dropped => `test_window_is_the_last_48_hours` fails (the old row appears);
  * the kind map broken (`completed` -> "task_failed") => the kind tests fail by name.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.database import utcnow
from app.core.timeutil import as_bound_instant
from app.models.task import Task, TaskStatus

NOTIFICATIONS_PATH = "/api/v1/notifications"

#: The frozen item key set — a shape change is a contract change.
ITEM_KEYS = {"id", "kind", "title", "task_id", "created_at"}


async def _seed_task(
    session,
    *,
    status: TaskStatus,
    minutes_ago: float,
    title: str,
    agent_type: str = "finance",
) -> Task:
    """Insert a task whose `updated_at`/`created_at` are `minutes_ago` in the past, dialect-bound.

    `as_bound_instant` is the repo's own binding rule (SQLite stores naive UTC text; PostgreSQL
    keeps an aware instant) — the same call the route makes for its window bound, so the seed and
    the query cannot disagree about the clock.
    """
    at = as_bound_instant(utcnow() - timedelta(minutes=minutes_ago), session)
    task = Task(title=title, agent_type=agent_type, status=status, created_at=at, updated_at=at)
    session.add(task)
    await session.commit()
    return task


def _naive_utc(value: datetime) -> datetime:
    """The value in UTC wall-clock form, tz-aware or naive — SQLite serves naive UTC text,
    PostgreSQL an aware instant, and the in-process identity map can hold either; the instant
    is what the contract asserts, not its representation."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


# ── auth ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_route_requires_the_api_key(api_client):
    """No key -> 401 with the named E_AUTH refusal envelope (same shape as every sibling route)."""
    resp = await api_client.get(NOTIFICATIONS_PATH)
    assert resp.status_code == 401, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_AUTH"

    wrong = await api_client.get(NOTIFICATIONS_PATH, headers={"X-API-Key": "definitely-wrong"})
    assert wrong.status_code == 401


# ── the empty case ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_database_answers_an_honest_empty_feed(api_client, auth_headers):
    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json() == {"items": [], "count": 0}


# ── shape, stable id, ISO timestamp ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_item_shape_and_a_stable_string_id(api_client, auth_headers, async_db_session):
    task = await _seed_task(
        async_db_session, status=TaskStatus.COMPLETED, minutes_ago=5, title="Shipped the report"
    )

    first = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    second = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert first.status_code == 200, first.text[:300]

    items = first.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert set(item) == ITEM_KEYS, "the item key set is the frozen contract"
    assert isinstance(item["id"], str) and item["id"]
    assert isinstance(item["task_id"], int) and item["task_id"] == task.id
    assert item["title"] == "Shipped the report"
    # stable: the id is a pure function of the row, so two requests agree (a client may key on it)
    assert second.json()["items"][0]["id"] == item["id"]
    # ISO-8601: it parses back into the instant the row carries
    parsed = datetime.fromisoformat(item["created_at"])
    assert _naive_utc(parsed) == _naive_utc(task.updated_at)


# ── kind mapping ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_task_maps_to_task_completed_with_its_completion_time(
    api_client, auth_headers, async_db_session
):
    """The completion timestamp is the one the repo's own writer stamps (`update_task_status`
    sets `updated_at` at the transition) — not `created_at` and not "now"."""
    from app.services.task_service import create_task, update_task_status

    task = await create_task(async_db_session, title="Finishes now", agent_type="finance")
    await update_task_status(async_db_session, task.id, "completed", result_summary="done")
    await async_db_session.commit()
    completed_write = task.updated_at

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    item = resp.json()["items"][0]
    assert item["kind"] == "task_completed"
    assert item["task_id"] == task.id
    assert item["title"] == "Finishes now"
    assert _naive_utc(datetime.fromisoformat(item["created_at"])) == _naive_utc(completed_write)


@pytest.mark.asyncio
async def test_failed_and_awaiting_approval_land_their_own_kinds(
    api_client, auth_headers, async_db_session
):
    failed = await _seed_task(
        async_db_session, status=TaskStatus.FAILED, minutes_ago=10, title="Blew up"
    )
    parked = await _seed_task(
        async_db_session, status=TaskStatus.AWAITING_APPROVAL, minutes_ago=20, title="Waiting on you"
    )
    pending = await _seed_task(
        async_db_session, status=TaskStatus.PENDING, minutes_ago=30, title="Not an event"
    )
    running = await _seed_task(
        async_db_session, status=TaskStatus.IN_PROGRESS, minutes_ago=40, title="Still going"
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    kinds = {item["task_id"]: item["kind"] for item in resp.json()["items"]}
    assert kinds == {failed.id: "task_failed", parked.id: "awaiting_approval"}
    assert pending.id not in kinds and running.id not in kinds, (
        "pending/in_progress are not notification events"
    )


# ── window ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_is_the_last_48_hours(api_client, auth_headers, async_db_session):
    inside = await _seed_task(
        async_db_session, status=TaskStatus.COMPLETED, minutes_ago=47 * 60 + 59, title="Just inside"
    )
    outside = await _seed_task(
        async_db_session, status=TaskStatus.COMPLETED, minutes_ago=48 * 60 + 1, title="Just outside"
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    ids = [item["task_id"] for item in resp.json()["items"]]
    assert inside.id in ids, "47h59m is inside the 48 h window"
    assert outside.id not in ids, "48h01m is outside the 48 h window"


# ── limit, order, count ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_is_20_and_newest_first_with_count_equal_to_items(
    api_client, auth_headers, async_db_session
):
    seeded: list[Task] = []
    for index in range(25):  # index 0 = newest; all inside the window (25 * 3 min < 48 h)
        seeded.append(
            await _seed_task(
                async_db_session,
                status=TaskStatus.COMPLETED,
                minutes_ago=index * 3,
                title=f"task {index}",
            )
        )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    body = resp.json()
    items = body["items"]
    assert len(items) == 20, "the limit is 20"
    assert body["count"] == len(items), "count equals the items length"
    expected = [task.id for task in seeded[:20]]  # the newest 20, newest first
    assert [item["task_id"] for item in items] == expected
    stamps = [datetime.fromisoformat(item["created_at"]) for item in items]
    assert stamps == sorted(stamps, reverse=True), "newest first"


@pytest.mark.asyncio
async def test_newest_first_holds_across_kinds(api_client, auth_headers, async_db_session):
    oldest = await _seed_task(
        async_db_session, status=TaskStatus.AWAITING_APPROVAL, minutes_ago=30, title="oldest"
    )
    middle = await _seed_task(
        async_db_session, status=TaskStatus.FAILED, minutes_ago=20, title="middle"
    )
    newest = await _seed_task(
        async_db_session, status=TaskStatus.COMPLETED, minutes_ago=10, title="newest"
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert [item["task_id"] for item in resp.json()["items"]] == [newest.id, middle.id, oldest.id]
    assert resp.json()["count"] == 3
