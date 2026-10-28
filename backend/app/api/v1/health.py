"""Liveness and readiness probes: GET /api/v1/health (liveness), GET /api/v1/ready (readiness).

F-50 (wave B2) — the split.  `/health` used to be the only probe, it returned a constant, and it is
what docker-compose's healthcheck calls: a backend whose database was gone answered `{"status":"ok"}`
forever, so the orchestrator kept a dead service in rotation.  Two probes now, with different jobs:

  * `/health` — LIVENESS.  Constant, unauthenticated, allocation-free: it answers one question, "is
    this process running and serving HTTP?".  A dependency outage is NOT a reason to restart the
    process, so this probe never touches one.  The payload is frozen at `{"status":"ok"}` by
    `app/schemas/common.HealthResponse`.
  * `/ready` — READINESS.  It issues one real `SELECT 1` on the app's own session and FAILS CLOSED:
    a database that cannot answer becomes `503` with the house `E_DB` refusal (`code`, `message`,
    `how_to_fix`, `X-Polsia-Error-Code`) rather than a 200 the orchestrator would trust.  A
    pre-route failure (the session factory itself raising) lands on the same 503 through
    `app.main`'s `SQLAlchemyError` handler.

Redis is deliberately NOT part of readiness: the activity feed degrades without it and every other
route still serves — taking the whole service out of rotation for a dead broker would be a bigger
outage than the one it reports.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import E_DB, PolsiaError
from app.schemas.common import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class ReadyResponse(BaseModel):
    """`GET /api/v1/ready` — the readiness verdict plus the per-check detail that produced it."""

    status: str
    checks: dict[str, str]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: constant and cheap.  Never consults a dependency (see the module docstring)."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(db: AsyncSession = Depends(get_db)) -> ReadyResponse:
    """Readiness: the database must answer, and a failure is a NAMED 503 (fail closed)."""
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("readiness probe failed on the database (%s)", type(exc).__name__)
        raise PolsiaError(
            E_DB,
            "not ready: the database did not answer the readiness probe",
            how_to_fix="check that the database is running and reachable (DATABASE_URL), then "
                       "retry GET /api/v1/ready",
            context={"probe": "database", "error": type(exc).__name__},
        ) from exc
    return ReadyResponse(status="ready", checks={"database": "ok"})
