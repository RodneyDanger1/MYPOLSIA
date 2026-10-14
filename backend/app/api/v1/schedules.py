"""GET/POST /api/v1/schedules + PATCH/DELETE /api/v1/schedules/{schedule_id} (wave V8, C2).

The owner's recurring task prompts.  This file adds NO business rules of its own — the service
(`app/services/schedule_service.py`) owns validation, the pause rule (`enabled=false`), the
company-tz next-run math and the cadence tick's claim; the routes are the wire shape over it.

    POST   /api/v1/schedules                    -> 201 <schedule>
    GET    /api/v1/schedules                    -> 200 {"items": [...], "total": n}
    PATCH  /api/v1/schedules/{schedule_id}      -> 200 <schedule>   (pause = {"enabled": false})
    DELETE /api/v1/schedules/{schedule_id}      -> 200 <schedule>   (the removed row, verbatim)

REFUSALS (named, never silent): an unknown id is `E_NOT_FOUND` (404); a malformed cadence/hour/
weekday is answered 422 BY THE SCHEMA (naming the field) before the row is touched — and the
service keeps the same refusals (`E_VALIDATION`) as the belt for internal callers.

AUTH: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries.

Commit discipline: every write builds its response and commits explicitly in the route body (the
`auto_mode.py` precedent), so the write lands even under a plain test-client session override
instead of riding `get_db`'s teardown.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.schedule import (
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleUpdate,
)
from app.services import schedule_service as schedules

router = APIRouter(prefix="/schedules", tags=["schedules"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(db: AsyncSession = Depends(get_db)) -> ScheduleListResponse:
    """Every schedule row, oldest first (stable id order); no rows is an honest empty list."""
    with refusal_guard():
        rows = await schedules.list_schedules(db)
        body = ScheduleListResponse(
            items=[ScheduleResponse.model_validate(row) for row in rows], total=len(rows)
        )
    return body


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreate, db: AsyncSession = Depends(get_db)
) -> ScheduleResponse:
    """Create one schedule; `enabled: true` computes its first `next_run_at` immediately."""
    with refusal_guard():
        row = await schedules.create_schedule(
            db,
            name=payload.name,
            prompt=payload.prompt,
            cadence=payload.cadence,
            hour=payload.hour,
            weekday=payload.weekday,
            enabled=payload.enabled,
        )
        body = ScheduleResponse.model_validate(row)
        await db.commit()
    return body


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: int, payload: ScheduleUpdate, db: AsyncSession = Depends(get_db)
) -> ScheduleResponse:
    """Apply only the provided fields; pause = `{"enabled": false}` (clears the due instant)."""
    with refusal_guard():
        row = await schedules.update_schedule(
            db, schedule_id, payload.model_dump(exclude_unset=True)
        )
        body = ScheduleResponse.model_validate(row)
        await db.commit()
    return body


@router.delete("/{schedule_id}", response_model=ScheduleResponse)
async def delete_schedule(
    schedule_id: int, db: AsyncSession = Depends(get_db)
) -> ScheduleResponse:
    """Remove the row and answer its removed representation (not a bare 204)."""
    with refusal_guard():
        row = await schedules.delete_schedule(db, schedule_id)
        body = ScheduleResponse.model_validate(row)
        await db.commit()
    return body
