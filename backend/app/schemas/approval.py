"""Approvals channel wire shapes (D1 / T02) — `POST /api/v1/approvals`.

The owner-facing mint half of the approval channel: an action a completed run recorded as
`requires_approval` carries an `action_fingerprint` (the firewall's
`policy_firewall.fingerprint()` digest of action class + channel + payload); the owner posts that
fingerprint here and receives a TTL-bounded HMAC token, which the NEXT run verifies
(`policy_firewall.verify_approval` via `decide()`) to let the gated action PROCEED.

Deliberately small: no free-text "approve everything" field exists, because the token mechanism
is fingerprint-scoped — a request shape that could not be verified would be a fake capability.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ApprovalIssueRequest(BaseModel):
    """Mint a token for ONE action fingerprint (the value the parked task recorded)."""

    #: The firewall fingerprint the token is scoped to (`policy_firewall.fingerprint()` — 24 hex
    #: chars today; the bound is generous on purpose, the verifier is exact).
    action_fingerprint: str = Field(min_length=8, max_length=128)
    #: Token lifetime in seconds.  Default 1h (D1); bounded to a day — a bearer token that outlives
    #: the operator's attention span is not an approval.
    ttl_s: int = Field(default=3600, ge=1, le=86400)


class ApprovalIssueResponse(BaseModel):
    """The minted token + the facts needed to use it without guessing."""

    token: str
    action_fingerprint: str
    #: Epoch seconds the token stops verifying (the same instant embedded in the token).
    expires_at: int
    ttl_s: int
