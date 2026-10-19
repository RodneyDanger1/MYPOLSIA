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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.timeutil import day_window, today_in
from app.models.company import CompanyConfig
from app.models.report import DailyReport
from app.models.task import Task
from app.services import economics_service
from app.services.query_helpers import fetch_rows, fetch_scalar

NO_CONTEXT = "No company context available."

#: How long a computed context may be reused.  Deliberately short: the agent
#: prompt is built from it, so staleness is bounded and visible within a cycle.
CONTEXT_CACHE_TTL_SECONDS: float = 30.0

# engine -> (expires_at, day_iso, tz, context).  A weak key so a disposed engine
# (every unit test builds one) is collectable instead of pinned by this cache.
_CONTEXT_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, str, str, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


def _cache_ttl() -> float:
    return float(getattr(settings, "company_context_ttl_seconds", CONTEXT_CACHE_TTL_SECONDS))


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
    polsia-work/build/R25-deadhelpers/raw/grep_cache_info.txt) — exported in `__all__`
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


async def get_company(db: AsyncSession) -> CompanyConfig | None:
    """The single company row, or `None`; a failed read is a named `E_DB` (L2-06 — was a
    raw `db.execute`, so `GET/PUT /config` answered a codeless 500 on an outage)."""
    return await fetch_scalar(
        db, select(CompanyConfig).order_by(CompanyConfig.id).limit(1), what="company config"
    )


async def get_company_by_name(db: AsyncSession, name: str) -> CompanyConfig | None:
    """Lookup by exact name; a failed read is a named `E_DB` (L2-06, same class as above).

    ZERO CALLERS (R25 bounded scan, 2026-10-13; raw grep evidence:
    polsia-work/build/R25-deadhelpers/raw/scan_public_funcs.txt) — exported in `__all__`
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
        },
        "kpis": company.kpis or {},
        "goals": company.goals or {},
        "yesterday_summary": evening_summary,
        "todays_tasks": todays_tasks,
        # V9/B1 rule (e), additive: the business idea the owner confirmed via
        # /api/v1/planner/confirm (`company_config.confirmed_idea`, 0008).  None = nothing
        # confirmed yet; the key is ALWAYS present so planner-gated consumers (the morning
        # plan's "tie every task to a milestone in idea_brief" rule) read one stable shape.
        "idea_brief": company.confirmed_idea,
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
        lines.append("")
        lines.append("KPIS:")
        for key, value in kpis.items():
            lines.append(f"- {key}: {value}")

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
    if isinstance(idea_brief, dict):
        # V9/B1: the confirmed idea renders into every prompt this builder serves (the
        # agent-layer path carries the key through the context dict; base_agent.py is
        # frozen-read for seat b1, so this renderer is the honest in-territory path).
        lines.append("")
        lines.append("CONFIRMED IDEA (idea_brief):")
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
    "NO_CONTEXT",
    "build_context_prompt",
    "cache_info",
    "get_company",
    "get_company_by_name",
    "get_daily_cycle_hour",
    "get_full_context",
    "invalidate_company_context",
    "measured_human_hourly_rate",
]
