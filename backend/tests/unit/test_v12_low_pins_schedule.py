"""V12/W-C — schedule-family PINS (xfail, defect named per test).

F6  — one malformed stored row refuses E_CONFIG for the WHOLE tick ("no partial tick",
       documented + pinned at tests/unit/test_v8_schedules.py:376; skip-and-record is a
       design change for the parent).
F19 — the dedupe bucket is the company-local date-HOUR, not the due instant; verified
       unreachable-collapse for the frozen cadences, but an intra-hour cadence would merge
       two occurrences onto one key. The key format `schedule:<id>:<iso-date-hour>` is
       byte-pinned at test_v8_schedules.py:296 — widening it is a deliberate pin update.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import app.models  # noqa: F401
from app.models.schedule import TaskSchedule
from app.services.schedule_service import claim_due_schedules

UTCz = ZoneInfo("UTC")
FROZEN = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def _make(db, name, hour, next_run_at):
    row = TaskSchedule(
        name=name, prompt="p", cadence="daily", hour=hour, weekday=None,
        enabled=True, next_run_at=next_run_at,
    )
    db.add(row)
    return row


@pytest.mark.xfail(
    strict=False,
    reason="F6: one malformed stored row stalls the WHOLE tick (E_CONFIG before any advance; "
    "no per-row isolation in cadence.run_due_schedules) — documented 'no partial tick', "
    "pin-update is a parent design call",
)
def test_f6_one_malformed_row_must_not_stall_the_whole_tick(queue_db):
    async def _go():
        async with queue_db.factory() as db:
            _make(db, "Healthy", 6, datetime(2026, 10, 14, 6, 0))
            _make(db, "Malformed", 99, datetime(2026, 10, 14, 6, 0))
            await db.flush()
            claims = await claim_due_schedules(db, now=FROZEN, tz=UTCz)
            return [claim.name for claim in claims]

    fired = queue_db.run(_go())
    assert fired == ["Healthy"], "healthy rows must still fire while the malformed one is skipped+recorded"


@pytest.mark.xfail(
    strict=False,
    reason="F19 [spec]: the dedupe key buckets the due instant to its company-local HOUR — two "
    "due instants inside one hour collide onto one key; key format is byte-pinned in shipped "
    "tests, so widening to the exact instant is a deliberate pin update",
)
def test_f19_two_due_instants_in_one_hour_must_not_share_a_dedupe_key(queue_db):
    async def _go():
        async with queue_db.factory() as db:
            row = _make(db, "Intra-hour", 6, datetime(2026, 10, 14, 6, 20))
            await db.flush()
            first = await claim_due_schedules(
                db, now=datetime(2026, 10, 14, 6, 30, tzinfo=UTC), tz=UTCz
            )
            row.next_run_at = datetime(2026, 10, 14, 6, 40)  # a second due instant, same hour
            await db.flush()
            second = await claim_due_schedules(
                db, now=datetime(2026, 10, 14, 6, 50, tzinfo=UTC), tz=UTCz
            )
            return first[0].dedupe_key, second[0].dedupe_key

    k1, k2 = queue_db.run(_go())
    assert k1 != k2, f"both due instants produced the same dedupe key {k1!r} (hour bucket)"
