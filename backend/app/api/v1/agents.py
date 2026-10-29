"""GET /api/v1/agents/status and POST /api/v1/agents/{agent_type}/trigger.

Auth ordering (measured FastAPI behavior, fastapi/dependencies/utils.py solve_dependencies:
dependencies are solved BEFORE path/query/body validation):
  * BOTH routes carry the key guard as a route dependency (`Depends(verify_api_key)`), so an
    unauthenticated caller ALWAYS gets the one named 401 E_AUTH — for a known agent name and
    an unknown one alike. U2-05 (wave-v14): the trigger route used to check the key INSIDE
    the handler, so an unauthenticated caller got 422 (whose body listed every valid agent
    type) for an unknown name and 401 for a known one — a credential-free valid/invalid
    enumeration oracle. Auth first closes it; WITH a valid key the enum still 422s an
    unknown name by name.
  * the body model is OPTIONAL (default None) so a request with no body cannot 422
    before auth runs.

`GET /status` is ONE statement (`task_service.get_agent_statuses`): a CTE of the
nine agent types plus correlated subqueries, so it returns exactly nine rows on an
empty database instead of 27 round trips (lane 06: 981.889 ms → 84.838 ms).

`POST /{agent_type}/trigger`'s broker hand-off (D6-R2): the row is COMMITTED before `.delay()`
(A2A2-01) and a refused `.delay()` does NOT terminalise it.  A publish refusal cannot prove that
nothing was delivered (A2-03), so the row stays `pending` with `metadata.rearmed_at` — the marker
`maintenance._reconcile_stranded_pending` re-dispatches on — and the caller gets the named `E_REDIS`
refusal saying the work was accepted but not dispatched.
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_DB, E_REDIS, ERROR_HINTS, MeridianError
from app.schemas.task import AgentStatusResponse, AgentTriggerRequest, AgentTriggerResponse
from app.services.task_service import (
    VALID_AGENT_TYPES,
    create_task,
    get_agent_statuses,
)

router = APIRouter(prefix="/agents", tags=["agents"])

# The enum is built from the LIVE registry on purpose: a literal member list here would silently
# drift from VALID_AGENT_TYPES (an added/renamed agent must still 422, not 500). The checker's
# [misc] asks for a literal; the registry is the source of truth, so the ignore is the honest
# form — a copy here is the thing that WOULD drift (T13).
AgentTypeEnum = Enum("AgentTypeEnum", {name: name for name in VALID_AGENT_TYPES})  # type: ignore[misc]  # built from the live VALID_AGENT_TYPES registry so the enum cannot drift


@router.get("/status", response_model=list[AgentStatusResponse], dependencies=[Depends(verify_api_key)])
async def agent_status(db: AsyncSession = Depends(get_db)) -> list[AgentStatusResponse]:
    with refusal_guard():
        rows = await get_agent_statuses(db)
    return [AgentStatusResponse(**row) for row in rows]


@router.post(
    "/{agent_type}/trigger",
    response_model=AgentTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_api_key)],
)
async def trigger_agent(
    agent_type: AgentTypeEnum,
    payload: AgentTriggerRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> AgentTriggerResponse:
    agent_name = agent_type.value
    title = (payload.task_title if payload else None) or f"Manual {agent_name} run"
    with refusal_guard():
        # T14b (wave-W3 REQUESTS §4): the manual finance run must SEE the failed-payment records
        # (its refund path needs a real `pi_…` id), exactly as the morning cycle's fixed finance
        # task does (`daily_cycle._morning_cycle`).  Agents hold no DB session, so the task's own
        # `metadata` is the delivery channel; the attach happens ONLY for finance and ONLY when
        # records exist — an empty read attaches NOTHING (the row is exactly what it was before,
        # and the agent's consumer degrades to no extra context).
        metadata: dict[str, Any] | None = None
        if agent_name == "finance":
            from app.services.finance_service import recent_failed_payments

            failed_payments = await recent_failed_payments(db)
            if failed_payments:
                metadata = {"failed_payments": failed_payments}
        task = await create_task(
            db,
            title=title,
            agent_type=agent_name,
            description=payload.description if payload else None,
            priority=payload.priority if payload else 3,
            source="manual",
            metadata=metadata,
        )
        # A2A2-01: COMMIT BEFORE THE HAND-OFF.  `create_task` only flushes; the commit that makes
        # the row visible to another connection used to be `get_db`'s, i.e. AFTER this response —
        # so a worker could pick the message up and claim a row that did not exist yet (it answers
        # `E_NOT_FOUND` and the message is consumed: nothing re-dispatches a `manual` row), and a
        # commit that failed left a live message for a row that would never exist.  The two
        # sibling call sites already state the rule (`approvals.py:79-81`, `chat_service.py:770-772`
        # — which cites THIS module as its source).  `expire_on_commit=False` (database.py:100)
        # keeps `task.id` usable after the commit.
        try:
            await db.commit()
        except SQLAlchemyError as exc:
            # The store refused the row: nothing was dispatched, and the caller gets the named
            # refusal instead of a 202 for work the broker was never told about.
            raise MeridianError(
                E_DB,
                f"task #{task.id} could not be committed; nothing was dispatched",
                how_to_fix=ERROR_HINTS[E_DB],
                context={"task_id": task.id, "agent_type": agent_name},
            ) from exc
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
    except Exception as exc:  # broker unavailable: a refusal cannot prove nothing was delivered
        # A2-03's class, in the API path (D6-R2).  The old branch wrote the row `failed` on the
        # strength of a comment claiming "NO delivery was accepted" — which a publish refusal
        # cannot know: a connection that dies mid-publish can leave a live message behind, and
        # `claim_task` accepts only `pending` rows, so the row was written off while the work was
        # already queued.  The row therefore stays `pending` and carries `metadata.rearmed_at`
        # (`task_service.requeue_stale_claims` / `_common.enqueue_created` write the same marker
        # through the same helper): a delivered row is claimed (the claim is its proof of life),
        # an undelivered one is re-dispatched by `maintenance._reconcile_stranded_pending` within
        # one window instead of waiting for a human.  The caller STILL gets the named refusal — a
        # fake 202/"queued" is worse than a loud one — and the refusal says exactly what happened:
        # the work was accepted (the row exists, still queued) but not dispatched.
        import logging

        from celery_app.tasks._common import stamp_rearmed  # lazy: worker layer, cycle-free

        reason = f"enqueue_refused:{exc.__class__.__name__}"
        logging.getLogger(__name__).warning(
            "failed to enqueue task %s for %s; re-arming the undelivered row for the reconciler",
            task.id,
            agent_name,
            exc_info=True,
        )
        rearmed = await stamp_rearmed(db, [task.id], reason=reason)
        # The refusal also lands ON the row (`error_message` names the code) while the row stays
        # `pending`: the diagnostic a human reads is kept, and the status keeps the truth that the
        # work was never proven dispatched.  `status` is deliberately NOT touched here — the
        # re-arm helper only merges `metadata`, and a claim is the delivery's proof of life.
        task.error_message = f"{E_REDIS}: enqueue failed ({exc.__class__.__name__})"
        # Load-bearing commit (T19): `create_task`/`stamp_rearmed` only FLUSH, and `get_db` rolls
        # the request session back on the exception raised right below — measured before this
        # commit (W4 scratch, re-read by W2): the row the 503 names ("task #16 ...") was rolled
        # back and never existed, so the ledger lost the attempt entirely while the caller held an
        # id.  The created row, its re-arm marker and its reason must survive the refusal.
        await db.commit()
        raise MeridianError(
            E_REDIS,
            f"task #{task.id} was accepted (created, still pending) but NOT dispatched: the "
            f"broker refused the hand-off ({exc.__class__.__name__}), so the row stays queued for "
            "the stranded-row reconciler to re-dispatch",
            how_to_fix=(
                "no action is required — the row is pending and the 5-minute stranded-row sweep "
                "re-dispatches it; check REDIS_URL and that the broker/worker are running if this "
                "keeps happening"
            ),
            context={
                "task_id": task.id,
                "agent_type": agent_name,
                "status": "pending",
                "accepted": True,
                "dispatched": False,
                "rearmed": rearmed,
                "reason": reason,
            },
        ) from exc
    return AgentTriggerResponse(
        status="queued",
        task_id=task.id,
        message=f"Task #{task.id} queued for {agent_name}",
    )
