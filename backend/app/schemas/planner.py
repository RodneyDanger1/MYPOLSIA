"""Planner wire + model contract (wave V9, seat B1) — the FROZEN shape the UI codes against.

Source of truth (verbatim): plan `2026-10-14_142249-polsia-remaining-waves-v7-v10.md` §9 B1
(lines 410-438):

    POST /api/v1/planner/propose  {} -> 201 {"candidates": [{
        "id": str, "name": str, "one_liner": str, "buyer": str, "price_band": [lo, hi],
        "channel": str, "unit_cost_sketch": str,
        "rubric": {"demand": int0-5, "margin": int0-5, "competition": int0-5,
                   "agent_fit": int0-5, "time_to_first_dollar": int0-5, "owner_fit": int0-5,
                   "total": int0-30},
        "gates": {"cm_positive": bool, "budget_fit": bool|null, "hours_fit": bool|null},
        "claims": [{"claim": str, "source_url": str, "fetched_at": str, "quote": str,
                    "verified": bool}]}]}
    POST /api/v1/planner/confirm  {"candidate_id": str} -> 200 {"confirmed": true}
    GET  /api/v1/planner/status -> {"confirmed": {...}|null, "last_proposed_at": str|null}

`claims_unverified` (per candidate, an int) is the ONE additive field the plan itself mandates:
rule (a) — "uncited claims are STRIPPED and counted (`claims_unverified`)".  Every other field
above is quoted from the frozen block; nothing here is invented.

Two model-facing models live here as well: `PlannerProposalDraft` is exactly what the LLM must
return (validated through the repair ladder, queried by `--json-schema`).  Its fields are
SIGNALS, not scores — the rubric is COMPUTED IN PYTHON (`planner_service`) from them + the owner
profile, which is the whole point of rule (b).  The three who-pays fields the pre-gate refuses
on (`buyer`, `price_band`, `channel`) default to empty ON PURPOSE: a missing buyer must be the
route's named 422 `E_VALIDATION` refusal, not a 502 schema-ladder failure.
"""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError


def is_http_url(value: object) -> bool:
    """2R-07 (V2 handoff): the ONE http(s)-scheme predicate for every ingested URL field.

    The render guards shipped first (`safeHref`, the site-package `_website_line`); this is the
    INGEST boundary: a `javascript:` / `data:` / `vbscript:` citation — and empty, relative or
    non-http(s) schemes generally — is never a usable source. Used by `PlannerClaim`'s refusing
    validator, `planner_service`'s claim-sanitize + fetched-URL legs, and
    `agent_outputs.Competitor`'s provenance filter, so all three read the SAME rule.
    """
    if not isinstance(value, str):
        return False
    parts = urlsplit(value.strip())
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


# ---------------------------------------------------------------------------
# model-facing (the LLM's output, validated by the repair ladder)
# ---------------------------------------------------------------------------

class PlannerClaimDraft(BaseModel):
    """One claim the model makes, with the citation it took it from."""

    claim: str
    source_url: str = ""
    quote: str = ""


class PlannerIdeaDraft(BaseModel):
    """One candidate as the model proposes it: who-pays fields + the rubric's input signals."""

    name: str
    one_liner: str = ""
    buyer: str = ""
    price_band: list[float] = Field(default_factory=list)   # [low, high] USD per sale
    channel: str = ""
    unit_cost_sketch: str = ""
    est_variable_cost: float                                 # USD per sale (CM gate input)
    launch_cost_usd: float                                   # cash the start needs (budget gate)
    days_to_first_dollar: int                                # days until first payment
    competition_level: Literal["none", "low", "medium", "high", "saturated"]
    fleet_fit: Literal["high", "medium", "low"]
    demand_grade: Literal["proven", "likely", "plausible", "untested"]
    owner_hours_per_week_needed: int
    claims: list[PlannerClaimDraft] = Field(default_factory=list)


class PlannerProposalDraft(BaseModel):
    """The planner agent's whole output: the candidate list (3-5, enforced in the service)."""

    ideas: list[PlannerIdeaDraft] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# wire (the frozen response objects)
# ---------------------------------------------------------------------------

class RubricScores(BaseModel):
    """The six dimensions + total — computed in Python, never by the model."""

    demand: int
    margin: int
    competition: int
    agent_fit: int
    time_to_first_dollar: int
    owner_fit: int
    total: int


class CandidateGates(BaseModel):
    """`cm_positive` is the HARD gate (always bool); the other two are null when the owner
    profile has no measured value for them (NULL is "unmeasured", never a fabricated pass)."""

    cm_positive: bool
    budget_fit: bool | None = None
    hours_fit: bool | None = None


class PlannerClaim(BaseModel):
    """A claim that survived provenance verification: it cites a URL the fetch step recorded."""

    claim: str
    source_url: str
    fetched_at: str
    quote: str
    verified: bool

    @field_validator("source_url")
    @classmethod
    def _source_url_must_be_http(cls, value: str) -> str:
        """2R-07: refuse a non-http(s) citation BY NAME at the wire boundary (E_VALIDATION).

        `verify_claims` already dropped anything that is not an absolute http(s) URL (it can
        never be "fetched"), so a claim reaching this model carries a real citation or it does
        not reach the response at all — this validator is the last, loudest layer: a fabricated
        or hostile scheme must never ship inside a `verified: True` claim.
        """
        if not is_http_url(value):
            raise PolsiaError(
                E_VALIDATION,
                f"planner claim source_url must be an absolute http(s) URL (got {value!r}): "
                "a non-http(s) citation can never be a verified source",
                how_to_fix=ERROR_HINTS[E_VALIDATION],
                context={"source_url": value},
            )
        return value


class PlannerCandidate(BaseModel):
    """One proposed candidate in the frozen contract's exact field set (+ claims_unverified)."""

    id: str
    name: str
    one_liner: str
    buyer: str
    price_band: list[float]
    channel: str
    unit_cost_sketch: str
    rubric: RubricScores
    gates: CandidateGates
    claims: list[PlannerClaim]
    claims_unverified: int


class ProposeResponse(BaseModel):
    """The frozen 201 body of `POST /api/v1/planner/propose`."""

    candidates: list[PlannerCandidate]


class ConfirmRequest(BaseModel):
    """`body {"candidate_id": str}` — one id from a propose response."""

    candidate_id: str


class ConfirmResponse(BaseModel):
    """`200 {"confirmed": true}`."""

    confirmed: bool


class StatusResponse(BaseModel):
    """`{"confirmed": {...}|null, "last_proposed_at": str|null}` — nulls stay null."""

    confirmed: dict[str, Any] | None = None
    last_proposed_at: str | None = None


__all__ = [
    "CandidateGates",
    "ConfirmRequest",
    "ConfirmResponse",
    "PlannerCandidate",
    "PlannerClaim",
    "PlannerClaimDraft",
    "PlannerIdeaDraft",
    "PlannerProposalDraft",
    "ProposeResponse",
    "RubricScores",
    "StatusResponse",
    "is_http_url",
]
