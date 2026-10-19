"""Planner service — proposal assembly, the PYTHON rubric/gates, and the column-free ledger.

Implements plan §9 B1 (rules a-e) behind the frozen contract in `app.schemas.planner`:

  * **propose** runs two steps through the SAME seams the fleet uses.  The FETCH step is the
    shipped `competitor_research` agent (WebSearch/WebFetch through its capability allowlist;
    the V8/C5 `RESEARCH_FETCH_BUDGET` is checked post-hoc with the SAME `research_budget_violation`
    helper, and an over-budget run records ONE honest `research_budget_exceeded` activity row —
    tier (a), code-gap #11: recorded, never silently overrun).  The MODEL step is the new
    `company_planner` agent through `BasePolsiaAgent.collect_structured` — the same callable
    every agent uses to reach the model.  Python then enforces the who-pays pre-gate
    (422 `E_VALIDATION`), computes the rubric + gates, and strips uncited claims.  propose
    writes ONE activity row — the column-free ledger this wave's dunning plan establishes
    (B4: "no new columns") — which doubles as `last_proposed_at` (status) and the confirm
    lookup.  It touches no task, run, post or email: NOTHING SPENDS.
  * **confirm** looks the candidate up in the LATEST propose ledger row (an unknown id is a
    named `E_NOT_FOUND`), writes `company_config.confirmed_idea`, invalidates the
    company-context cache, and logs ONE row.  It runs nothing.
  * **status** answers the confirmed object (or null) + the latest propose row's OWN
    `created_at` (or null) — a real clock read, never a fabricated timestamp.

Rubric (six dimensions 0-5, total 0-30, ALL computed here from the candidate's structured
signals + the owner profile `owner_hours_per_week` / `owner_budget_usd` from 0008):

  demand   = demand_grade: proven 5, likely 4, plausible 2, untested 0
  margin   = gross ratio against the LOW price, (lo - est_variable_cost)/lo:
             >=0.75 -> 5, >=0.60 -> 4, >=0.45 -> 3, >=0.30 -> 2, >0 -> 1, else 0
  competition = none 5, low 4, medium 3, high 1, saturated 0 (higher = friendlier field)
  agent_fit = fleet_fit: high 5, medium 3, low 1
  time_to_first_dollar = days: <=7 -> 5, <=14 -> 4, <=30 -> 3, <=60 -> 2, <=90 -> 1, else 0
  owner_fit = WITH a measured owner_hours_per_week H: needed <= H*0.5 -> 5, <= H -> 4,
              <= H*1.5 -> 2, <= H*2 -> 1, else 0; WITHOUT one, the idea's own weekly need is
              scored on a fixed scale: <=2 -> 5, <=5 -> 4, <=10 -> 3, <=20 -> 1, else 0 —
              never fabricated from an owner figure that does not exist.

Gates: `cm_positive = price_band[0] - est_variable_cost > 0` (the HARD gate, compared in exact
cents via Decimal(str(...)) — the repo's A-T18 boundary rule); `budget_fit` = launch cost fits
the owner budget, or None when the budget is unmeasured; `hours_fit` = needed hours fit the
owner hours, or None when those are unmeasured.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.company_planner.agent import CompanyPlannerAgent
from app.agents.competitor_research.agent import (
    RESEARCH_FETCH_BUDGET,
    CompetitorResearchAgent,
    research_budget_violation,
)
from app.core.database import utcnow
from app.core.errors import (
    E_CLI_TIMEOUT,
    E_NOT_FOUND,
    E_TASK_CLAIMED,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.models.company import CompanyConfig
from app.models.report import ActivityLog
from app.schemas.planner import is_http_url
from app.services.activity_service import log_activity
from app.services.company_service import get_company, invalidate_company_context
from app.services.query_helpers import fetch_scalar

#: The agent_type every planner ledger row (and the confirm/status reads) is keyed by.
PLANNER_AGENT_TYPE = "company_planner"
#: Activity actions — the plan's column-free ledger (dunning plan, V9 B4: activity rows as the
#: record).  `idea_proposed` doubles as last_proposed_at + the confirm lookup.
PROPOSED_ACTION = "idea_proposed"
CONFIRMED_ACTION = "idea_confirmed"
BUDGET_EXCEEDED_ACTION = "research_budget_exceeded"

#: The contract's candidate bounds (plan: "candidates 3-5").
MIN_CANDIDATES = 3
MAX_CANDIDATES = 5

#: F3 (V11/W2): the bounded step timeout — a model/fetch step that overruns refuses by name
#: instead of holding the request (and the process's model budget) open indefinitely.  The
#: RESPONSE is bounded here; the worker thread unwinds when the runtime's own timeout fires.
PLANNER_STEP_TIMEOUT_SECONDS = 300.0
#: The research step's agent_type (the budget gate's per-agent window, same name the worker uses).
PLANNER_RESEARCH_AGENT_TYPE = "competitor_research"
#: In-flight dedupe (F3): ONE proposal at a time per process — a second concurrent run would
#: double the model spend for a duplicate answer.  (Single-worker API process: an asyncio lock
#: IS the process-wide guard; multi-replica deployments would need a shared marker — recorded,
#: not hidden.)
_PROPOSE_IN_FLIGHT = False
_PROPOSE_LOCK = asyncio.Lock()

_DEMAND_SCORES: dict[str, int] = {"proven": 5, "likely": 4, "plausible": 2, "untested": 0}
_COMPETITION_SCORES: dict[str, int] = {"none": 5, "low": 4, "medium": 3, "high": 1, "saturated": 0}
_FLEET_FIT_SCORES: dict[str, int] = {"high": 5, "medium": 3, "low": 1}


# ── numeric helpers ──────────────────────────────────────────────────────────────────────────

def _as_decimal(value: Any) -> Decimal | None:
    """Exact Decimal for a JSON number / Decimal / numeric string, else None.

    Boundary rule A-T18: money crosses into a decision through `Decimal(str(x))` — a JSON
    float's shortest repr is the decimal it was written as.  Bools are NOT numbers here, and
    NaN/inf are unmeasurable (None), never coerced.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _low_price(idea: dict[str, Any]) -> Decimal | None:
    """The low end of `price_band` as a Decimal, or None when it is not a 2-number band."""
    band = idea.get("price_band")
    if not isinstance(band, (list, tuple)) or len(band) != 2:
        return None
    return _as_decimal(band[0])


def _plain_int(value: Any) -> int | None:
    """The value as a plain int (bools are NOT ints here), or None for anything else.

    Explicit `int | None` return (not a bool predicate) so the type checker narrows after the
    None check — the same rule the rubric applies at runtime: a signal that is not a real
    integer is unmeasured, never coerced.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ── the rubric (pure functions; pinned by tests/unit/test_v9_rubric.py) ──────────────────────

def _margin_score(idea: dict[str, Any]) -> int:
    low = _low_price(idea)
    cost = _as_decimal(idea.get("est_variable_cost"))
    if low is None or cost is None or low <= 0:
        return 0
    contribution = low - cost
    if contribution <= 0:
        return 0
    ratio = contribution / low
    for floor, score in (
        (Decimal("0.75"), 5),
        (Decimal("0.60"), 4),
        (Decimal("0.45"), 3),
        (Decimal("0.30"), 2),
    ):
        if ratio >= floor:
            return score
    return 1


def _time_score(idea: dict[str, Any]) -> int:
    days = _plain_int(idea.get("days_to_first_dollar"))
    if days is None or days < 0:
        return 0
    for cutoff, score in ((7, 5), (14, 4), (30, 3), (60, 2), (90, 1)):
        if days <= cutoff:
            return score
    return 0


def _owner_fit_score(idea: dict[str, Any], owner_hours_per_week: int | None) -> int:
    need = _plain_int(idea.get("owner_hours_per_week_needed"))
    if need is None or need < 0:
        return 0
    if owner_hours_per_week is not None:
        if need <= owner_hours_per_week * 0.5:
            return 5
        if need <= owner_hours_per_week:
            return 4
        if need <= owner_hours_per_week * 1.5:
            return 2
        if need <= owner_hours_per_week * 2:
            return 1
        return 0
    if need <= 2:
        return 5
    if need <= 5:
        return 4
    if need <= 10:
        return 3
    if need <= 20:
        return 1
    return 0


def compute_rubric(
    idea: dict[str, Any],
    *,
    owner_hours_per_week: int | None,
    owner_budget_usd: Any | None,
) -> dict[str, int]:
    """The frozen rubric: six 0-5 dimensions + total 0-30, deterministic from `idea` signals
    and the owner profile.  `owner_budget_usd` is accepted for one-call symmetry with
    `compute_gates`; the budget drives `gates.budget_fit`, not a rubric dimension (the frozen
    rubric has no budget dimension)."""
    demand = _DEMAND_SCORES.get(str(idea.get("demand_grade")), 0)
    margin = _margin_score(idea)
    competition = _COMPETITION_SCORES.get(str(idea.get("competition_level")), 0)
    agent_fit = _FLEET_FIT_SCORES.get(str(idea.get("fleet_fit")), 0)
    time_to_first_dollar = _time_score(idea)
    owner_fit = _owner_fit_score(idea, owner_hours_per_week)
    return {
        "demand": demand,
        "margin": margin,
        "competition": competition,
        "agent_fit": agent_fit,
        "time_to_first_dollar": time_to_first_dollar,
        "owner_fit": owner_fit,
        "total": demand + margin + competition + agent_fit + time_to_first_dollar + owner_fit,
    }


def compute_gates(
    idea: dict[str, Any],
    *,
    owner_hours_per_week: int | None,
    owner_budget_usd: Any | None,
) -> dict[str, bool | None]:
    """The frozen gates.  `cm_positive` is the HARD gate (`price_band[0] - est_variable_cost
    > 0`, exact cents) and is always a bool; `budget_fit` / `hours_fit` are null exactly when
    the owner profile does not measure the figure they need."""
    low = _low_price(idea)
    cost = _as_decimal(idea.get("est_variable_cost"))
    cm_positive = low is not None and cost is not None and (low - cost) > 0

    launch = _as_decimal(idea.get("launch_cost_usd"))
    budget = _as_decimal(owner_budget_usd)
    budget_fit: bool | None = None if (budget is None or launch is None) else launch <= budget

    need = _plain_int(idea.get("owner_hours_per_week_needed"))
    hours_fit: bool | None = None
    if owner_hours_per_week is not None and need is not None:
        hours_fit = need <= owner_hours_per_week

    return {"cm_positive": cm_positive, "budget_fit": budget_fit, "hours_fit": hours_fit}


# ── claims: provenance verification (rule a) ─────────────────────────────────────────────────

def verify_claims(
    claims: Any,
    *,
    fetched_urls: set[str],
    fetched_at: str,
) -> tuple[list[dict[str, Any]], int]:
    """Keep only claims citing a URL the fetch step actually recorded; count the rest.

    Returns `(kept, dropped)`.  A kept claim carries the provenance the contract freezes:
    the citation (`source_url`), the `quote`, the clock value of the FETCH (`fetched_at` — the
    fetch step's completion time, stamped by the service, never by the model) and
    `verified: True`.  A claim with no citation, or citing a page nobody fetched, is dropped
    and counted — the count is what `claims_unverified` reports, the claim itself never ships.
    """
    fetched = {url.strip() for url in fetched_urls if isinstance(url, str) and url.strip()}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for raw in claims if isinstance(claims, list) else []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        url = str(raw.get("source_url") or "").strip()
        # 2R-07: the sanitize leg. A non-http(s) citation (javascript:/data:/vbscript:, empty,
        # relative) is dropped + counted exactly like an uncited claim — even when the same
        # hostile string sits in `fetched_urls` (a sloppy or injected research output must not
        # launder a `verified: True` claim out of it). The count is the recording: it ships as
        # `claims_unverified`.
        if not url or not is_http_url(url) or url not in fetched:
            dropped += 1
            continue
        kept.append(
            {
                "claim": str(raw.get("claim") or ""),
                "source_url": url,
                "fetched_at": fetched_at,
                "quote": str(raw.get("quote") or ""),
                "verified": True,
            }
        )
    return kept, dropped


def _fetched_urls(research: Any) -> set[str]:
    """Every URL the fetch step recorded, from the research output's provenance fields."""
    urls: set[str] = set()
    competitors = research.get("competitors") if isinstance(research, dict) else None
    for competitor in competitors if isinstance(competitors, list) else []:
        if not isinstance(competitor, dict):
            continue
        for key in ("source_urls", "sources"):
            values = competitor.get(key)
            if isinstance(values, list):
                # 2R-07: only absolute http(s) URLs are recordable provenance — a `javascript:`
                # string in the research output can never become a URL a claim verifies against.
                urls.update(
                    str(value).strip()
                    for value in values
                    if isinstance(value, str) and value.strip() and is_http_url(value)
                )
    return urls


# ── the who-pays pre-gate (rule c) ───────────────────────────────────────────────────────────

def require_who_pays(idea: dict[str, Any], index: int) -> None:
    """A candidate missing buyer / price / payment path fails validation — 422 `E_VALIDATION`.

    "Who pays, how much, through which path" is the minimum viable business statement; a
    proposal that cannot state it is refused BY NAME before anything is written, rather than
    shipped as a half-idea the owner has to decode.
    """
    missing: list[str] = []
    if not str(idea.get("buyer") or "").strip():
        missing.append("buyer")
    low = _low_price(idea)
    high = None
    band = idea.get("price_band")
    if isinstance(band, (list, tuple)) and len(band) == 2:
        high = _as_decimal(band[1])
    if low is None or high is None or low <= 0 or high < low:
        missing.append("price_band")
    if not str(idea.get("channel") or "").strip():
        missing.append("channel")
    if missing:
        raise PolsiaError(
            E_VALIDATION,
            f"planner candidate #{index} ({idea.get('name') or 'unnamed'}) is missing who-pays "
            f"prerequisites: {', '.join(missing)}",
            how_to_fix=(
                "every candidate must name a buyer, a price band [low, high] and a payment "
                "channel/path — re-run propose so the model supplies them"
            ),
            context={"index": index, "missing": missing, "name": idea.get("name")},
        )


# ── the two agent seams (mockable; each runs the SAME machinery the fleet runs) ──────────────

def _research_sync(profile: dict[str, Any]) -> dict[str, Any]:
    """The fetch step: run the SHIPPED competitor_research agent (same mechanism, same budget).

    Sync by design (the agent machinery is sync); the async service calls it through
    `asyncio.to_thread` — the same way the worker runs agents, so the contextvar/publish
    behaviour is identical to a dispatched run.
    """
    agent = CompetitorResearchAgent()
    task = {
        "title": "Planner landscape research",
        "description": (
            "Find priced comparables, buyer behaviour, payment channels and demand evidence for "
            "business ideas this company could start. Record every URL you actually used."
        ),
    }
    return agent.timed_run(task, {"company": profile})


def _proposal_sync(profile: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    """The model step: run the company_planner agent with the research as quarantined DATA."""
    agent = CompanyPlannerAgent()
    task = {
        "title": "Propose business candidates",
        "description": (
            "Propose 3-5 who-pays business candidates from the research block, with rubric "
            "signals and cited claims."
        ),
    }
    return agent.timed_run(task, {"company": profile, "research": research})


# ── F3 (V11/W2): the money gate, the bounded step, the in-flight dedupe ─────────────────────

async def _gate_planner_budget(db: AsyncSession, agent_type: str, *, step: str) -> None:
    """The day-cap gate in front of a planner agent step — the worker choke point's policy.

    Read through the CALLER's session (`day_spend_by_agent(db)`): the check sees the same
    company-local day and the same committed spend the request already has in view.  A
    `BudgetStop` (`E_BUDGET_EXCEEDED`, or `E_POLICY_DENIED` for the kill switch) is re-raised
    as the named refusal WITH its how_to_fix — and nothing has run when it raises.
    """
    from app.agents.accounting import BudgetStop, authorize, budget_policy_from_settings
    from app.agents.policy_firewall import kill_grade_from_env

    # Lazy import, app-side precedent `support_inbox_service`: the shared spend reader lives
    # with the worker machinery; everything else here is dependency-light.
    from celery_app.tasks._common import day_spend_by_agent

    policy = budget_policy_from_settings(kill_switch=kill_grade_from_env() == "freeze")
    total, per_agent = await day_spend_by_agent(db)
    try:
        authorize(
            policy,
            agent_type=agent_type,
            est_cost_usd=policy.per_run_cap_usd,
            day_spent_usd=total,
            agent_spent_usd=per_agent.get(agent_type, 0.0),
        )
    except BudgetStop as exc:
        raise PolsiaError(
            exc.code,
            f"planner {step} refused before dispatch: {exc.message}",
            how_to_fix=ERROR_HINTS.get(exc.code, ""),
            context={**(exc.context or {}), "step": step, "agent_type": agent_type},
        ) from exc


async def _run_planner_step(fn, *args: Any, step: str) -> dict[str, Any]:
    """One agent step behind the bounded timeout (F3) — a timeout is a NAMED refusal.

    The RESPONSE is bounded here; the worker thread unwinds when the runtime's own timeout
    fires (a thread cannot be killed — recorded, not hidden).
    """
    try:
        async with asyncio.timeout(PLANNER_STEP_TIMEOUT_SECONDS):
            return await asyncio.to_thread(fn, *args)
    except TimeoutError as exc:
        raise PolsiaError(
            E_CLI_TIMEOUT,
            f"the planner {step} step exceeded the {PLANNER_STEP_TIMEOUT_SECONDS:g}s bound",
            how_to_fix=(
                "retry propose; if it keeps timing out, the model/fetch backend is degraded "
                "(raise PLANNER_STEP_TIMEOUT_SECONDS only with day-cap headroom)"
            ),
            context={"step": step, "timeout_seconds": PLANNER_STEP_TIMEOUT_SECONDS},
        ) from exc


# ── assembly ─────────────────────────────────────────────────────────────────────────────────

def _company_profile(company: CompanyConfig) -> dict[str, Any]:
    """The JSON-safe company/owner profile the agents' prompts are assembled from."""
    return {
        "id": company.id,
        "name": company.name,
        "mission": company.mission,
        "vision": company.vision,
        "description": company.description,
        "target_market": company.target_market,
        "value_prop": company.value_prop,
        "industry": company.industry,
        "product_type": company.product_type,
        "owner_skills": company.owner_skills,
        "owner_goal": company.owner_goal,
        "risk_tolerance": company.risk_tolerance,
        "market_locale": company.market_locale,
        "owner_hours_per_week": company.owner_hours_per_week,
        "owner_budget_usd": str(company.owner_budget_usd) if company.owner_budget_usd is not None else None,
    }


def _assemble_candidate(
    idea: dict[str, Any],
    *,
    owner_hours_per_week: int | None,
    owner_budget_usd: Any | None,
    fetched_urls: set[str],
    fetched_at: str,
) -> dict[str, Any]:
    """One frozen candidate: stripped claims first, then the Python rubric + gates."""
    claims, unverified = verify_claims(
        idea.get("claims"), fetched_urls=fetched_urls, fetched_at=fetched_at
    )
    band = idea.get("price_band") or [0, 0]
    return {
        "id": f"idea-{uuid.uuid4().hex[:12]}",
        "name": str(idea.get("name") or ""),
        "one_liner": str(idea.get("one_liner") or ""),
        "buyer": str(idea.get("buyer") or ""),
        "price_band": [float(band[0]), float(band[1])],
        "channel": str(idea.get("channel") or ""),
        "unit_cost_sketch": str(idea.get("unit_cost_sketch") or ""),
        "rubric": compute_rubric(
            idea, owner_hours_per_week=owner_hours_per_week, owner_budget_usd=owner_budget_usd
        ),
        "gates": compute_gates(
            idea, owner_hours_per_week=owner_hours_per_week, owner_budget_usd=owner_budget_usd
        ),
        "claims": claims,
        "claims_unverified": unverified,
    }


# ── the three service entry points (behind the frozen routes) ────────────────────────────────

async def propose(db: AsyncSession) -> dict[str, Any]:
    """Fetch -> model -> pre-gate -> rubric -> ledger; budgeted, bounded, de-duplicated (F3).

    Named refusals, all before (or instead of) agent work:
    * `E_TASK_CLAIMED` — a proposal is already running in this process (in-flight dedupe:
      a second concurrent run would double the model spend for a duplicate answer);
    * `E_BUDGET_EXCEEDED` — the day's spend window is exhausted (the SAME policy and reader
      the worker choke point enforces) — raised per agent type before its step;
    * `E_CLI_TIMEOUT` — a step overran `PLANNER_STEP_TIMEOUT_SECONDS`.
    """
    global _PROPOSE_IN_FLIGHT
    if _PROPOSE_LOCK.locked() or _PROPOSE_IN_FLIGHT:
        raise PolsiaError(
            E_TASK_CLAIMED,
            "a planner proposal is already running; a concurrent second run would double "
            "the model spend for a duplicate answer",
            how_to_fix="wait for the running proposal to answer, then retry",
        )
    await _PROPOSE_LOCK.acquire()
    _PROPOSE_IN_FLIGHT = True
    try:
        return await _propose_once(db)
    finally:
        _PROPOSE_IN_FLIGHT = False
        _PROPOSE_LOCK.release()


async def _propose_once(db: AsyncSession) -> dict[str, Any]:
    """The body of `propose` (see its docstring for the guards) — fetch, model, rubric, ledger."""
    company = await get_company(db)
    if company is None:
        raise PolsiaError(
            E_NOT_FOUND,
            "the planner has no company to plan for: no company_config row exists",
            how_to_fix="seed the company first (make seed or PUT /api/v1/config), then propose",
            context={"agent_type": PLANNER_AGENT_TYPE},
        )
    profile = _company_profile(company)

    await _gate_planner_budget(db, PLANNER_RESEARCH_AGENT_TYPE, step="research fetch")
    research = await _run_planner_step(_research_sync, profile, step="research fetch")
    fetched_at = utcnow().isoformat()
    violation = research_budget_violation(research)
    if violation is not None:
        await log_activity(
            db,
            PLANNER_AGENT_TYPE,
            BUDGET_EXCEEDED_ACTION,
            (
                f"research step recorded {violation['count']} fetches over the "
                f"{RESEARCH_FETCH_BUDGET} budget (recorded only — tier (a), code-gap #11)"
            ),
            level="warning",
            detail=violation,
        )
    fetched_urls = _fetched_urls(research)

    await _gate_planner_budget(db, PLANNER_AGENT_TYPE, step="model proposal")
    draft = await _run_planner_step(_proposal_sync, profile, research, step="model proposal")
    raw_ideas = draft.get("ideas") if isinstance(draft, dict) else None
    ideas = [idea for idea in (raw_ideas or []) if isinstance(idea, dict)]
    if not (MIN_CANDIDATES <= len(ideas) <= MAX_CANDIDATES):
        raise PolsiaError(
            E_VALIDATION,
            f"the planner proposed {len(ideas)} candidate(s); the contract is "
            f"{MIN_CANDIDATES}-{MAX_CANDIDATES}",
            how_to_fix="re-run propose; the planner ROLE instructs 3-5 sourced candidates",
            context={"count": len(ideas)},
        )
    for index, idea in enumerate(ideas):
        require_who_pays(idea, index)

    candidates = [
        _assemble_candidate(
            idea,
            owner_hours_per_week=company.owner_hours_per_week,
            owner_budget_usd=company.owner_budget_usd,
            fetched_urls=fetched_urls,
            fetched_at=fetched_at,
        )
        for idea in ideas
    ]
    passing = sum(1 for candidate in candidates if candidate["gates"]["cm_positive"])
    await log_activity(
        db,
        PLANNER_AGENT_TYPE,
        PROPOSED_ACTION,
        f"proposed {len(candidates)} business candidate(s); {passing} pass the CM gate",
        detail={
            "candidates": candidates,
            "fetched_at": fetched_at,
            "fetched_urls": sorted(fetched_urls),
        },
    )
    return {"candidates": candidates}


async def confirm(db: AsyncSession, candidate_id: str) -> bool:
    """Write the chosen candidate into `company_config.confirmed_idea` (and nothing else)."""
    company = await get_company(db)
    if company is None:
        raise PolsiaError(
            E_NOT_FOUND,
            "the planner has no company to confirm an idea for: no company_config row exists",
            how_to_fix="seed the company first (make seed or PUT /api/v1/config)",
            context={"agent_type": PLANNER_AGENT_TYPE},
        )
    latest = await fetch_scalar(
        db,
        select(ActivityLog.detail)
        .where(ActivityLog.agent_type == PLANNER_AGENT_TYPE, ActivityLog.action == PROPOSED_ACTION)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(1),
        what="the latest planner proposal",
    )
    if latest is None:
        raise PolsiaError(
            E_NOT_FOUND,
            "there is no planner proposal on record to confirm",
            how_to_fix="call POST /api/v1/planner/propose first, then confirm one of its ids",
            context={"candidate_id": candidate_id},
        )
    candidates = latest.get("candidates") if isinstance(latest, dict) else None
    match = next(
        (
            candidate
            for candidate in (candidates or [])
            if isinstance(candidate, dict) and candidate.get("id") == candidate_id
        ),
        None,
    )
    if match is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"candidate id {candidate_id!r} is not in the latest proposal",
            how_to_fix=(
                "confirm one of the ids returned by the latest POST /api/v1/planner/propose "
                "response"
            ),
            context={"candidate_id": candidate_id},
        )
    company.confirmed_idea = match
    # The context cache holds the company for up to 30s; the confirmed idea must be visible to
    # the NEXT get_full_context (rule e), so the write evicts the cache exactly like PUT /config does.
    invalidate_company_context()
    await log_activity(
        db,
        PLANNER_AGENT_TYPE,
        CONFIRMED_ACTION,
        f"confirmed idea {match.get('name')!r} ({match.get('id')})",
        detail={"candidate_id": match.get("id"), "name": match.get("name")},
    )
    return True


async def status(db: AsyncSession) -> dict[str, Any]:
    """The confirmed idea (or null) + when the last proposal ran (or null).  Read-only."""
    company = await get_company(db)
    confirmed = company.confirmed_idea if company is not None else None
    last_proposed = await fetch_scalar(
        db,
        select(ActivityLog.created_at)
        .where(ActivityLog.agent_type == PLANNER_AGENT_TYPE, ActivityLog.action == PROPOSED_ACTION)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(1),
        what="the latest planner proposal timestamp",
    )
    return {
        "confirmed": confirmed or None,
        "last_proposed_at": last_proposed,
    }


__all__ = [
    "BUDGET_EXCEEDED_ACTION",
    "CONFIRMED_ACTION",
    "MAX_CANDIDATES",
    "MIN_CANDIDATES",
    "PLANNER_AGENT_TYPE",
    "PROPOSED_ACTION",
    "compute_gates",
    "compute_rubric",
    "confirm",
    "propose",
    "require_who_pays",
    "status",
    "verify_claims",
]
