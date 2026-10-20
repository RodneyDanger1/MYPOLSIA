"""GET /api/v1/ops/events/stats + POST /api/v1/ops/events/reset — the breaker's operator surface (R22).

...plus GET /api/v1/ops/agent-quality (V9 B2, S1 #8/#9): the deterministic per-agent quality read
over the `agent_runs` ledger — runs_7d, success_rate, last_failure_at, cost_per_completed_task,
null-honest.

...plus GET /api/v1/ops/sandbox (V11 unit 2, capability audit B1/B2): the read-only
`SANDBOX_MODE` flag. The shipped default is sandboxed and until this route nothing outside the
process could say so — the UI showed an apparently-broken product with no explanation.

...plus GET /api/v1/ops/capabilities (V16/E6, frozen interface H-C1): every agent's REAL
capability surface — registry tools (name + the registry's own description), permitted action
classes and external channels — DERIVED at request time from `policy_firewall.AGENT_CAPABILITIES`
+ `tool_registry` through `app.services.capability_manifest` (never hand-copied). The same
derivation renders the agents' own context blocks and the orchestrator's fleet map, so this
surface cannot disagree with what the runtime enforces or with what agents are told.

...plus POST/GET /api/v1/ops/run-workspace-cleanup (V27/L2): the owner's manual surface for the
V26 run-workspace retention policy (`tool_registry.cleanup_expired_run_workspaces` — keep the
newest 20 run directories under `<workspace>/runs/`, max age 72 h).  The POST is a DRY RUN unless
the body says `{"apply": true}`; the GET serves the last recorded result (or an honest null).
The automatic caller is the daily beat task `celery_app.tasks.maintenance.cleanup_run_workspaces`.

WHY this file exists: `events.publish_stats()` and `reset_publish_breaker()` shipped with the
activity circuit breaker (the "a skip is never silent" contract, `app/core/events.py:44-61`) and
had ZERO production callers — the counters and the recovery lever were reachable only from tests.
These routes are the owner's read/reset surface during/after a Redis incident; the 15-minute beat
tick (`celery_app.tasks.maintenance.check_activity_publish_health`) writes the same stats into the
worker's log.

PROCESS BOUNDARY (stated, not hidden): the breaker's counters are module globals in
`app.core.events`, i.e. PER PROCESS.  These routes report and reset the API process's state; the
beat tick reports the worker process's state.  Neither surface claims to see the other's numbers.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`support.py:28`, `memory.py:17`).  The reset verb needs no `refusal_guard`: it flips in-memory
counters and nothing else — no DB row, no external call, nothing that can refuse.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.config import settings
from app.core import events, redis_client
from app.core.errors import E_DB, E_REDIS, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import day_window, iso_utc, today_in
from app.models.report import ActivityLog
from app.models.task import AgentRun
from app.schemas.common import UtcDatetime
from app.schemas.ops import (
    AgentQualityResponse,
    CapabilityManifestResponse,
    EventPublishStatsResponse,
    SandboxStateResponse,
)
from app.services import capability_manifest
from app.services.query_helpers import fetch_rows

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(verify_api_key)])

logger = logging.getLogger(__name__)


@router.get("/events/stats", response_model=EventPublishStatsResponse)
async def read_event_publish_stats() -> EventPublishStatsResponse:
    """failures / skipped_while_open / breaker_open / cooldown_seconds / last_error."""
    return EventPublishStatsResponse.model_validate(events.publish_stats())


@router.post("/events/reset", response_model=EventPublishStatsResponse)
async def reset_event_publish_breaker() -> EventPublishStatsResponse:
    """Clear the breaker and its counters after an incident; returns the fresh (clean) stats."""
    events.reset_publish_breaker()
    return EventPublishStatsResponse.model_validate(events.publish_stats())


# ── GET /ops/activity-publish (V23/L4, COMPLETENESS #8) ──────────────────────────────────────
#
# The breaker's counters are module globals in `app.core.events`, i.e. PER PROCESS.  The routes
# above serve the API process's numbers; the CELERY WORKER's numbers are published by the
# 15-minute beat tick (`check_activity_publish_health`) under a shared Redis key whose TTL is one
# beat period.  This route is the honest read of that snapshot:
#
#   200 {"data": {...}}   the worker published: failures / skipped_while_open / breaker_open / ts
#   200 {"data": null}    the worker never published (or the key expired): NO fabricated zeros
#   503 E_REDIS           Redis is unreachable, or the stored value is not the frozen shape
#
# The models live here rather than in `app/schemas/ops.py` for the same reason the invoice request
# models live in their router: the schemas package is frozen for this wave.


class ActivityPublishStatsBody(BaseModel):
    """The worker's own snapshot — exactly the four keys `check_activity_publish_health` writes."""

    failures: int
    skipped_while_open: int
    breaker_open: bool
    #: The instant the WORKER wrote the snapshot (aware ISO-8601), never this request's time.
    ts: str


class ActivityPublishResponse(BaseModel):
    """`{data: ...}` — null is a measured absence (the worker never published), never zeros."""

    data: ActivityPublishStatsBody | None


@router.get("/activity-publish", response_model=ActivityPublishResponse)
async def read_activity_publish_stats() -> ActivityPublishResponse:
    """The worker process's last published breaker counters — or an honest null."""
    with refusal_guard():
        try:
            raw = await redis_client.get_redis().get(redis_client.ACTIVITY_PUBLISH_STATS_KEY)
        except Exception as exc:  # noqa: BLE001 - a dependency failure is NAMED, not a bare 500
            raise PolsiaError(
                E_REDIS,
                f"activity publish stats could not be read from Redis: {type(exc).__name__}: {exc}",
                how_to_fix=ERROR_HINTS[E_REDIS],
                context={"key": redis_client.ACTIVITY_PUBLISH_STATS_KEY},
            ) from exc

        if raw is None:
            return ActivityPublishResponse(data=None)

        try:
            snapshot = ActivityPublishStatsBody.model_validate(json.loads(raw), strict=True)
        except (TypeError, ValueError, ValidationError) as exc:
            # A value that is not the shape this worker writes cannot be rendered as counters —
            # serving it would fabricate a reading.  Named, in the same dependency class.
            raise PolsiaError(
                E_REDIS,
                "activity publish stats in Redis are unreadable (not the frozen shape)",
                how_to_fix=ERROR_HINTS[E_REDIS],
                context={"key": redis_client.ACTIVITY_PUBLISH_STATS_KEY},
            ) from exc

        return ActivityPublishResponse(data=snapshot)


# ── GET /ops/sandbox (V11 unit 2) ─────────────────────────────────────────────────────────────


@router.get("/sandbox", response_model=SandboxStateResponse)
async def read_sandbox_state() -> SandboxStateResponse:
    """Whether external actions are sandboxed (`settings.SANDBOX_MODE`).

    Read-only BY DESIGN: flipping the flag stays a deliberate `.env` + restart action — but the
    product must be able to SAY sandbox is on instead of silently drafting everything.
    """
    return SandboxStateResponse(sandbox_mode=bool(settings.sandbox_mode))


# ── GET /ops/agent-quality (V9 B2, S1 #8/#9) ──────────────────────────────────────────────────

#: The lookback the quality view reports (days, company-local, today inclusive).
QUALITY_WINDOW_DAYS = 7


def _blank_stats() -> dict[str, Any]:
    return {"runs_7d": 0, "completed": 0, "failed": 0, "cost_sum": None, "cost_measured": 0,
            "last_failure_at": None}


async def _agent_quality_rows(db: AsyncSession) -> list[dict[str, Any]]:
    """One row per agent: the 7-day ledger window + the all-time last failure, null-honest.

    DETERMINISTIC, no LLM.  Every registry agent answers (a missing agent reports a measured
    `runs_7d` of 0 — the ledger genuinely holds no runs for it); `success_rate` and
    `cost_per_completed_task` stay null when their denominator or measurement is absent — never 0.
    """
    from app.agents.crew_factory import AGENT_MAP

    start, _ = day_window(db, today_in() - timedelta(days=QUALITY_WINDOW_DAYS - 1))
    window_rows = await fetch_rows(
        db,
        select(AgentRun.agent_type, AgentRun.status, AgentRun.cost_usd).where(
            AgentRun.started_at >= start
        ),
        what="agent quality runs",
    )
    stats: dict[str, dict[str, Any]] = {agent_type: _blank_stats() for agent_type in sorted(AGENT_MAP)}
    for agent_type, status, cost_usd in window_rows:
        entry = stats.setdefault(str(agent_type), _blank_stats())
        entry["runs_7d"] += 1
        if status == "completed":
            entry["completed"] += 1
            if cost_usd is not None:
                entry["cost_sum"] = (entry["cost_sum"] or 0) + cost_usd
                entry["cost_measured"] += 1
        elif status == "failed":
            entry["failed"] += 1

    # The newest failure per agent, ALL TIME (the 7-day window is a rate; "when did this agent
    # last break" is a timestamp).  `coalesce(ended_at, started_at)` reads the best instant a run
    # carries; a failed row with neither stays out instead of inventing a date.
    failure_rows = await fetch_rows(
        db,
        select(
            AgentRun.agent_type,
            func.max(func.coalesce(AgentRun.ended_at, AgentRun.started_at)).label("last_failure_at"),
        )
        .where(AgentRun.status == "failed")
        .group_by(AgentRun.agent_type),
        what="agent last failures",
    )
    for agent_type, last_failure_at in failure_rows:
        stats.setdefault(str(agent_type), _blank_stats())["last_failure_at"] = last_failure_at

    rows: list[dict[str, Any]] = []
    for agent_type in sorted(stats):
        entry = stats[agent_type]
        terminal = int(entry["completed"]) + int(entry["failed"])
        rows.append(
            {
                "agent_type": agent_type,
                "runs_7d": int(entry["runs_7d"]),
                "success_rate": (int(entry["completed"]) / terminal if terminal else None),
                "last_failure_at": iso_utc(entry["last_failure_at"]),
                "cost_per_completed_task": (
                    float(entry["cost_sum"]) / int(entry["completed"])
                    if entry["cost_measured"] and int(entry["completed"])
                    else None
                ),
            }
        )
    return rows


@router.get("/agent-quality", response_model=AgentQualityResponse)
async def agent_quality(db: AsyncSession = Depends(get_db)) -> AgentQualityResponse:
    """`{agents: [{agent_type, runs_7d, success_rate, last_failure_at, cost_per_completed_task}]}`."""
    with refusal_guard():
        rows = await _agent_quality_rows(db)
    return AgentQualityResponse(agents=rows)


# ── GET /ops/capabilities (V16/E6, frozen interface H-C1) ─────────────────────────────────────


@router.get("/capabilities", response_model=CapabilityManifestResponse)
async def read_capabilities() -> CapabilityManifestResponse:
    """Every agent's REAL capabilities + the registry tool catalog, derived at request time.

    H-C1 (V16): `{agents: [{agent_type, tools: [{name, description}], action_classes, channels}],
    registry_tools: [{name, description}]}` — derived from `policy_firewall.AGENT_CAPABILITIES`
    and `tool_registry` through `app.services.capability_manifest` (never hand-copied). A registry
    inconsistency (an allowlist naming a tool no spec ships) raises the service's named `E_CONFIG`
    refusal through `refusal_guard` instead of fabricating a capability.

    V20/L1 (card L2-06): the payload is declared (`CapabilityManifestResponse`) instead of
    `dict[str, Any]`, so the frozen H-C1 shape is in OpenAPI. The service dict is validated —
    never rebuilt — so the served bytes are unchanged (pinned byte-for-byte in
    `tests/unit/test_v20_api_typing.py`).
    """
    with refusal_guard():
        return CapabilityManifestResponse.model_validate(
            capability_manifest.capability_manifest()
        )


# ── POST/GET /ops/run-workspace-cleanup (V27/L2) ─────────────────────────────────────────────
#
# V26 shipped `tool_registry.cleanup_expired_run_workspaces` — keep the newest 20 run directories
# under `<workspace>/runs/`, max age 72 h — with NO caller.  The daily beat task is the automatic
# caller (`celery_app.tasks.maintenance.cleanup_run_workspaces`); these two routes are the owner's
# manual surface:
#
#   POST  body optional; `apply` defaults to FALSE (dry run).  `{"apply": true}` deletes.
#         Returns {runs_dir, count, kept, removed, skipped, policy, applied} — plus
#         `removed_truncated` / `skipped_truncated` ONLY when the 50-name cap actually cut a list
#         (V29/L1 T1.4; `response_model_exclude_none` keeps the seven-key shape otherwise, and the
#         GET below serves `null` for them rather than excluding its own `{"data": null}`).
#   GET   the last recorded result (the newest activity row this server wrote) or {"data": null}.
#
# The body is parsed BY HAND, not through a FastAPI body model: the app's RequestValidationError
# handler serves the framework's uncoded `{"detail": [...]}` shape for ordinary 422s, and this
# verb's contract says a bad body is the NAMED `E_VALIDATION` (code + fix hint), like every other
# refusal in the product.  Nothing is deleted before the body is fully validated.
#
# `workspace_root` is the DRILL SEAM: the live acceptance run seeds a temp root and points the
# route at it.  Omitted, the task and the route both read `settings.code_workspace_dir` at call
# time.  The registry function still only ever touches `<root>/runs/`, still refuses a linked
# `runs/`, and still never follows a link out of the workspace.
#
# The policy is NOT declared here.  `run_workspace_cleanup` (the maintenance module this lane also
# owns) calls the V26 function for the applied path and imports its constants for the preview, so
# the two paths cannot drift (pinned in `tests/unit/test_v27_retention.py`).  The import is lazy
# so the API process loads the worker task modules only when this surface is first used.


class RunWorkspaceCleanupPolicy(BaseModel):
    """The V26 registry policy, as served: newest `keep_latest`, max age `max_age_hours`."""

    keep_latest: int
    max_age_hours: float


class RunWorkspaceCleanupSkip(BaseModel):
    """One run-directory-shaped entry deliberately NOT deleted, and why."""

    name: str
    reason: str


class RunWorkspaceCleanupResult(BaseModel):
    """The seven-key contract, plus two OPTIONAL truncation flags.

    `removed` is "would remove" when `applied` is false.  The counts are always exact; the NAME
    lists are capped at `RUN_WORKSPACE_CLEANUP_NAME_CAP` (50) so a mass expiry stays a bounded
    activity row.  `removed_truncated`/`skipped_truncated` say that a list really WAS capped:
    `true` only when the cap bit, and no claim otherwise — the POST body omits them
    (`response_model_exclude_none`, which keeps its frozen seven-key shape) while the GET serves
    `null` (`{"data": null}` must stay a measured absence, so that route cannot exclude `None`).
    Neither `false`, neither `null` nor absent ever means "zero" (V29/L1 T1.4: the response model
    used to drop the flags, so a capped list read exactly like a complete one).
    """

    runs_dir: str
    count: int
    kept: int
    removed: list[str]
    skipped: list[RunWorkspaceCleanupSkip]
    policy: RunWorkspaceCleanupPolicy
    applied: bool
    removed_truncated: bool | None = None
    skipped_truncated: bool | None = None


class RunWorkspaceCleanupRecord(BaseModel):
    """The last recorded run: its activity row, when it was written, and who triggered it."""

    activity_id: int
    created_at: UtcDatetime | None
    trigger: str
    result: RunWorkspaceCleanupResult


class RunWorkspaceCleanupLastResponse(BaseModel):
    """`{data: ...}`; null is a measured absence (nothing recorded yet), never zeroed counts."""

    data: RunWorkspaceCleanupRecord | None


#: The only keys the body may carry.  Anything else is refused by name (no silent ignore).
_CLEANUP_BODY_KEYS = frozenset({"apply", "workspace_root"})


async def _cleanup_body(request: Request) -> tuple[bool, str | None]:
    """`(apply, workspace_root)` — or a NAMED `E_VALIDATION`, before anything is touched.

    An ABSENT body is the dry run (`False, None`): the safe default must not require a payload.
    A present body must be a JSON object with at most the two documented keys; `apply` must be a
    real JSON boolean (no string/int coercion — `{"apply": "yes"}` is a mistake, not consent) and
    `workspace_root` a non-blank string when present.
    """
    raw = await request.body()
    if not raw or not raw.strip():
        return False, None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PolsiaError(
            E_VALIDATION,
            "the request body is not valid JSON",
            how_to_fix='send {"apply": false|true} (or no body at all for a dry run)',
            context={"reason": str(exc)[:200]},
        ) from exc
    if not isinstance(payload, dict):
        raise PolsiaError(
            E_VALIDATION,
            f"the request body must be a JSON object (got {type(payload).__name__})",
            how_to_fix='send {"apply": false|true}',
            context={"type": type(payload).__name__},
        )
    unknown = sorted(set(payload) - _CLEANUP_BODY_KEYS)
    if unknown:
        raise PolsiaError(
            E_VALIDATION,
            f"the request body carries unknown key(s): {', '.join(unknown)}",
            how_to_fix='send only {"apply": boolean} and, for a drill, {"workspace_root": string}',
            context={"unknown": unknown},
        )
    apply = payload.get("apply", False)
    if not isinstance(apply, bool):
        raise PolsiaError(
            E_VALIDATION,
            f"`apply` must be a JSON boolean (got {type(apply).__name__})",
            how_to_fix="pass true to delete the expired run directories, false (or omit) to preview",
            context={"apply": repr(apply)[:40]},
        )
    root = payload.get("workspace_root")
    if root is not None:
        if not isinstance(root, str) or not root.strip():
            raise PolsiaError(
                E_VALIDATION,
                "`workspace_root` must be a non-blank string when present",
                how_to_fix="pass a directory path, or omit workspace_root for the configured one",
                context={"workspace_root": repr(root)[:200]},
            )
        root = root.strip()
        # A control character can never belong to a path.  `Path.resolve()` refuses it with a bare
        # ValueError, which the retention registry dresses up as E_CONFIG (503 — "this service
        # cannot take work right now"), blaming the server for the caller's value.  Refuse it as
        # the caller error it is, before any filesystem work (V29 follow-up B; the same class as
        # the /trust/link control-character refusal).
        for position, char in enumerate(root):
            if ord(char) < 0x20 or ord(char) == 0x7F:
                raise PolsiaError(
                    E_VALIDATION,
                    "`workspace_root` contains a control character that cannot appear in a path",
                    how_to_fix=(
                        "pass a normal directory path (re-paste it without control characters)"
                    ),
                    context={
                        "field": "workspace_root",
                        "position": position,
                        "codepoint": f"U+{ord(char):04X}",
                    },
                )
    return apply, root


@router.post("/run-workspace-cleanup", response_model=RunWorkspaceCleanupResult,
             response_model_exclude_none=True)
async def run_workspace_cleanup_route(
    request: Request, db: AsyncSession = Depends(get_db)
) -> RunWorkspaceCleanupResult:
    """Dry-run by default; `{"apply": true}` deletes the expired run directories.

    The filesystem work runs off the event loop (`run_in_threadpool`) — deleting a tree is
    blocking.  Both outcomes write ONE activity row (counts + run names + policy, never file
    contents), which is what `GET` serves and what the `/ops` panel renders.  A dry run is also
    recorded: "the owner looked" is an operator fact, and it deletes nothing.
    """
    from celery_app.tasks.maintenance import record_run_workspace_cleanup, run_workspace_cleanup

    with refusal_guard():
        apply, workspace_root = await _cleanup_body(request)
        outcome = await run_in_threadpool(
            run_workspace_cleanup, apply=apply, workspace_root=workspace_root
        )
        entry = await record_run_workspace_cleanup(db, outcome, trigger="owner")
        activity_id = int(entry.id)
        await db.commit()

    logger.info(
        "owner run-workspace cleanup: applied=%s count=%s kept=%s activity=%s",
        outcome["applied"], outcome["count"], outcome["kept"], activity_id,
    )
    return RunWorkspaceCleanupResult.model_validate(outcome)


@router.get("/run-workspace-cleanup", response_model=RunWorkspaceCleanupLastResponse)
async def read_last_run_workspace_cleanup(
    db: AsyncSession = Depends(get_db),
) -> RunWorkspaceCleanupLastResponse:
    """The newest recorded cleanup — or `{data: null}` when none has run yet.

    An unreadable stored detail is a NAMED `E_DB` refusal, not a null: null means "never ran",
    and serving it for a corrupt row would erase the difference between absence and damage.
    """
    from celery_app.tasks.maintenance import RUN_WORKSPACE_CLEANUP_ACTION

    with refusal_guard():
        row = (
            await db.execute(
                select(ActivityLog)
                .where(ActivityLog.action == RUN_WORKSPACE_CLEANUP_ACTION)
                .order_by(ActivityLog.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return RunWorkspaceCleanupLastResponse(data=None)

        detail = row.detail if isinstance(row.detail, dict) else None
        if detail is None:
            raise PolsiaError(
                E_DB,
                f"activity row {row.id} carries no readable run-workspace cleanup detail",
                how_to_fix="inspect the activity_log row; the panel cannot render a result it cannot read",
                context={"activity_id": int(row.id)},
            )
        try:
            result = RunWorkspaceCleanupResult.model_validate(detail)
        except ValidationError as exc:
            raise PolsiaError(
                E_DB,
                f"activity row {row.id} is not the run-workspace cleanup shape this server writes",
                how_to_fix="inspect the activity_log row's detail JSON",
                context={"activity_id": int(row.id), "errors": exc.errors()[:5]},
            ) from exc

        trigger = detail.get("trigger")
        return RunWorkspaceCleanupLastResponse(
            data=RunWorkspaceCleanupRecord(
                activity_id=int(row.id),
                created_at=row.created_at,
                trigger=trigger if isinstance(trigger, str) else "unknown",
                result=result,
            )
        )
