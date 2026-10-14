"""V8/C5 — the support inbox 14-day ANSWER bound (code-gap §10.2; dossier §11 line 128:
"It only answers mail received in the last 14 days.").

Choice recorded (lane STATE.md): the bound is a NAMED SKIP at the attach/sweep path, not a
refusal at submit.  A submit-time refusal cannot bound a later dispatch (an item in-window at
submit can sit pending through budget denials and be answered a week later); the dossier's
sentence is about ANSWERING.  Old items stay STORED — the owner read keeps the evidence — but
they are never attached, and `_email_inbox_sweep` reports the skip by name with the bound.

The bound reuses the module's existing ordering expression `coalesce(received_at, created_at)`
on both dialects (`as_bound_instant`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.models.support import SupportInboxItem
from app.models.task import Task
from app.services.support_inbox_service import (
    ANSWER_WINDOW_DAYS,
    attach_pending,
    count_expired_pending,
    list_items,
    submit_item,
)

BASE_ITEM = dict(
    provider_message_id="msg-bound-1",
    from_address="customer@example.com",
    subject="order #1042",
    body="Where is my order?",
)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _submit(db, **over):
    async def _go():
        item = await submit_item(db, **{**BASE_ITEM, **over})
        await db.commit()
        return item

    return _go()


async def _bump_created_at(db, item, when: datetime) -> None:
    """Move `created_at` at the row level (the ordering expression's fallback column)."""
    await db.execute(
        update(SupportInboxItem)
        .where(SupportInboxItem.id == item.id)
        .values(created_at=when)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _task_count(db) -> int:
    return int((await db.scalar(select(func.count()).select_from(Task))) or 0)


# ── the constant is the documented bound ─────────────────────────────────────────────────────
def test_the_bound_is_the_documented_fourteen_days():
    assert ANSWER_WINDOW_DAYS == 14


# ── attach: inside the window ────────────────────────────────────────────────────────────────
async def test_an_item_inside_the_window_is_attached(async_db_session):
    item = await _submit(async_db_session, received_at=NOW - timedelta(days=13))

    attached = await attach_pending(async_db_session, now=NOW)
    await async_db_session.commit()

    assert len(attached) == 1
    assert attached[0]["provider_message_id"] == "msg-bound-1"
    assert item.status == "dispatched" and item.task_id == attached[0]["task_id"]
    assert await _task_count(async_db_session) == 1


# ── attach: outside the window ───────────────────────────────────────────────────────────────
async def test_an_item_outside_the_window_is_never_attached_and_stays_pending(async_db_session):
    item = await _submit(async_db_session, received_at=NOW - timedelta(days=15))

    attached = await attach_pending(async_db_session, now=NOW)
    await async_db_session.commit()

    assert attached == [], "an item older than the bound must never become a task"
    assert item.status == "pending", "it stays pending (stored evidence), just never attached"
    assert item.task_id is None
    assert await _task_count(async_db_session) == 0


async def test_the_stated_received_at_wins_over_a_recent_created_at(async_db_session):
    """The bound reads `coalesce(received_at, created_at)` — the stated arrival decides."""
    await _submit(async_db_session, received_at=NOW - timedelta(days=15))

    attached = await attach_pending(async_db_session, now=NOW)

    assert attached == []
    assert await count_expired_pending(async_db_session, now=NOW) == 1


async def test_a_recent_received_at_wins_over_an_old_created_at(async_db_session):
    """The other half of the coalesce precedence: an old row `created` later inherits recency
    from its stated arrival and is attached."""
    item = await _submit(async_db_session, received_at=NOW - timedelta(days=3))
    await _bump_created_at(async_db_session, item, NOW - timedelta(days=30))

    attached = await attach_pending(async_db_session, now=NOW)

    assert len(attached) == 1


async def test_a_null_received_at_falls_back_to_the_created_at(async_db_session):
    item = await _submit(async_db_session)  # no received_at: "not stated", never invented
    await _bump_created_at(async_db_session, item, NOW - timedelta(days=15))

    attached = await attach_pending(async_db_session, now=NOW)

    assert attached == []
    assert await count_expired_pending(async_db_session, now=NOW) == 1


async def test_the_bound_is_relative_to_the_now_it_is_given(async_db_session):
    """Determinism pin: the same row is expired for `now` and attachable for an earlier `now`."""
    item = await _submit(async_db_session, received_at=NOW - timedelta(days=13))

    too_late = await attach_pending(async_db_session, now=NOW + timedelta(days=5))
    assert too_late == []
    assert await count_expired_pending(async_db_session, now=NOW + timedelta(days=5)) == 1

    in_time = await attach_pending(async_db_session, now=NOW)
    assert len(in_time) == 1
    assert item.status == "dispatched"


# ── the owner read keeps the evidence ────────────────────────────────────────────────────────
async def test_the_owner_read_still_shows_expired_items(async_db_session):
    await _submit(async_db_session, received_at=NOW - timedelta(days=40))

    items = await list_items(async_db_session)

    assert len(items) == 1, "the bound is about answering, not about hiding stored mail"


async def test_count_expired_pending_counts_only_pending_items(async_db_session):
    item = await _submit(async_db_session, received_at=NOW - timedelta(days=15))
    assert await count_expired_pending(async_db_session, now=NOW) == 1

    item.status = "dispatched"
    await async_db_session.commit()

    assert await count_expired_pending(async_db_session, now=NOW) == 0


# ── the sweep reports the named skip (file-backed DB, the real task path) ────────────────────
def _run_sweep(qdb, monkeypatch) -> dict:
    """The real sweep body; intercept only the broker hand-off (external boundary)."""
    import celery_app.tasks.agent_tasks as agent_tasks

    async def _record(_task_ids, *, label):  # pragma: no cover - recorded, never asserted here
        return None

    monkeypatch.setattr(agent_tasks, "enqueue_created", _record)
    monkeypatch.setattr(agent_tasks, "sweep_bucket", lambda period_hours, now=None: "v8-bound")
    return qdb.run(agent_tasks._email_inbox_sweep())


def test_the_sweep_reports_the_named_skip_with_the_bound(queue_db, monkeypatch, mock_redis):
    from app.services.support_inbox_service import submit_item as submit

    old_received = datetime.now(UTC) - timedelta(days=15)

    async def _seed():
        async with queue_db.factory() as db:
            await submit(
                db,
                provider_message_id="msg-sweep-old",
                from_address="customer@example.com",
                subject="ancient order",
                body="still waiting",
                received_at=old_received,
            )
            await db.commit()

    queue_db.run(_seed())

    result = _run_sweep(queue_db, monkeypatch)

    assert result["attached"] == []
    assert result["expired"] == {
        "count": 1,
        "bound_days": 14,
        "reason": "older_than_14_days",
    }, "the skip must be NAMED with the bound stated"
    # File truth: no per-item task was created, and the item is still pending.
    assert queue_db.count("tasks", "dedupe_key LIKE 'support-inbox-%'") == 0
    row = queue_db.query("SELECT status, task_id FROM support_inbox_items")[0]
    assert row["status"] == "pending" and row["task_id"] is None


def test_the_sweep_reports_zero_expired_when_none_are_old(queue_db, monkeypatch, mock_redis):
    from app.services.support_inbox_service import submit_item as submit

    async def _seed():
        async with queue_db.factory() as db:
            await submit(
                db,
                provider_message_id="msg-sweep-fresh",
                from_address="customer@example.com",
                subject="fresh order",
                body="hello",
                received_at=datetime.now(UTC) - timedelta(days=1),
            )
            await db.commit()

    queue_db.run(_seed())

    result = _run_sweep(queue_db, monkeypatch)

    assert len(result["attached"]) == 1
    assert result["expired"]["count"] == 0
