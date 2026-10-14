"""POST / GET / DELETE /api/v1/auto-mode — the bounded Auto Mode window (wave U5, plan P2/B22).

WHY this file exists: the reference product can hand the company to "Auto Mode" for a bounded
window; the clone had the per-run dispatch gate but no session surface at all.  This is the
owner's surface onto `auto_mode_sessions` (the schema lane P3 froze; the service is
`app/services/auto_mode_service.py` — this file adds NO business rules of its own).

THE SHAPE (response models live IN this file, the channels.py precedent; tests/unit/
test_u5_auto_mode.py pins it key by key)

    POST /api/v1/auto-mode   {"duration_hours": 1..120}      -> 201
        200-success shape: {"started": true, "session": <view>}
    GET  /api/v1/auto-mode                                   -> 200
        {"active": bool, "session": <view> | null}
    DELETE /api/v1/auto-mode                                 -> 200
        {"stopped": true, "session": <view>}

    <view> = {id, started_at, ends_at, stopped_at, stop_reason, tasks_run,
              spend_used, time_left_seconds}

PROVENANCE — every number comes from a real source; none is invented here:

  * `tasks_run`     <- the session row, INCREMENTED by the worker's terminal write
    (`celery_app/tasks/agent_tasks._execute_agent_task`, one UPDATE folded into the run's own
    activity transaction) — this route only reads it.
  * `spend_used`    <- SUM(agent_runs.cost_usd) over [session.started_at, session.ends_at],
    exact Decimal arithmetic through `finance_service.money`.  `null` means NOTHING WAS
    MEASURED (no ledger rows, or all-NULL costs) — never a fabricated `0.00`; a real measured
    zero is the finance layer's business, not this counter's.
  * `time_left_seconds` <- `ends_at - now`, only for a COVERING window; `null` once closed.
  * `stopped_at`/`stop_reason`<- the service's recorded close; this route never invents one.

REFUSALS (named, never silent):
  * duration outside 1 hour..5 days                     -> E_VALIDATION (422)
  * a session is already active (never two windows)     -> E_VALIDATION (422)
  * DELETE with nothing active                          -> E_VALIDATION (422)
  * an expired window is closed LAZILY on read with `stop_reason="duration_end"`; GET and
    DELETE commit that close explicitly, so the write lands even under a plain test-client
    session override (the approvals.py precedent) instead of riding `get_db`'s teardown.

AUTH: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`channels.py:52`, `ops.py:27`).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.services import auto_mode_service as ams

router = APIRouter(
    prefix="/auto-mode", tags=["auto-mode"], dependencies=[Depends(verify_api_key)]
)


class AutoModeStartRequest(BaseModel):
    """`duration_hours` — the window length; validated 1 hour..5 days by the service."""

    duration_hours: float = Field(
        ...,
        description="Window length in hours, 1..120 (1 hour to 5 days).",
    )


class AutoModeSessionView(BaseModel):
    """One session row + its live counters (see the module docstring for provenance)."""

    id: int
    started_at: datetime
    ends_at: datetime
    stopped_at: datetime | None
    stop_reason: str | None
    tasks_run: int
    spend_used: float | None
    time_left_seconds: int | None


class AutoModeStatusResponse(BaseModel):
    active: bool
    session: AutoModeSessionView | None


class AutoModeStartResponse(BaseModel):
    started: bool
    session: AutoModeSessionView


class AutoModeStopResponse(BaseModel):
    stopped: bool
    session: AutoModeSessionView


@router.post("", response_model=AutoModeStartResponse, status_code=status.HTTP_201_CREATED)
async def start_auto_mode(
    payload: AutoModeStartRequest, db: AsyncSession = Depends(get_db)
) -> AutoModeStartResponse:
    """Open the one Auto Mode window (refuses `E_VALIDATION` if one is already active)."""
    with refusal_guard():
        row = await ams.start_session(db, duration_hours=payload.duration_hours)
        view = await ams.session_view(db, row)
        # Explicit commit so the write lands independently of `get_db`'s teardown commit
        # (identical behaviour under the test-client's plain session override).
        await db.commit()
    return AutoModeStartResponse.model_validate({"started": True, "session": view})


@router.get("", response_model=AutoModeStatusResponse)
async def auto_mode_status(db: AsyncSession = Depends(get_db)) -> AutoModeStatusResponse:
    """The live window + live counters; an expired window is closed lazily here."""
    with refusal_guard():
        snapshot = await ams.status_snapshot(db)
        await db.commit()
    return AutoModeStatusResponse.model_validate(snapshot)


@router.delete("", response_model=AutoModeStopResponse)
async def stop_auto_mode(db: AsyncSession = Depends(get_db)) -> AutoModeStopResponse:
    """Stop the live window early with `stop_reason="user_stop"`; refuses if none is active."""
    with refusal_guard():
        row = await ams.stop_active_session(db)
        view = await ams.session_view(db, row)
        await db.commit()
    return AutoModeStopResponse.model_validate({"stopped": True, "session": view})
