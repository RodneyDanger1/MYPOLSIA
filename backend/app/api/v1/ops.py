"""GET /api/v1/ops/events/stats + POST /api/v1/ops/events/reset — the breaker's operator surface (R22).

WHY this file exists: `events.publish_stats()` and `reset_publish_breaker()` shipped with the
activity circuit breaker (the "a skip is never silent" contract, `app/core/events.py:44-61`) and
had ZERO production callers — the counters and the recovery lever were reachable only from tests.
These routes are the owner's read/reset surface during/after a Redis incident; the 15-minute beat
tick (`celery_app.tasks.maintenance.check_activity_publish_health`) writes the same stats into the
worker's log.

PROCESS BOUNDARY (stated, not hidden): the breaker's counters are module globals in
`app.core.events`, i.e. PER PROCESS.  These routes report and reset the API process's state; the
beat tick reports the worker process's state.  Neither surface claims to see the other's numbers.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`support.py:28`, `memory.py:17`).  The reset verb needs no `refusal_guard`: it flips in-memory
counters and nothing else — no DB row, no external call, nothing that can refuse.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import verify_api_key
from app.core import events

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(verify_api_key)])


@router.get("/events/stats")
async def read_event_publish_stats() -> dict[str, Any]:
    """failures / skipped_while_open / breaker_open / cooldown_seconds / last_error."""
    return events.publish_stats()


@router.post("/events/reset")
async def reset_event_publish_breaker() -> dict[str, Any]:
    """Clear the breaker and its counters after an incident; returns the fresh (clean) stats."""
    events.reset_publish_breaker()
    return events.publish_stats()
