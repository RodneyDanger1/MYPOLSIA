"""Wire schemas for /api/v1/schedules (wave V8, C2's surface).

The frozen row (`app.models.schedule.TaskSchedule`): `cadence` one of once|daily|weekdays|weekly,
`hour` the COMPANY-LOCAL wall-clock hour (0-23), `weekday` (0-6, Monday=0) applying to `weekly`,
`enabled` the pause switch.  Validation lives HERE (the settings-page doctrine): FastAPI answers
422 naming the field BEFORE a row is touched — and the service keeps the same refusals
(`E_VALIDATION`) as the belt for callers that never pass through the route.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

Cadence = Literal["once", "daily", "weekdays", "weekly"]


class ScheduleCreate(BaseModel):
    """POST body.  `enabled` defaults FALSE (the column default): created PAUSED unless stated."""

    name: str = Field(..., min_length=1, max_length=255)
    prompt: str = Field(..., min_length=1)
    cadence: Cadence
    hour: int = Field(..., ge=0, le=23)
    weekday: int | None = Field(default=None, ge=0, le=6)
    enabled: bool = False


class ScheduleUpdate(BaseModel):
    """PATCH body — every field optional; only the PROVIDED keys are applied (`exclude_unset`)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    prompt: str | None = Field(default=None, min_length=1)
    cadence: Cadence | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    weekday: int | None = Field(default=None, ge=0, le=6)
    enabled: bool | None = None


class ScheduleResponse(ORMModel):
    """One schedule row, verbatim (the GET/POST/PATCH/DELETE item shape)."""

    id: int
    name: str
    prompt: str
    cadence: str
    hour: int
    weekday: int | None = None
    enabled: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime | None = None


class ScheduleListResponse(BaseModel):
    items: list[ScheduleResponse]
    total: int
