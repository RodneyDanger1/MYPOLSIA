from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    """Deliberately ONLY `status` - GET /api/v1/health must serialise to {"status":"ok"}."""

    status: str = "ok"


class MessageResponse(BaseModel):
    """The generic `{"message": ...}` ack body.

    ZERO CONSUMERS (fix-5 sweep, 2026-10-13; raw grep evidence:
    polsia-work/build/full-sweep/fix-5/raw/greps_zero_consumer.txt) — no route declares it as a
    `response_model` and nothing imports it; every shipped ack/refusal travels as something more
    specific (the `{ok:false,error:{…}}` envelope for refusals).  Wiring a route's ack to it vs
    deleting it is an owner decision (polsia-work/build/full-sweep/fix-5/REQUESTS.md §2).
    """

    message: str


class TimestampedORM(ORMModel):
    created_at: datetime | None = None
