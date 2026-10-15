"""V11/W2 F4 — a consumed one-shot must not be re-armed by a metadata-only edit.

Audit finding F4: `update_schedule` (schedule_service.py:277-281) recomputed `next_run_at`
whenever it was None — so ANY edit, including a name- or prompt-only one, re-armed a spent
`once` schedule (the tick clears `next_run_at` to None after firing; `claim_due_schedules`),
contradicting the function's own docstring ("a name- or prompt-only edit leaves the pending
instant alone") and silently firing a one-shot the user had already used up.

Pinned here:
1. a consumed `once` row stays consumed under prompt-only, name-only and empty edits — T1;
2. a real SEMANTICS change (cadence/hour/weekday, or enabling) still computes the next run — T2;
3. the pause rule is unchanged: disabling clears, enabling arms — T3.
"""
from __future__ import annotations


async def _consumed_once(db):
    """A `once` schedule the tick already fired (`next_run_at` cleared to None)."""
    from app.services.schedule_service import create_schedule

    row = await create_schedule(
        db, name="one shot", prompt="do the thing once", cadence="once", hour=6, enabled=True
    )
    assert row.next_run_at is not None  # created armed
    row.next_run_at = None  # exactly what claim_due_schedules writes for a spent once
    await db.flush()
    return row


async def test_metadata_only_edits_do_not_rearm_a_consumed_once(async_db_session):
    from app.services.schedule_service import update_schedule

    db = async_db_session
    row = await _consumed_once(db)

    assert (await update_schedule(db, row.id, {"prompt": "say it differently"})).next_run_at is None
    assert (await update_schedule(db, row.id, {"name": "one shot (renamed)"})).next_run_at is None
    assert (await update_schedule(db, row.id, {})).next_run_at is None


async def test_a_semantics_change_still_arms_the_next_run(async_db_session):
    from app.services.schedule_service import update_schedule

    db = async_db_session
    row = await _consumed_once(db)

    updated = await update_schedule(db, row.id, {"cadence": "daily"})
    assert updated.next_run_at is not None, "a cadence change is a re-arm by design"

    weekly = await update_schedule(db, row.id, {"cadence": "weekly", "weekday": 2})
    assert weekly.next_run_at is not None


async def test_pause_rule_unchanged(async_db_session):
    from app.services.schedule_service import create_schedule, update_schedule

    db = async_db_session
    row = await create_schedule(
        db, name="paused", prompt="later", cadence="daily", hour=8, enabled=False
    )
    assert row.next_run_at is None  # a paused row carries no pending instant

    armed = await update_schedule(db, row.id, {"enabled": True})
    assert armed.next_run_at is not None, "enabling must arm from the wall clock"

    cleared = await update_schedule(db, row.id, {"enabled": False})
    assert cleared.next_run_at is None, "disabling must clear the pending instant"
