"""Company profile -> agent context dict -> prompt text.

Shape contract (pinned by `tests/unit/test_company_service.py`): `{}` — an empty
dict with NO keys — when there is no company row, and `company` / `kpis` keys when
there is one.  This module never returns a partially-filled dict.

Two changes from the original implementation:

* **Fetch only what the prompt needs.**  The original loaded a whole `DailyReport`
  ORM row and 50 whole `Task` rows to use one column of each.  The report query now
  selects `evening_summary` alone; the task query the six fields `todays_tasks`
  carries.
* **A small TTL cache with an explicit invalidation path.**  `get_full_context` is
  called once per agent run and twice per daily cycle, and its content only changes
  when the company row changes or the company-local day rolls over.  The cache is
  keyed by engine and additionally checks that the *company's* local day is still
  the day the entry was built for, so it rolls over at local midnight rather than at
  an arbitrary TTL boundary.  `invalidate_company_context()` is the explicit
  eviction path and is called by `PUT /api/v1/config`, the one route that can change
  the row.

The cache never memoises "there is no company": a miss is cheap and caching it
would make first boot (before `make seed`) sticky.
"""
from __future__ import annotations

import copy
import math
import time
import weakref
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.timeutil import day_window, today_in
from app.models.company import CompanyConfig
from app.models.report import ActivityLog, DailyReport
from app.models.task import Task
from app.services import economics_service
from app.services.query_helpers import fetch_rows, fetch_scalar

NO_CONTEXT = "No company context available."

#: ── F-18 (wave 3): the confirmed idea's PROVENANCE ────────────────────────────────────────────
#:
#: `company_config.confirmed_idea` is a JSON bag written by the Planner's confirm step.  It carries
#: no timestamp of its own, and the profile it was confirmed against lives in a DIFFERENT row of
#: the same table — so after the owner edits the company (a new name, a new product, a new
#: market), the old idea silently becomes a statement about a business that no longer exists,
#: while every prompt still called it "CONFIRMED IDEA".  Measured live (VISUALS-2 F-18): a
#: candle/soap company was briefed with an "AI Marketing Ops Retainer for SMB Agencies" idea
#: confirmed fourteen days and several profile edits earlier.
#:
#: The honest signal available without inventing a migration: the Planner logs one
#: `idea_confirmed` activity row in the SAME transaction as the write (`planner_service.confirm`),
#: and `company_config.updated_at` is bumped by any later edit.  So "the profile was edited after
#: the idea was confirmed" is a MEASURED fact, not a guess.  When the idea is stale, or when no
#: confirmation is on record at all, the renderer never presents it as a current fact.
#:
#: The two literals below are `planner_service.PLANNER_AGENT_TYPE` / `CONFIRMED_ACTION`.  They are
#: written out here (and pinned by a test that compares them) because `planner_service` imports
#: THIS module — a module-level import back would be a cycle.
IDEA_AGENT_TYPE = "company_planner"
IDEA_CONFIRMED_ACTION = "idea_confirmed"

IDEA_NONE = "none"
IDEA_CONFIRMED = "confirmed"
IDEA_STALE = "stale"
IDEA_UNVERIFIED = "unverified"

#: ── F-W3-1 (wave 4): KPI PROVENANCE, stated where the KPIs are RENDERED ───────────────────────
#:
#: `company_config.kpis` holds values the OWNER typed into Settings.  They are CONFIGURED data —
#: targets and bookkeeping, not measurements — but three surfaces rendered them as bare numbers:
#: the agent prompt's `business_state` layer (`KPIs:\n- mrr_usd: 0`), `build_context_prompt`, and
#: (before its TS-4 fix) the dashboard card.  Live evidence: finance run 313 reported *"Company KPI
#: active_customers = 0"* as a fact, and the same stack's `/finance/summary` said MRR/subscribers
#: were UNMEASURED (`mrr_cents: null`).  A configured zero and a measured zero are different facts;
#: the only measured money in this app lives in `revenue_snapshots`, read by the finance agent's
#: revenue-facts block and by `/finance/summary`.
#:
#: So the rendering now carries the source: `configured` when the row holds at least one KPI key,
#: `unset` when it holds none (an empty dict is not "configured values"), and the prompt label says
#: out loud that a configured KPI is NOT a measurement.  ONE helper so the wire, the prompt and the
#: CLI fallback cannot drift.
KPI_PROVENANCE_UNSET = "unset"
KPI_PROVENANCE_CONFIGURED = "configured"

#: How long a computed context may be reused.  Deliberately short: the agent
#: prompt is built from it, so staleness is bounded and visible within a cycle.
#: The value is the `company_context_ttl_seconds` Settings field (default 30 s) — one number,
#: declared once in `app/config.py` (SV-W4-05: the old module constant was a second copy that
#: the getattr read could never actually be configured).

# engine -> (expires_at, day_iso, tz, context).  A weak key so a disposed engine
# (every unit test builds one) is collectable instead of pinned by this cache.
_CONTEXT_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, str, str, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


def _cache_ttl() -> float:
    return float(settings.company_context_ttl_seconds)


def invalidate_company_context() -> int:
    """Drop every cached context.  Returns the number of entries evicted.

    The explicit invalidation path: call it after any write to `company_config`
    (the API does so on `PUT /config`), from a seed script, or from a test that must
    observe fresh data immediately rather than after the TTL.
    """
    count = len(_CONTEXT_CACHE)
    _CONTEXT_CACHE.clear()
    return count


def cache_info() -> dict[str, Any]:
    """Introspection for probes/ops: what is live, and for how much longer.

    ZERO CALLERS (R25 dead-helper sweep, 2026-10-13; raw grep evidence:
    meridian-work/build/R25-deadhelpers/raw/grep_cache_info.txt) — exported in `__all__`
    but called by nothing: no app/, celery_app/ or scripts/ code, and no test reads it
    either.  Intended as the ops/probe view of `_CONTEXT_CACHE` (entries, TTL, remaining
    life).  Removal vs adoption is an owner decision (REQUESTS R25-1).
    """
    now = time.monotonic()
    entries = []
    for expires, day, tz, _ctx in list(_CONTEXT_CACHE.values()):
        entries.append(
            {
                "day": day,
                "tz": tz,
                "expires_in": round(expires - now, 3),
                "age_seconds": round(_cache_ttl() - (expires - now), 3),
            }
        )
    return {"entries": len(entries), "ttl_seconds": _cache_ttl(), "live": entries}


def kpis_provenance(kpis: Any) -> str:
    """Where the KPI values on a surface came from — the ONE rule (F-W3-1, wave 4).

    `configured` when the company row holds at least one KPI key (owner-typed Settings values);
    `unset` when it holds none.  There is no `measured` state HERE on purpose: this app has no
    measured-KPI source, and the measured money facts are the finance reads
    (`revenue_snapshots` → `/finance/summary`, `measured_finance` on the dashboard).  A caller that
    ever gains one must add the state to this function so every surface moves together.

    A non-mapping (a JSON list stored by an old client) is `unset`, the same reading the dashboard
    already applies when it coerces a mis-shaped `kpis` cell to `{}`.
    """
    return KPI_PROVENANCE_CONFIGURED if isinstance(kpis, dict) and kpis else KPI_PROVENANCE_UNSET


async def get_company(db: AsyncSession) -> CompanyConfig | None:
    """The single company row, or `None`; a failed read is a named `E_DB` (L2-06 — was a
    raw `db.execute`, so `GET/PUT /config` answered a codeless 500 on an outage)."""
    return await fetch_scalar(
        db, select(CompanyConfig).order_by(CompanyConfig.id).limit(1), what="company config"
    )


async def get_company_by_name(db: AsyncSession, name: str) -> CompanyConfig | None:
    """Lookup by exact name; a failed read is a named `E_DB` (L2-06, same class as above).

    ZERO CALLERS (R25 bounded scan, 2026-10-13; raw grep evidence:
    meridian-work/build/R25-deadhelpers/raw/scan_public_funcs.txt) — exported in `__all__`
    but called by nothing (production or tests); the app resolves the single company row
    via `get_company`.  Removal vs adoption is an owner decision (REQUESTS R25-1).
    """
    return await fetch_scalar(
        db,
        select(CompanyConfig).where(CompanyConfig.name == name).limit(1),
        what=f"company {name!r}",
    )


async def get_daily_cycle_hour(db: AsyncSession) -> int | None:
    """The company's stored Morning Cycle Hour, or None when no value is stored.

    `company_config.daily_cycle_hour` is `int | None` (DDL server_default 6, Python default 6 —
    but a stored NULL means "no value", NOT 6). This getter never invents a default: None is an
    answer, and the caller decides the fallback (see daily_cycle._resolve_morning_hour).
    """
    company = await get_company(db)
    if company is None:
        return None
    return company.daily_cycle_hour


def measured_human_hourly_rate(company: CompanyConfig | None) -> float | None:
    """The stored human hourly rate as a measurement, or None when there is no honest value.

    `company_config.human_hourly_rate` (migration 0011, V23/L3) is `float | None`: NULL is "no
    rate was ever set" and is answered as None — the Finance planning view then keeps
    `margin_template.human_rate: null` WITH its named reason and never fabricates a rate (the
    owner profile's hours are not a rate).  A stored value is the rate the owner entered on the
    Finance page; the write boundary (`CompanyConfigUpdate`) bounds it to a finite `>= 0` number,
    and this accessor re-checks it for rows written before that boundary existed (raw SQL, an
    old seed): a negative / non-finite / non-numeric stored value is treated as NO VALUE — the
    unmeasured state with a reason — rather than forwarded into money math.
    """
    if company is None:
        return None
    raw = company.human_hourly_rate
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _cached_context(db: AsyncSession) -> dict[str, Any] | None:
    """The live cached context for this engine, or None.  Zero queries."""
    entry = _CONTEXT_CACHE.get(db.get_bind())
    if entry is None:
        return None
    expires, day, tz, context = entry
    if time.monotonic() >= expires:
        return None
    if today_in(tz).isoformat() != day:
        return None  # the company's day rolled over since the entry was built
    return copy.deepcopy(context)


async def get_full_context(
    db: AsyncSession,
    *,
    tz_name: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """{} when no company row exists; otherwise company/kpis/goals/yesterday/todays_tasks/idea_brief.

    Never cached when empty (see module docstring).  The returned dict is always a
    copy, so a caller cannot mutate the cached context or another caller's view.
    """
    if use_cache and tz_name is None:
        cached = _cached_context(db)
        if cached is not None:
            return cached

    company = await get_company(db)
    if company is None:
        return {}
    tz = tz_name or (company.timezone or "UTC")

    evening_summary = await fetch_scalar(
        db,
        select(DailyReport.evening_summary)
        .order_by(DailyReport.report_date.desc())
        .limit(1),
        what="latest daily report summary",
    )

    task_rows = await fetch_rows(
        db,
        select(
            Task.id,
            Task.title,
            Task.description,
            Task.agent_type,
            Task.status,
            Task.priority,
        )
        .where(Task.status.in_(("pending", "in_progress")))
        .order_by(Task.priority.asc(), Task.id.asc())
        .limit(50),
        what="today's open tasks",
    )
    todays_tasks = [
        {
            "id": task_id,
            "title": title,
            "description": description,
            "agent_type": agent_type,
            "status": status,
            "priority": priority,
        }
        for task_id, title, description, agent_type, status, priority in task_rows
    ]

    # F-18 (wave 3): `confirmed_idea` carries NO timestamp of its own, so `idea_brief_state`
    # carries the measured provenance (confirmed / stale / unverified) beside it — a consumer that
    # reads `idea_brief` as a FACT about the current company has to ignore that state to do it.
    # The confirmation lookup is skipped entirely when no idea is stored.
    idea_state = idea_brief_state(
        company,
        await _idea_confirmed_at(db) if isinstance(company.confirmed_idea, dict) else None,
    )

    context: dict[str, Any] = {
        "company": {
            "id": company.id,
            "name": company.name,
            "mission": company.mission,
            "vision": company.vision,
            "description": company.description,
            "target_market": company.target_market,
            "value_prop": company.value_prop,
            "pricing_model": company.pricing_model,
            "goals": company.goals,
            "website_url": company.website_url,
            "github_repo": company.github_repo,
            "product_type": company.product_type,
            "industry": company.industry,
            "timezone": company.timezone,
            "daily_cycle_hour": company.daily_cycle_hour,
            # F-W3-1 (wave 4): the owner's recorded LAUNCH budget (NUMERIC(10,2) at rest, so it
            # arrives as `Decimal`).  It is configured data — the break-even denominator the money
            # agent may use — and it was previously not in any agent's context at all (run 313 got
            # the $125 from its task text instead).  None means "never set", never 0.
            "owner_budget_usd": company.owner_budget_usd,
        },
        "kpis": company.kpis or {},
        # F-W3-1: the KPI values above are CONFIGURED (owner-entered Settings data) — this key is
        # what lets every prompt say so, instead of printing an owner-typed 0 as a measurement.
        "kpis_provenance": kpis_provenance(company.kpis),
        "goals": company.goals or {},
        "yesterday_summary": evening_summary,
        "todays_tasks": todays_tasks,
        # V9/B1 rule (e), additive: the business idea the owner confirmed via
        # /api/v1/planner/confirm (`company_config.confirmed_idea`, 0008).  None = nothing
        # confirmed yet; the key is ALWAYS present so planner-gated consumers (the morning
        # plan's "tie every task to a milestone in idea_brief" rule) read one stable shape.
        # F-18 (wave 3): the bag carries NO timestamp, so `idea_brief_state` carries the
        # measured provenance (confirmed / stale / unverified) beside it — a consumer that reads
        # `idea_brief` as a FACT about the current company has to ignore that state to do it.
        "idea_brief": company.confirmed_idea,
        "idea_brief_state": idea_state,
        # V11/W2: the sourced macro snapshot (World Bank / FRED / frankfurter, cache-only via
        # `economics_service`) — explicit ok/partial/unavailable with per-section reasons;
        # never fabricated values.
        "macro": economics_service.macro_snapshot(),
        "as_of": today_in(tz).isoformat(),
    }

    if use_cache and tz_name is None:
        # The day key comes from the frozen window helper, so the cache rolls over
        # exactly where every other "today" in the app does — the company's local
        # midnight, dialect-correct.
        day_window(db, tz_name=tz)
        _CONTEXT_CACHE[db.get_bind()] = (
            time.monotonic() + _cache_ttl(),
            today_in(tz).isoformat(),
            tz,
            copy.deepcopy(context),
        )
    return context


def _as_utc(value: datetime | None) -> datetime | None:
    """A stored timestamp as aware UTC.  SQLite hands back naive datetimes, PostgreSQL aware ones.

    `None` stays `None`: "no timestamp" is a fact this module reports, never a 1970 stand-in.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def idea_brief_state(company: CompanyConfig, confirmed_at: datetime | None) -> dict[str, Any]:
    """The confirmed idea's provenance, from MEASURED timestamps only (F-18, wave 3).

    `status` is one of:

      * `none`       — no idea is stored; the prompt says nothing about one;
      * `confirmed`  — a Planner `idea_confirmed` record exists and the company profile has NOT
                       been edited since it was written, so the idea still describes THIS company;
      * `stale`      — the profile was edited AFTER the confirmation: the idea describes an
                       earlier business.  Rendered as PRIOR IDEA history, never as a fact;
      * `unverified` — an idea is stored but no confirmation record exists (written by an API
                       client or a seed, not by the Planner's confirm step): rendered as a
                       CANDIDATE, labelled unverified.

    The comparison is strict (`profile_updated_at > confirmed_at`): `confirm()` writes the idea and
    its activity row in ONE transaction, where both timestamps come from the same `now()`.
    """
    if not isinstance(company.confirmed_idea, dict):
        return {
            "status": IDEA_NONE,
            "confirmed_at": None,
            "profile_updated_at": None,
            "reason": "no confirmed idea is stored",
        }
    edited = _as_utc(company.updated_at)
    confirmed = _as_utc(confirmed_at)
    base = {
        "confirmed_at": confirmed.isoformat() if confirmed is not None else None,
        "profile_updated_at": edited.isoformat() if edited is not None else None,
    }
    if confirmed is None:
        return {
            "status": IDEA_UNVERIFIED,
            **base,
            "reason": (
                "an idea is stored but no Planner confirmation is on record, so its age and the "
                "profile it was confirmed against are unknown"
            ),
        }
    if edited is not None and edited > confirmed:
        return {
            "status": IDEA_STALE,
            **base,
            "reason": (
                "the company profile was edited after the idea was confirmed, so the idea may "
                "describe a different business than the one above"
            ),
        }
    return {
        "status": IDEA_CONFIRMED,
        **base,
        "reason": "confirmed in the Planner and the company profile has not been edited since",
    }


async def _idea_confirmed_at(db: AsyncSession) -> datetime | None:
    """When the Planner last confirmed an idea, or `None` when no confirmation is on record."""
    return await fetch_scalar(
        db,
        select(ActivityLog.created_at)
        .where(
            ActivityLog.agent_type == IDEA_AGENT_TYPE,
            ActivityLog.action == IDEA_CONFIRMED_ACTION,
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(1),
        what="the latest planner idea confirmation",
    )


def build_context_prompt(context: dict[str, Any] | None) -> str:
    """Render the context dict as agent prompt text.  Synchronous by contract."""
    if not context:
        return NO_CONTEXT

    company = context.get("company") or {}
    if not company:
        return NO_CONTEXT

    lines: list[str] = ["COMPANY CONTEXT", ""]
    lines.append(f"NAME: {company.get('name') or 'Unknown'}")
    for label, key in (
        ("INDUSTRY", "industry"),
        ("PRODUCT TYPE", "product_type"),
        ("MISSION", "mission"),
        ("VISION", "vision"),
        ("DESCRIPTION", "description"),
        ("TARGET MARKET", "target_market"),
        ("VALUE PROPOSITION", "value_prop"),
        ("WEBSITE", "website_url"),
        ("GITHUB REPO", "github_repo"),
    ):
        value = company.get(key)
        if value:
            lines.append(f"{label}: {value}")

    kpis = context.get("kpis") or {}
    if kpis:
        # F-W3-1: a configured KPI is the OWNER's number, not a measurement.  Printing it under a
        # bare "KPIS:" heading is how an owner-typed `mrr_usd: 0` becomes "MRR is $0.00" in a
        # prompt an agent then quotes as a fact.
        lines.append("")
        lines.append(
            "KPIS (CONFIGURED - owner-entered Settings values, NOT measurements):"
        )
        for key, value in kpis.items():
            lines.append(f"- {key}: {value}")
        lines.append(
            "  A configured KPI is not revenue or subscriber data read from Stripe; it must "
            "never be reported as a measured figure."
        )

    goals = context.get("goals") or company.get("goals") or {}
    if goals:
        lines.append("")
        lines.append("GOALS:")
        if isinstance(goals, dict):
            for key, value in goals.items():
                lines.append(f"- {key}: {value}")
        else:
            for value in goals:
                lines.append(f"- {value}")

    idea_brief = context.get("idea_brief")
    state = context.get("idea_brief_state") or {}
    if isinstance(idea_brief, dict):
        # F-18 (wave 3): the confirmed idea is NOT automatically a fact about this company.
        # `idea_brief_state` (measured: the Planner's confirmation record vs the profile's
        # `updated_at`) decides how it is presented.  Before this, every prompt opened with
        # "CONFIRMED IDEA" for an idea confirmed days and several profile edits earlier — a
        # candle/soap company was briefed with an "AI Marketing Ops Retainer" business and then
        # reasoned, and wrote, from the wrong company.  Whatever the state, the ONE thing a
        # prompt may never do is assert the idea is current when the evidence does not say so.
        if state.get("status") == IDEA_STALE:
            lines.append("")
            lines.append(
                "PRIOR IDEA — NOT THE CURRENT BUSINESS (this is history, not a fact about the "
                "company above):"
            )
            if idea_brief.get("name"):
                lines.append(f"RECORDED NAME: {idea_brief.get('name')}")
            lines.append(
                f"WHY IT IS HISTORY: the idea was confirmed in the Planner at "
                f"{state.get('confirmed_at')}, and the company profile was edited afterwards at "
                f"{state.get('profile_updated_at')} — it may describe a different business."
            )
            lines.append(
                "INSTRUCTION: do not brief work from this idea, do not repeat its claims, and do "
                "not treat it as a fact about this company. It is kept only so the owner can "
                "re-confirm or replace it in the Planner."
            )
        else:
            if state.get("status") == IDEA_CONFIRMED:
                lines.append("")
                lines.append(
                    "CONFIRMED IDEA (idea_brief — confirmed in the Planner at "
                    f"{state.get('confirmed_at')}; the company profile has not been edited since):"
                )
            else:
                # No confirmation on record (written by an API client / a seed), or a caller that
                # passed a bare context dict: an unverified CANDIDATE, never a confirmed fact.
                lines.append("")
                lines.append(
                    "RECORDED IDEA (idea_brief — UNVERIFIED: no Planner confirmation is on record "
                    "for it; treat it as a candidate, never as a fact about the company above):"
                )
            for label, key in (
                ("IDEA", "name"),
                ("ONE-LINER", "one_liner"),
                ("BUYER", "buyer"),
                ("CHANNEL", "channel"),
            ):
                value = idea_brief.get(key)
                if value:
                    lines.append(f"{label}: {value}")
            band = idea_brief.get("price_band")
            if isinstance(band, list) and len(band) == 2:
                lines.append(f"PRICE BAND: {band[0]}-{band[1]}")

    yesterday = context.get("yesterday_summary")
    if yesterday:
        lines.append("")
        lines.append(f"YESTERDAY: {yesterday}")

    tasks = context.get("todays_tasks") or []
    lines.append("")
    lines.append("TODAY'S TASKS:")
    if tasks:
        for task in tasks:
            lines.append(
                f"- {task.get('title')} (@{task.get('agent_type')}, {task.get('status')})"
            )
    else:
        lines.append("- none yet")

    return "\n".join(lines)


__all__ = [
    "IDEA_AGENT_TYPE",
    "IDEA_CONFIRMED",
    "IDEA_CONFIRMED_ACTION",
    "IDEA_NONE",
    "IDEA_STALE",
    "IDEA_UNVERIFIED",
    "KPI_PROVENANCE_CONFIGURED",
    "KPI_PROVENANCE_UNSET",
    "NO_CONTEXT",
    "build_context_prompt",
    "cache_info",
    "get_company",
    "get_company_by_name",
    "get_daily_cycle_hour",
    "get_full_context",
    "idea_brief_state",
    "invalidate_company_context",
    "kpis_provenance",
    "measured_human_hourly_rate",
]
