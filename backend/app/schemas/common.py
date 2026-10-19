from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _as_utc(value: datetime) -> datetime:
    """Read a naive stamp as UTC (the clock the database stores) and render it aware.

    WHY: SQLite hands `DateTime(timezone=True)` columns back NAIVE while PostgreSQL hands them
    back offset-bearing, so the SAME row was served as `2026-05-01T12:00:00` from one dialect
    and `2026-05-01T12:00:00Z` from the other — a client could not tell whether the naive form
    was UTC or local, and JavaScript's `new Date()` reads it as LOCAL.  Every response model
    uses `UtcDatetime`, so one at-rest instant has one wire spelling on both engines.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


#: A wire datetime: aware UTC, serialized as `...Z` (ISO-8601).  Use for every RESPONSE field;
#: request models keep plain `datetime` (the parser must accept both forms, the writer normalises).
UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc, return_type=datetime, when_used="json")]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Deliberately ONLY `status` - GET /api/v1/health must serialise to {"status":"ok"}."""

    status: str = "ok"


class TimestampedORM(ORMModel):
    created_at: UtcDatetime | None = None
