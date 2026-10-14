"""Wave U4 (B7) — GET /api/v1/notifications projects `run_requeued` items from the
`stale_claims_requeued` activity rows the U3 producer writes.

The contract delta this file measures (old->new, recorded in the wave receipt):

    OLD  kinds = task_completed | task_failed | awaiting_approval; every item DERIVED from a
         task row; id = `task:<task_id>:<kind>`.
    NEW  + run_requeued — the SECOND projection: every `activity_log` row with
         action='stale_claims_requeued' inside the same 48 h window (the ONE batched row per
         sweep `task_service.requeue_stale_claims` writes, U3/H-02) contributes ONE item PER
         entry of `detail.requeued_ids`:
             id = `activity:<row_id>:<task_id>:run_requeued` (stable, pure function of the row)
             kind = "run_requeued"
             title = "Stalled run requeued: task #<task_id>"  (names the id)
             task_id = that id, created_at = the row's created_at
         merged with the task-derived items, newest-first, TOTAL cap 20, same window bound
         (`as_bound_instant(utcnow() - 48h, db)`), `count` still the items length.

What each block exists to catch:
  * projection — one item per requeued id, exact id/title/task_id/created_at, `failed_ids` NOT
    projected (that branch already flips the task to `failed` -> task_failed item);
  * window — the SAME 48 h bound applies to activity rows (47h59m in, 48h01m out);
  * merge/cap — activity + task items interleave by their own timestamps and the TOTAL stays 20;
  * scope — only the `stale_claims_requeued` action projects; other actions with requeue-shaped
    detail stay out;
  * hostile rows — NULL/empty/non-list/junk `detail` projections, a malformed at-rest JSON row
    (raw-SQL writer: the column is read as TEXT, parsed tolerantly — the feed never dies), and a
    requeued id whose task row no longer exists (the event is still served; no join).

Harness: the shared `api_client`/`async_db_session` fixtures (SQLite in-memory behind the app's
own session factory) — the same harness `test_u2_notifications.py` uses, so the two files measure
the same contract on the same clock.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.database import utcnow
from app.core.timeutil import as_bound_instant
from app.models.report import ActivityLog
from app.models.task import Task, TaskStatus

NOTIFICATIONS_PATH = "/api/v1/notifications"

#: The frozen item key set — unchanged by the new kind (only `kind`'s vocabulary grew).
ITEM_KEYS = {"id", "kind", "title", "task_id", "created_at"}

_DEFAULT = object()  # sentinel: "build the producer-shaped detail for me"


async def _seed_task(
    session,
    *,
    status: TaskStatus,
    minutes_ago: float,
    title: str,
    agent_type: str = "finance",
) -> Task:
    """Same seed discipline as the U2 file: dialect-bound instants, both stamps the same."""
    at = as_bound_instant(utcnow() - timedelta(minutes=minutes_ago), session)
    task = Task(title=title, agent_type=agent_type, status=status, created_at=at, updated_at=at)
    session.add(task)
    await session.commit()
    return task


async def _seed_requeue_row(
    session,
    *,
    minutes_ago: float,
    requeued_ids: list | None = None,
    failed_ids: list | None = None,
    action: str = "stale_claims_requeued",
    detail: object = _DEFAULT,
) -> ActivityLog:
    """One activity row in the exact shape `requeue_stale_claims` writes (U3/H-02)."""
    at = as_bound_instant(utcnow() - timedelta(minutes=minutes_ago), session)
    if detail is _DEFAULT:
        detail = {
            "requeued_ids": requeued_ids or [],
            "failed_ids": failed_ids or [],
            "lease_seconds": 600,
        }
    row = ActivityLog(
        agent_type="orchestrator",
        action=action,
        summary="u4 test sweep",
        level="warning",
        detail=detail,
        created_at=at,
    )
    session.add(row)
    await session.commit()
    return row


def _naive_utc(value: datetime) -> datetime:
    """The instant, not its representation (SQLite naive / PG aware — same as the U2 file)."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _raw_activity_row(session, *, minutes_ago: float, detail_text: str) -> None:
    """A row written OUTSIDE the ORM (the raw-SQL/ops writer path) — `detail` exactly as given.

    The ORM can only ever write VALID JSON (its bind processor serializes), so a malformed or
    non-dict `detail` at rest is only reachable this way.  Timestamps are formatted in the same
    `%Y-%m-%d %H:%M:%S.%f` convention the SQLAlchemy SQLite DateTime processor binds, so the
    route's dialect-bound comparison orders these rows with every other row.
    """
    stamp = as_bound_instant(utcnow() - timedelta(minutes=minutes_ago), session)
    await session.execute(
        text(
            "INSERT INTO activity_log (agent_type, action, summary, level, detail, created_at) "
            "VALUES (:agent_type, :action, :summary, :level, :detail, :created_at)"
        ),
        {
            "agent_type": "orchestrator",
            "action": "stale_claims_requeued",
            "summary": "raw writer",
            "level": "warning",
            "detail": detail_text,
            "created_at": stamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
        },
    )
    await session.commit()


# ── projection: one item per requeued id, exact shape ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_requeue_row_projects_one_item_per_requeued_id(
    api_client, auth_headers, async_db_session
):
    """A sweep row with requeued_ids [11, 12] and failed_ids [13] serves exactly two items.

    RED (pre-fix): zero items — the route derives from task status only and never reads the row.
    """
    row = await _seed_requeue_row(
        async_db_session, minutes_ago=5, requeued_ids=[11, 12], failed_ids=[13]
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["count"] == 2
    assert body["count"] == len(body["items"])

    items = body["items"]
    assert [item["kind"] for item in items] == ["run_requeued", "run_requeued"]
    # one item per requeued id, in the order the producer wrote them (one shared created_at ->
    # the stable sort keeps construction order); the failed id is NOT projected.
    assert [item["task_id"] for item in items] == [11, 12]
    assert items[0]["id"] == f"activity:{row.id}:11:run_requeued"
    assert items[1]["id"] == f"activity:{row.id}:12:run_requeued"
    for item in items:
        assert set(item) == ITEM_KEYS, "the item key set is the frozen contract"
    # the title names the task id (the owner's spec)
    assert "#11" in items[0]["title"] and "#12" in items[1]["title"]
    # created_at is the ROW's timestamp, dialect-normalised on the wire
    assert _naive_utc(datetime.fromisoformat(items[0]["created_at"])) == _naive_utc(row.created_at)

    # stable id: a pure function of the source row -> two requests agree
    again = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert [item["id"] for item in again.json()["items"]] == [item["id"] for item in items]


# ── window: the SAME 48 h bound, on the SAME helper ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_48h_window_applies_to_activity_rows_too(
    api_client, auth_headers, async_db_session
):
    """47h59m old projects; 48h01m old does not — `as_bound_instant` on the shared bound."""
    await _seed_requeue_row(async_db_session, minutes_ago=47 * 60 + 59, requeued_ids=[601])
    await _seed_requeue_row(async_db_session, minutes_ago=48 * 60 + 1, requeued_ids=[602])

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    ids = [item["task_id"] for item in resp.json()["items"]]
    assert 601 in ids, "47h59m is inside the 48 h window"
    assert 602 not in ids, "48h01m is outside the 48 h window"


# ── merge + TOTAL cap ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merges_with_task_items_newest_first_and_caps_the_total_at_20(
    api_client, auth_headers, async_db_session
):
    """25 completions + 1 requeue row -> exactly 20 items; the requeue item takes its true place.

    The 25 tasks sit at 10, 13, ... 82 minutes; the requeue row at 5 minutes.  The merged feed
    keeps the newest 20 overall: the requeue item first, then the 19 newest tasks — the oldest
    task (82 min) is pushed out by the merge, not by a source-specific cap.
    """
    seeded: list[Task] = []
    for index in range(25):
        seeded.append(
            await _seed_task(
                async_db_session,
                status=TaskStatus.COMPLETED,
                minutes_ago=10 + index * 3,
                title=f"u4 merge task {index}",
            )
        )
    await _seed_requeue_row(async_db_session, minutes_ago=5, requeued_ids=[500])

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    body = resp.json()
    items = body["items"]
    assert len(items) == 20, "the TOTAL cap is still 20"
    assert body["count"] == len(items)
    assert items[0]["kind"] == "run_requeued" and items[0]["task_id"] == 500
    assert [item["task_id"] for item in items[1:]] == [task.id for task in seeded[:19]]
    assert seeded[19].id not in [item["task_id"] for item in items], "the merged cap pushed it out"
    assert seeded[24].id not in [item["task_id"] for item in items]
    stamps = [datetime.fromisoformat(item["created_at"]) for item in items]
    assert stamps == sorted(stamps, reverse=True), "newest first across both projections"


# ── scope: only the producer's action projects ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_stale_claims_requeued_rows_project(api_client, auth_headers, async_db_session):
    """A different action carrying requeue-shaped detail projects nothing — the row must be the
    producer's own action, never any row that happens to mention ids."""
    await _seed_requeue_row(
        async_db_session, minutes_ago=5, requeued_ids=[701], action="task_completed"
    )
    await _seed_requeue_row(
        async_db_session, minutes_ago=6, requeued_ids=[702], action="morning_plan_complete"
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert resp.json() == {"items": [], "count": 0}


# ── hostile rows: un-usable detail projects nothing (never a fabricated item) ────────────────


@pytest.mark.asyncio
async def test_rows_without_a_usable_id_list_project_nothing(
    api_client, auth_headers, async_db_session
):
    """NULL detail, {}, empty list, non-list, junk-only elements -> zero items; NOT a crash.

    One row with a duplicated id ([31, 31]) proves the literal reading of "one item per task id":
    exactly ONE item, not two.
    """
    await _seed_requeue_row(async_db_session, minutes_ago=1, detail=None)
    await _seed_requeue_row(async_db_session, minutes_ago=2, detail={})
    await _seed_requeue_row(
        async_db_session,
        minutes_ago=3,
        detail={"requeued_ids": [], "failed_ids": [999], "lease_seconds": 600},
    )
    await _seed_requeue_row(async_db_session, minutes_ago=4, detail={"requeued_ids": 7})
    await _seed_requeue_row(async_db_session, minutes_ago=5, detail={"requeued_ids": True})
    await _seed_requeue_row(
        async_db_session, minutes_ago=6, detail={"requeued_ids": ["x", None, True, 2.5]}
    )

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json() == {"items": [], "count": 0}

    await _seed_requeue_row(async_db_session, minutes_ago=7, detail={"requeued_ids": [31, 31]})
    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    items = resp.json()["items"]
    assert len(items) == 1, "duplicate ids inside one row are ONE task id"
    assert items[0]["task_id"] == 31


@pytest.mark.asyncio
async def test_malformed_or_non_dict_detail_at_rest_never_kills_the_feed(
    api_client, auth_headers, async_db_session
):
    """Raw-SQL rows with malformed JSON '{not-json' and non-dict JSON '[1,2]' project NOTHING;
    an honest ORM row and a raw VALID-JSON row still serve.

    This is the measured hazard this route is built around: the ORM JSON column's result
    processor raises `json.JSONDecodeError` while FETCHING when one at-rest row is malformed —
    reading the column as text and parsing per row keeps every honest item on the wire.
    """
    await _seed_requeue_row(async_db_session, minutes_ago=10, requeued_ids=[801])
    await _raw_activity_row(async_db_session, minutes_ago=5, detail_text="{not-json")
    await _raw_activity_row(async_db_session, minutes_ago=4, detail_text="[1,2]")
    await _raw_activity_row(async_db_session, minutes_ago=3, detail_text='{"requeued_ids": [802]}')

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert sorted(item["task_id"] for item in body["items"]) == [801, 802]
    assert body["count"] == 2, "the two malformed rows contributed nothing — and nothing died"


# ── a requeued id whose task row no longer exists ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_requeued_id_with_no_live_task_row_still_serves_its_item(
    api_client, auth_headers, async_db_session
):
    """The row records the recovery EVENT; there is no join to `tasks`, so a task row that no
    longer exists cannot un-record it (the deep link is the frontend's honest no-op)."""
    await _seed_requeue_row(async_db_session, minutes_ago=5, requeued_ids=[987654])

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["task_id"] == 987654
    assert items[0]["kind"] == "run_requeued"


# ── coexistence: both projections on one feed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_items_and_requeue_items_coexist_newest_first(
    api_client, auth_headers, async_db_session
):
    """A completion at 15 min and a requeue at 5 min -> both kinds, requeue first."""
    await _seed_task(
        async_db_session, status=TaskStatus.COMPLETED, minutes_ago=15, title="Keep shipping"
    )
    await _seed_requeue_row(async_db_session, minutes_ago=5, requeued_ids=[901])

    resp = await api_client.get(NOTIFICATIONS_PATH, headers=auth_headers)
    items = resp.json()["items"]
    assert [item["kind"] for item in items] == ["run_requeued", "task_completed"]
    assert [item["task_id"] for item in items[0:1]] == [901]
    assert items[1]["title"] == "Keep shipping"
