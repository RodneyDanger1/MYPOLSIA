"""POST /api/v1/approvals — the owner's approval-token mint (D1 / T02).

The missing producer of the firewall's approval channel.  Before this route the firewall's
`verify_approval` ALLOW path was live but nothing could mint a token, so every gated action ended
in "approval required but no approval channel configured" and the DENY was final.  The loop this
route completes:

  1. a run hits a gated action (irreversible / tainted / >= the $ value threshold), the firewall
     answers `REQUIRE_APPROVAL`, and the worker parks the task `awaiting_approval` with the action's
     `fingerprint` recorded in its result metadata;
  2. the owner mints a token for that fingerprint HERE;
  3. the next run supplies the token (the task's `metadata.approval_token` channel —
     `agent_tasks._execute_agent_task` publishes it as the run's `RunApproval` context), the
     firewall verifies it and the action PROCEEDS.

T02b (the owner's chosen design, wave-W1 REQUESTS §1 option (b)) closes the WHOLE loop over HTTP:
the request body now carries an OPTIONAL `task_id`.  When it is present, the minted token is
delivered onto that task by `task_service.attach_approval_token` (which refuses a missing row with
`E_NOT_FOUND` and a row that is not `awaiting_approval` with `E_VALIDATION` — never a silent
metadata overwrite), the row is re-queued (`awaiting_approval` -> `pending`, the exact fields the
dispatcher's claim keys on) and handed to a worker — after the commit, so the run the worker
starts can READ the token it was dispatched with.  When `task_id` is absent the request and
response are byte-identical to the pre-T02b behaviour.

Auth is the same owner API key as every other owner route.  With no `APPROVAL_SECRET` configured
there is no channel at all, and this route says so by name (`E_CONFIG`) instead of minting a token
that could never verify — issuing is impossible by construction, not by protocol.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.policy_firewall import configured_approval_secret, issue_approval
from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_CONFIG, E_REDIS, PolsiaError
from app.schemas.approval import ApprovalIssueRequest, ApprovalIssueResponse
from app.services.task_service import attach_approval_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(verify_api_key)])


class ApprovalIssueRequestWithTask(ApprovalIssueRequest):
    """The mint request plus the OPTIONAL attach target (T02b).

    WHY a subclass declared HERE and not a field on `ApprovalIssueRequest`: the schema module
    `app/schemas/approval.py` is frozen to this lane's edit list, so the additive wire field ships
    in the route module that owns the behaviour.  The declaration itself is the documented pydantic
    v2 shape for an omittable field: a `T | None` ANNOTATION alone is required-but-nullable in v2,
    so the explicit `default=None` is what keeps every pre-T02b request valid (FastAPI docs,
    "Request Body": "when a model attribute has a default value, it is not required").
    """

    #: The PARKED task the minted token is delivered to (`awaiting_approval` rows only).  Absent
    #: means "just mint" — the pre-T02b behaviour, byte for byte.
    task_id: int | None = Field(default=None, ge=1)


async def _attach_and_requeue(db: AsyncSession, task_id: int, token: str) -> None:
    """Deliver the token onto the parked row, commit, then hand the re-queued row to a worker.

    The ordering is deliberate and mirrors `celery_app.tasks.daily_cycle`'s "fan out AFTER the
    commit" rule: a worker that claims the row must be able to read the token it was dispatched
    with, and its claim runs in its own session.  The commit also makes the write independent of
    `get_db`'s teardown commit, so the route behaves identically under a plain test-client session
    override.

    A broker that refuses the hand-off is the named `E_REDIS` refusal — the identical contract
    `app/api/v1/agents.py` implements for the manual trigger, and for the same measured reason
    (A2-03): a publish refusal cannot prove that NOTHING was delivered, so terminalising the row
    `failed` would write off work that may already be queued (and `claim_task` accepts only
    `pending` rows).  The row therefore STAYS `pending`, carries `metadata.rearmed_at` (the
    marker `maintenance._reconcile_stranded_pending` re-dispatches on) plus the `E_REDIS` reason
    for the human reading it, and the caller gets a refusal that says exactly that: accepted and
    re-queued with its token, but NOT dispatched.
    """
    await attach_approval_token(db, task_id, token)   # refuses by name; never a partial write
    await db.commit()
    try:
        from celery_app.tasks.agent_tasks import run_agent_task

        # `.delay()` is a BLOCKING call (the same measured redis-py retry ladder the manual
        # trigger documents); `to_thread` keeps the event loop responsive during the hand-off.
        await asyncio.to_thread(run_agent_task.delay, task_id)
    except Exception as exc:  # broker unavailable: delivery is UNPROVEN, not proven absent
        from app.models.task import Task  # lazy: keeps this module's import graph as it was
        from celery_app.tasks._common import stamp_rearmed  # lazy: worker layer, cycle-free

        logger.warning("approvals: failed to enqueue re-queued task %s", task_id, exc_info=True)
        await stamp_rearmed(db, [task_id], reason=f"enqueue_refused:{exc.__class__.__name__}")
        row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        if row is not None:
            # The refusal lands ON the row while its status keeps the truth: nothing proved the
            # message was never queued, so the row must stay claimable.
            row.error_message = f"{E_REDIS}: enqueue failed ({exc.__class__.__name__})"
        # Load-bearing commit: `stamp_rearmed` only FLUSHES, and the request session is not
        # guaranteed to commit a path that raises right below.
        await db.commit()
        raise PolsiaError(
            E_REDIS,
            f"task #{task_id} was re-queued with its approval token but NOT dispatched: the "
            f"broker refused the hand-off ({exc.__class__.__name__}), so the row stays queued "
            "for the stranded-row reconciler to re-dispatch",
            how_to_fix=(
                "no action is required — the row is pending with its token and the 5-minute "
                "stranded-row sweep re-dispatches it; check REDIS_URL and that the broker and a "
                "worker are running if this keeps happening"
            ),
            context={"task_id": task_id},
        ) from exc


@router.post("", response_model=ApprovalIssueResponse, status_code=status.HTTP_201_CREATED)
async def issue_approval_endpoint(
    payload: ApprovalIssueRequestWithTask,
    db: AsyncSession = Depends(get_db),
) -> ApprovalIssueResponse:
    """Mint a 1h (default) token for ONE action fingerprint — and deliver it when asked.

    A refusal here is NAMED and actionable: an unconfigured channel (`E_CONFIG`, 503), a missing
    attach target (`E_NOT_FOUND`, 404), a task that is not parked (`E_VALIDATION`, 422), or a dead
    broker after a successful attach (`E_REDIS`, 503) — never a success-shaped placeholder.
    """
    with refusal_guard():
        secret = configured_approval_secret()
        if not secret:
            raise PolsiaError(
                E_CONFIG,
                "the approvals channel is not configured: APPROVAL_SECRET is unset, so no token "
                "could ever verify",
                how_to_fix="set APPROVAL_SECRET in .env (see .env.example) and restart the service, "
                           "then mint a token for the fingerprint the parked task recorded",
                context={"action_fingerprint": payload.action_fingerprint},
            )
        minted = issue_approval(
            secret,
            action_fingerprint=payload.action_fingerprint,
            ttl_s=payload.ttl_s,
        )
        if payload.task_id is not None:
            # Inside the guard on purpose: the task-side refusals (404/422) must reach the client
            # named, and a refusal must never leave a half-written row — the helper commits only
            # after the attach itself has succeeded.
            await _attach_and_requeue(db, payload.task_id, minted.token)
    return ApprovalIssueResponse(
        token=minted.token,
        action_fingerprint=minted.action_fingerprint,
        expires_at=int(minted.expires_at),
        ttl_s=payload.ttl_s,
    )
