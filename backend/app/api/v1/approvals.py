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

Auth is the same owner API key as every other owner route.  With no `APPROVAL_SECRET` configured
there is no channel at all, and this route says so by name (`E_CONFIG`) instead of minting a token
that could never verify — issuing is impossible by construction, not by protocol.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.agents.policy_firewall import configured_approval_secret, issue_approval
from app.api.deps import refusal_guard, verify_api_key
from app.core.errors import E_CONFIG, PolsiaError
from app.schemas.approval import ApprovalIssueRequest, ApprovalIssueResponse

router = APIRouter(prefix="/approvals", tags=["approvals"], dependencies=[Depends(verify_api_key)])


@router.post("", response_model=ApprovalIssueResponse, status_code=status.HTTP_201_CREATED)
async def issue_approval_endpoint(payload: ApprovalIssueRequest) -> ApprovalIssueResponse:
    """Mint a 1h (default) token for ONE action fingerprint.

    A refusal here is NAMED and actionable: the only way this route cannot mint is an unconfigured
    channel (`E_CONFIG`, 503) — never a success-shaped placeholder.
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
    return ApprovalIssueResponse(
        token=minted.token,
        action_fingerprint=minted.action_fingerprint,
        expires_at=int(minted.expires_at),
        ttl_s=payload.ttl_s,
    )
