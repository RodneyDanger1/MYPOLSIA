"""T17 — `create_task_deduped` asserts the full task-status set (fix-5 REQUESTS §7).

The deduped creator constructs `Task(...)` DIRECTLY — it never went through
`task_service.create_task`, which has asserted the value set since L1-09/L2-02.  Its callers pass
no `status` today, so the hole was unreachable, but a future caller could bind an out-of-set (or
unset/None) status straight onto the row: on SQLite the `ck_tasks_status` CHECK only exists on
fresh `create_all` schemas, and the sweep paths write rows the dashboard serves.

RED leg: delete the assertion -> `test_an_invalid_status_is_refused_before_any_row` comes back
with a written row instead of a ValueError.
"""
from __future__ import annotations

import pytest

from app.services.task_service import VALID_TASK_STATUSES
from celery_app.tasks._common import create_task_deduped


def _create(queue_db, *, status, dedupe_key: str = "t17:key:1", title: str = "t17"):
    from app.agents.crew_factory import AGENT_MAP  # noqa: F401  (registry is imported by callers)

    async def _make():
        async with queue_db.factory() as db:
            task = await create_task_deduped(
                db,
                title=title,
                agent_type="finance",
                dedupe_key=dedupe_key,
                status=status,
            )
            await db.commit()
            return None if task is None else int(task.id)

    return queue_db.run(_make())


def test_every_valid_status_is_accepted_and_stored(queue_db):
    """The whole tuple, not a sample — the creator and the writers agree on one set."""
    from app.services.task_service import VALID_TASK_STATUSES as statuses

    for index, status in enumerate(statuses):
        task_id = _create(queue_db, status=status, dedupe_key=f"t17:ok:{index}", title=f"t17 {status}")
        assert task_id is not None
        row = queue_db.row(task_id)
        assert row["status"] == status


@pytest.mark.parametrize("bad", ["not_a_status", "complete", "REJECTED", ""])
def test_an_invalid_status_is_refused_before_any_row(queue_db, bad):
    with pytest.raises(ValueError) as excinfo:
        _create(queue_db, status=bad, dedupe_key="t17:bad:1")

    message = str(excinfo.value)
    assert "Unknown task status" in message
    assert repr(bad) in message, "the refusal names the offending value"
    assert VALID_TASK_STATUSES[0] in message and VALID_TASK_STATUSES[-1] in message, (
        "the refusal names the full valid set"
    )
    assert queue_db.count("tasks", "dedupe_key = ?", ("t17:bad:1",)) == 0, "nothing was written"


def test_an_unset_status_is_refused_too(queue_db):
    """`None` is not a status: the column may hold NULL, but no WRITER may create one that way."""
    with pytest.raises(ValueError) as excinfo:
        _create(queue_db, status=None, dedupe_key="t17:unset:1")
    assert "Unknown task status" in str(excinfo.value)
    assert repr(None) in str(excinfo.value)
    assert queue_db.count("tasks", "dedupe_key = ?", ("t17:unset:1",)) == 0


def test_the_default_status_is_still_pending(queue_db):
    """Additive change: the default path writes exactly the previous row."""
    from celery_app.tasks._common import create_task_deduped  # noqa: F811

    async def _make_default():
        async with queue_db.factory() as db:
            task = await create_task_deduped(
                db, title="t17 default", agent_type="finance", dedupe_key="t17:default:1"
            )
            await db.commit()
            return int(task.id)

    task_id = queue_db.run(_make_default())
    assert queue_db.row(task_id)["status"] == "pending"
