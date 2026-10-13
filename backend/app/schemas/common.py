from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Deliberately ONLY `status` - GET /api/v1/health must serialise to {"status":"ok"}."""

    status: str = "ok"


class TimestampedORM(ORMModel):
    created_at: datetime | None = None
