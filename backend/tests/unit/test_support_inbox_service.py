"""R17 — `support_inbox_service`: submit / list / attach on the real ORM (in-memory SQLite).

Drives the REAL service (the only writer of `support_inbox_items`) against the REAL models and
the REAL deduped-task path (`celery_app.tasks._common.create_task_deduped`) — no doubles: the
dedupe hit, the dispatched mark and the flush-only discipline are all asserted on rows the same
session would have committed.

The chain these legs feed (submit -> attach -> agent prompt) lives end to end in
`tests/unit/celery/test_support_inbox_sweep.py`; this file pins the service contract itself.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import E_VALIDATION, PolsiaError
from app.models.support import SupportInboxItem
from app.models.task import Task
from app.services.support_inbox_service import (
    VALID_STATUSES,
    attach_pending,
    inbox_task_dedupe_key,
    list_items,
    submit_item,
)

BASE_ITEM = dict(
    provider_message_id="msg-svc-1",
    from_address="customer@example.com",
    subject="order #1042",
    body="Where is my order #1042? It was due Tuesday.",
)


async def _count(db, model) -> int:
    return int((await db.scalar(select(func.count()).select_from(model))) or 0)


async def _tasks(db) -> list[Task]:
    return list(
        (await db.execute(select(Task).order_by(Task.id.asc()))).scalars().all()
    )


# ── submit ──────────────────────────────────────────────────────────────────────────────────
async def test_submit_stores_a_pending_item_with_no_invented_arrival(async_db_session):
    item = await submit_item(async_db_session, **BASE_ITEM)

    assert item.id is not None and item.provider_message_id == "msg-svc-1"
    assert item.from_address == "customer@example.com"
    assert item.subject == "order #1042"
    assert item.body == BASE_ITEM["body"]
    assert item.status == "pending", "a stored item must be attachable by the sweep"
    assert item.task_id is None, "no task exists yet — nothing may be fabricated"
    assert item.received_at is None, "not stated -> NULL, never an invented arrival instant"


async def test_submit_stores_an_aware_received_at_dialect_bound(async_db_session):
    received = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    item = await submit_item(async_db_session, **{**BASE_ITEM, "received_at": received})

    # SQLite stores naive UTC text (the claim path's binding rule); the value round-trips exactly.
    assert item.received_at is not None
    assert item.received_at.replace(tzinfo=UTC) == received


@pytest.mark.parametrize("field", ["provider_message_id", "from_address", "subject", "body"])
@pytest.mark.parametrize("value", ["", "   "])
async def test_blank_fields_are_named_refusals(async_db_session, field, value):
    payload = {**BASE_ITEM, field: value}
    with pytest.raises(PolsiaError) as excinfo:
        await submit_item(async_db_session, **payload)
    assert excinfo.value.code == E_VALIDATION
    assert field in excinfo.value.message, "the refusal must name the field"


async def test_an_oversize_provider_id_is_refused_so_the_key_can_never_truncate(
    async_db_session,
):
    """`support-inbox-` + 240 chars <= 255 (tasks.dedupe_key); 241 must refuse by name."""
    with pytest.raises(PolsiaError) as excinfo:
        await submit_item(async_db_session, **{**BASE_ITEM, "provider_message_id": "x" * 241})
    assert excinfo.value.code == E_VALIDATION
    assert "provider_message_id" in excinfo.value.message


async def test_a_timezone_less_received_at_is_refused_not_assumed(async_db_session):
    with pytest.raises(PolsiaError) as excinfo:
        await submit_item(
            async_db_session, **{**BASE_ITEM, "received_at": datetime(2026, 10, 13, 9, 0)}
        )
    assert excinfo.value.code == E_VALIDATION
    assert "timezone" in excinfo.value.message


async def test_resubmitting_a_known_provider_id_returns_the_stored_row_unchanged(
    async_db_session,
):
    """At-least-once ingest: one row, and the STORED customer message is never overwritten."""
    first = await submit_item(async_db_session, **BASE_ITEM)
    again = await submit_item(
        async_db_session, **{**BASE_ITEM, "subject": "edited", "body": "REPLACEMENT TEXT"}
    )

    assert again.id == first.id
    assert await _count(async_db_session, SupportInboxItem) == 1
    stored = (
        await async_db_session.execute(
            select(SupportInboxItem).where(SupportInboxItem.id == first.id)
        )
    ).scalar_one()
    assert stored.body == BASE_ITEM["body"], "a re-submission must not edit stored evidence"
    assert stored.subject == "order #1042"


# ── list ────────────────────────────────────────────────────────────────────────────────────
async def test_list_filters_by_status_and_refuses_an_unknown_one(async_db_session):
    await submit_item(async_db_session, **BASE_ITEM)
    await submit_item(async_db_session, **{**BASE_ITEM, "provider_message_id": "msg-svc-2"})

    pending = await list_items(async_db_session, status="pending")
    assert len(pending) == 2
    assert await list_items(async_db_session, status="dispatched") == []

    with pytest.raises(PolsiaError) as excinfo:
        await list_items(async_db_session, status="delivered")
    assert excinfo.value.code == E_VALIDATION, "an unknown filter must refuse, not answer empty"
    assert "delivered" in excinfo.value.message


# ── attach ──────────────────────────────────────────────────────────────────────────────────
async def test_attach_creates_one_deduped_task_per_item_with_the_rd12_metadata(async_db_session):
    item = await submit_item(async_db_session, **BASE_ITEM)

    attached = await attach_pending(async_db_session)

    assert attached == [
        {
            "item_id": item.id,
            "provider_message_id": "msg-svc-1",
            "task_id": attached[0]["task_id"],
            "task_created": True,
        }
    ]
    tasks = await _tasks(async_db_session)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.agent_type == "customer_support"
    assert task.dedupe_key == inbox_task_dedupe_key("msg-svc-1") == "support-inbox-msg-svc-1"
    assert task.status == "pending" and task.priority == 3 and task.source == "scheduler"
    # the EXACT RD12 consumer contract (agent.py:64-149 reads metadata.inbox_item)
    assert task.metadata_json == {
        "inbox_item": {
            "id": "msg-svc-1",
            "from": "customer@example.com",
            "subject": "order #1042",
            "body": BASE_ITEM["body"],
        }
    }

    stored = (
        await async_db_session.execute(
            select(SupportInboxItem).where(SupportInboxItem.id == item.id)
        )
    ).scalar_one()
    assert stored.status == "dispatched"
    assert stored.task_id == task.id, "dispatched must carry the task it was attached to"


async def test_a_second_attach_is_a_noop_no_item_is_double_attached(async_db_session):
    await submit_item(async_db_session, **BASE_ITEM)
    first = await attach_pending(async_db_session)
    assert len(first) == 1

    second = await attach_pending(async_db_session)
    assert second == [], "dispatched items are skipped — nothing new was attached"
    assert await _count(async_db_session, Task) == 1, "no duplicate task may ever appear"


async def test_attach_is_oldest_first_by_received_at_not_by_insertion_order(async_db_session):
    """`coalesce(received_at, created_at)` decides — the arrive-earlier message goes first."""
    later = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    earlier = later - timedelta(hours=5)
    await submit_item(
        async_db_session, **{**BASE_ITEM, "provider_message_id": "msg-late", "received_at": later}
    )
    await submit_item(
        async_db_session,
        **{**BASE_ITEM, "provider_message_id": "msg-early", "received_at": earlier},
    )

    attached = await attach_pending(async_db_session)

    assert [row["provider_message_id"] for row in attached] == ["msg-early", "msg-late"]


async def test_attach_is_flush_only_the_caller_owns_the_commit(async_db_session):
    """The service flushes; a rollback takes the whole attach back (item marks included)."""
    item = await submit_item(async_db_session, **BASE_ITEM)
    await async_db_session.commit()  # the submit is the caller's business, committed here
    item_id = item.id  # read BEFORE the attach/rollback: rollback expires the instance

    await attach_pending(async_db_session)
    await async_db_session.rollback()

    assert await _count(async_db_session, Task) == 0, "the created task never committed"
    stored = (
        await async_db_session.execute(
            select(SupportInboxItem).where(SupportInboxItem.id == item_id)
        )
    ).scalar_one()
    assert stored.status == "pending" and stored.task_id is None, (
        "the dispatched mark was part of the rolled-back transaction"
    )


# ── the state-set decision (pinned, see the model docstring & receipt) ──────────────────────
def test_the_sweep_needs_exactly_two_states_and_served_was_rejected():
    """`served` would be write-less surface: no code path in this tree could ever set it."""
    assert VALID_STATUSES == ("pending", "dispatched")
