"""GET /api/v1/agents/status and POST /api/v1/agents/{agent_type}/trigger.

Designed against the measured FastAPI ordering (fastapi/dependencies/utils.py
solve_dependencies): sub-dependencies are solved BEFORE path/query/body validation.
Therefore:
  * a router-level auth dependency would answer 401 for an UNKNOWN agent type -- the
    path param is never validated;
  * this router instead validates `agent_type` as an Enum (=> 422) and checks the
    X-API-Key header INSIDE the handler, so "unknown agent" is 422 with or without a
    key, while "known agent without key" is still 401;
  * the body model is OPTIONAL (default None) so a request with no body cannot 422
    before the inline auth check runs.

`GET /status` is ONE statement (`task_service.get_agent_statuses`): a CTE of the
nine agent types plus correlated subqueries, so it returns exactly nine rows on an
empty database instead of 27 round trips (lane 06: 981.889 ms → 84.838 ms).
"""
from __future__ import annotations

import asyncio
from enum import Enum

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.security import check_api_key
from app.schemas.task import AgentStatusResponse, AgentTriggerRequest, AgentTriggerResponse
from app.services.task_service import (
    VALID_AGENT_TYPES,
    create_task,
    get_agent_statuses,
)

router = APIRouter(prefix="/agents", tags=["agents"])

AgentTypeEnum = Enum("AgentTypeEnum", {name: name for name in VALID_AGENT_TYPES})


@router.get("/status", response_model=list[AgentStatusResponse], dependencies=[Depends(verify_api_key)])
async def agent_status(db: AsyncSession = Depends(get_db)) -> list[AgentStatusResponse]:
    with refusal_guard():
        rows = await get_agent_statuses(db)
    return [AgentStatusResponse(**row) for row in rows]


@router.post(
    "/{agent_type}/trigger",
    response_model=AgentTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_agent(
    agent_type: AgentTypeEnum,
    payload: AgentTriggerRequest | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> AgentTriggerResponse:
    await check_api_key(x_api_key)
    agent_name = agent_type.value
    title = (payload.task_title if payload else None) or f"Manual {agent_name} run"
    with refusal_guard():
        task = await create_task(
            db,
            title=title,
            agent_type=agent_name,
            description=payload.description if payload else None,
            priority=payload.priority if payload else 3,
            source="manual",
        )
    try:
        from celery_app.tasks.agent_tasks import run_agent_task

        # `.delay()` is a BLOCKING call: with the Redis broker it runs redis-py's connection retry
        # ladder, and called directly from an async handler that stalls the uvicorn event loop for the
        # whole ladder — every concurrent request then hangs behind it. Measured by this repo's own e2e
        # run: one "Run Now" click left GET /api/v1/config timing out at 20s while the backend log
        # filled with `celery.backends.redis: Connection to Redis lost: Retry (7/20)`. `to_thread` keeps
        # the loop free; the bounded broker transport (celery_app/celery_config.py) keeps the thread
        # from waiting out a long ladder before the except branch below handles the outage.
        await asyncio.to_thread(run_agent_task.delay, task.id)
    except Exception:  # broker unavailable: the task row still exists and is queued
        import logging

        logging.getLogger(__name__).warning("failed to enqueue task %s", task.id, exc_info=True)
    return AgentTriggerResponse(
        status="queued",
        task_id=task.id,
        message=f"Task #{task.id} queued for {agent_name}",
    )
