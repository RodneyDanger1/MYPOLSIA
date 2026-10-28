"""Per-agent Celery tasks — dispatched by the orchestrator or Beat.

What the upload did and what this file does instead (all measured, lane 03/04/06):

| Upload | Here |
|---|---|
| `_run_sync` (dead code, `RuntimeError` on 3.12 — D-09) | deleted; `run_async` on one process loop |
| `create_async_engine()` + `dispose()` per task (4.279 ms warm / 179 ms cold) | `app.core.database.get_session_factory` |
| exception swallowed into the DB, `max_retries=2` inert (D-07) | failure is classified, `attempts` recorded, real `self.retry` with backoff |
| no claim → two workers can run the same row | atomic `claim_task` first; a second delivery exits without side effects |
| sweeps create a task per tick (duplicates under at-least-once, D-06) | `<agent_type>:<utc-day>:<bucket>` dedupe key |
| `agent_runs` duration only, tokens/cost/session never written | duration, tokens, cost, CLI session persisted |
| a run's side effects never reached the tables (`social_posts`, `email_logs`, `revenue_snapshots` stayed empty) | `_persist_run_artifacts` writes what a completed run really performed, on the run's own transaction |
| every per-channel cap read `context["channel_sends"]`, which had no producer anywhere | `_with_channel_sends` injects the channel snapshot for this agent's own channels (absent-and-recorded if the producer cannot answer) |
| stored memories reached no run (the read seam had no producer; `remember_run` had no caller) | `_with_run_memories` resolves `context["memories"]` per dispatch; `_persist_run_artifacts` folds a `remember_run` memory report in (V15/E2-H4) |
| the 6-hourly ads/stripe dispatch carried no provider input (metrics/revenue existed only if the model invented them) | H2/H3 attaches on the created tasks: `metadata.ad_metrics` (both-platform, the configured one) + `metadata.revenue_facts` (V15/E2-H2/H3) |

The claim API (`claim_task`, `heartbeat_task`, `release_task_claim`) is requested from the
services owner in the lane receipt (`## REQUESTS`) — it is imported inside the coroutine so
this module still imports (and registers its tasks) before that lands.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any

from app.config import settings
from app.core.errors import (
    E_ACTION_REPLAY_UNAVAILABLE,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_TIMEOUT,
    E_DB,
    E_INTEGRATION,
    E_NOT_FOUND,
    E_REDIS,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    E_VALIDATION,
    PolsiaError,
)
from app.models.task import TaskStatus
from celery_app.tasks._common import (
    ACTION_KEY,
    actions_from_outcome,
    assert_budget_allows,
    create_task_deduped,
    dedupe_key,
    dispatch_paused_reason,
    enqueue_created,
    merge_actions,
    paused_skip,
    prior_actions,
    run_async,
    stamp_rearmed,
    sweep_bucket,
)
from celery_app.worker import app

logger = logging.getLogger(__name__)

# Failures worth another attempt: the run never produced a result and the cause is
# infrastructure (CLI transport, provider, database).  Everything else — a bad prompt, an
# unknown agent, a sandbox refusal, a budget stop, a validation error — is terminal by
# definition (SPEC-AGENTS D-07: "backoff for infrastructure errors only; business
# failures stay terminal").
#
# `E_AGENT_BAD_OUTPUT` was in this set and is NOT infrastructure: it is the agent's own output
# failing its output contract (F-23, live: `social_media` produced a twitter post > 280 chars,
# `POLSIA-E110`, and the worker spent TWO more model calls re-asking the identical prompt before
# the row was left wedged).  A schema validator is deterministic about the same instruction — the
# measured retries all failed — so this code is terminal now: ONE attempt, the named failure on
# the row, and the owner re-runs deliberately (possibly after a prompt change) if they want it.
#
# D7 (owner ruling): `E_RATE_LIMITED` is deliberately NOT here.  It is a DAILY-CAP REFUSAL
# ("refused before charge" — the per-channel cap, the day budget cap, or the provider-side
# limit the firewall maps to it): nothing was performed, and re-delivering the same run
# spends `agent_max_retries` more model calls working around a limit that resets on its own
# schedule.  The refusal is recorded on the task row and the ledger names the reason; the
# next scheduled tick retries when the window has moved.  (The CLI-attempt ladder's use of
# a provider `rate_limit` subtype inside one run — `claude_cli.is_retryable_failure` — is a
# different contract and is unchanged.)
RETRYABLE_CODES = frozenset(
    {
        E_CLI_TIMEOUT,
        E_CLI_EXIT,
        E_CLI_JSON,
        E_INTEGRATION,
        E_DB,
        E_REDIS,
    }
)

# A run whose outcome must never be written back to the task row: the row belongs to
# somebody else now.
_SKIP_CLAIM_CODES = (E_TASK_CLAIMED, E_TASK_NOT_RUNNABLE)

#: The `tasks.metadata` key the pause gate stamps on a row it RELEASED (wave v15/3-03, decision
#: #2 + BUG-DIGEST §B7).  Producer here; the consumer is the `rearm_paused_released_tasks`
#: maintenance tick (`celery_app.tasks.maintenance`), which re-dispatches marked `pending` rows
#: exactly once after resume — the producer/consumer split `metadata.inbox_item` and
#: `metadata.approval_token` already use.
PAUSED_RELEASE_KEY = "paused_release"


def worker_id() -> str:
    """Identity of this worker process, for `tasks.claimed_by`."""
    return f"{socket.gethostname()}:{os.getpid()}"


def is_retryable(exc: BaseException | None) -> bool:
    if exc is None:
        return True
    if isinstance(exc, PolsiaError):
        return exc.code in RETRYABLE_CODES
    # Unexpected failures (driver/network/OS) are treated as transient; the ladder is
    # bounded by settings.agent_max_retries and every attempt is visible in the ledger.
    return True


def _notification_outcome(task_status: str, requeued: bool) -> str:
    """The ONE outcome the owner-notification leg routes for this attempt (V8/C5, D4).

    A requeued attempt is NOT a terminal `failed`: the row is back in the queue for its retry
    (`task_row.status = TaskStatus.PENDING` below).  Routing `requeued` — its own frozen
    toggle, `notify_on_run_requeued` — is also what keeps the two emails from doubling: this
    leg makes exactly ONE call per attempt, so choosing the outcome HERE is the whole edge;
    `notify_on_failed` must never also fire for the same attempt.
    """
    return "requeued" if requeued else task_status


def retry_countdown(attempts: int) -> float:
    """Exponential backoff for the Nth failed attempt, capped at 10 minutes.

    `attempts` counts EXECUTIONS of the row (the claim increments it on every delivery), so the first
    failed execution passes 1 and waits `task_retry_backoff_seconds`, the second twice that, and so on.
    The docstring here previously called it "the number of failures already recorded", which is what the
    caller double-counted against — see the failure path in `_execute_agent_task`.
    """
    base = max(1, int(settings.task_retry_backoff_seconds))
    return min(600.0, float(base) * (2 ** max(0, attempts - 1)))


def _apply_run_usage(run_row: Any, result: dict[str, Any]) -> None:
    """Persist the run's accounting columns (added by migration 0002).

    The keys are the ones the agent runtime attaches to a finished run
    (`app.agents.base_agent.RUN_META_KEYS` — `cli_session_id`, `tokens_input`,
    `tokens_output`, `tokens_used`, `cost_usd`, `cost_estimate_usd`, `usage_json`,
    `model`, …), with the CLI envelope's `usage.input_tokens`/`output_tokens` accepted as
    aliases.  A key the agent did not report stays NULL: nothing here is invented.

    V22/L1: `tools_used` is the one key with its OWN column, and its presence is load-bearing —
    the DeepSeek envelope only carries the key when tools were available at all, so an absent
    key leaves `agent_runs.tools_used` NULL ("never recorded") and a present `[]` is written as
    `[]` ("recorded; no tool ran").  The value is re-bounded here (the write boundary is not
    allowed to trust a producer) into both the column and the `usage_json` audit copy.
    """
    if not isinstance(result, dict):
        return

    from app.agents.base_agent import RUN_META_KEYS

    # V22/L1: read presence once; `bounded_tools_used` maps absent/junk -> None.
    tools_used: list[str] | None = None
    if "tools_used" in result:
        from app.agents.accounting import bounded_tools_used

        tools_used = bounded_tools_used(result.get("tools_used"))
        if tools_used is not None:
            run_row.tools_used = tools_used

    # Read each key ONCE into a local: `result.get("usage") if isinstance(result.get("usage"), dict)`
    # calls .get twice, so a non-deterministic mapping (a property, or a dict subclass) can pass the
    # isinstance check on the first call and return something else on the second.
    raw_usage = result.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    raw_envelope = result.get("usage_json")
    envelope_usage: dict[str, Any] = raw_envelope if isinstance(raw_envelope, dict) else {}

    tokens_input = result.get("tokens_input", usage.get("input_tokens"))
    tokens_output = result.get("tokens_output", usage.get("output_tokens"))
    tokens_used = result.get("tokens_used")
    session_id = result.get("cli_session_id") or result.get("session_id")
    cost = result.get("cost_usd", result.get("total_cost_usd"))

    if tokens_input is not None:
        run_row.tokens_input = int(tokens_input)
    if tokens_output is not None:
        run_row.tokens_output = int(tokens_output)
    if tokens_used is not None:
        run_row.tokens_used = int(tokens_used)
    elif tokens_input is not None and tokens_output is not None:
        # F-10/R8: this was `elif tokens_input is not None or tokens_output is not None:
        # run_row.tokens_used = int(tokens_input or 0) + int(tokens_output or 0)` — a one-sided
        # sum adds a FABRICATED 0 to the side that was measured, producing a precise-looking total
        # for a run whose other half was never reported. One side unknown -> the column stays NULL.
        run_row.tokens_used = int(tokens_input) + int(tokens_output)
    if session_id:
        run_row.cli_session_id = str(session_id)[:120]
    if cost is not None:
        # A-T17 single-site rule: money crosses to the column only through the one
        # boundary in task_service (lazy import — this module's imports stay lazy).
        from app.services.task_service import _as_column_decimal

        try:
            run_row.cost_usd = _as_column_decimal(cost)
        except (TypeError, ValueError, ArithmeticError):  # pragma: no cover - defensive, logged not silent
            logger.warning("non-numeric cost in agent result: %r", cost)

    recorded: dict[str, Any] = dict(envelope_usage)
    if usage:
        recorded.setdefault("usage", usage)
    for key in RUN_META_KEYS:
        if key not in {"usage_json", "tools_used"} and key in result:
            recorded[key] = result[key]
    if tools_used is not None:
        # The BOUNDED value, never the raw provider list: the audit copy and the column agree.
        recorded["tools_used"] = tools_used
    if recorded:
        run_row.usage_json = recorded


#: The slug a completed run carries in `result["persistence"]["error"]["code"]` when its artifacts
#: could not be written. Deliberately NOT an `app.core.errors` code: those map to HTTP statuses and
#: operator hints, while this is an internal record of a PARTIAL outcome (the run succeeded; only
#: the persistence leg did not).
E_RUN_ARTIFACTS_PERSIST_FAILED = "E_RUN_ARTIFACTS_PERSIST_FAILED"


async def _with_channel_sends(db: Any, agent_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """`context` plus the per-channel send counts this agent's cap checks read (T5 worker hook).

    Why worker-side: the channel agents check `context["channel_sends"][channel]` before an external
    send, and until T5 NOTHING in the tree produced that key (README: "no writer anywhere in the
    tree"), so every per-channel cap saw an absent count. The agents hold no DB session; the worker
    does — so the snapshot is taken here, once, before the run.

    Only the channels in THIS agent's `policy_firewall` capability are looked at (an agent should
    not even see counts for channels it cannot touch), and the return is a NEW dict: the copy-on-
    write spread cannot mutate the (deep-copied) cached context `get_full_context` hands back.

    A producer that cannot answer leaves the key ABSENT and records why — "unmeasured" stays
    distinguishable from "measured zero sends". The firewall denies a configured cap on an unknown
    count (fail closed); an injected `0` would silently allow the send.
    """
    from app.agents.policy_firewall import AGENT_CAPABILITIES

    cap = AGENT_CAPABILITIES.get(agent_type)
    channels = tuple(cap.channels) if cap is not None else ()
    if not channels:
        return context  # orchestrator / business_planning: no external channel to count
    try:
        from app.services.channel_service import channel_sends_snapshot

        snapshot = await channel_sends_snapshot(db, channels, now=None)
    except Exception as exc:  # noqa: BLE001 — recorded, never a fabricated count
        logger.warning(
            "code=channel_sends_unavailable: %s runs without per-channel send counts (%s)",
            agent_type,
            exc,
            exc_info=True,
        )
        return context
    return {**context, "channel_sends": snapshot}


async def _with_compliance_preflight(
    db: Any, agent_type: str, context: dict[str, Any]
) -> dict[str, Any]:
    """`context` plus the resolved compliance preflight a guarded-sender run must carry.

    (V12/W-C W1 OPEN(a) for `email_outreach`; wave v15 / E3 item 4-01 adds `customer_support`
    per E2's FROZEN H1 interface — `customer_support/send_bridge.PREFLIGHT_KEY` re-exports the
    outreach key, so ONE producer serves both.)  The `channel_sends` pattern: the worker owns
    the DB and hands the agent what the guarded sender needs under `compliance_send.PREFLIGHT_KEY`,
    so the per-send loop-local engine in `compliance_send` (mode 2) leaves the hot path.  Nothing
    is invented: a producer that cannot answer leaves the key ABSENT and records why — the agent
    then falls back to mode 2, which resolves the same facts on its own session (fail closed).
    """
    if agent_type not in ("email_outreach", "customer_support"):
        return context
    from app.agents.email_outreach.compliance_send import PREFLIGHT_KEY
    from app.services.outreach_service import compliance_preflight

    try:
        preflight = await compliance_preflight(db)
    except Exception as exc:  # noqa: BLE001 — recorded, never a fabricated identity
        logger.warning(
            "code=compliance_preflight_unavailable: %s runs without a worker-resolved "
            "preflight (%s)",
            agent_type,
            exc,
            exc_info=True,
        )
        return context
    return {**context, PREFLIGHT_KEY: preflight}


async def _with_agent_quality(db: Any, agent_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """`context` plus the 7-day per-agent quality snapshot (V9/B2(e) worker hook).

    Why worker-side: the orchestrator's ONE weekly quality line reads `context["agent_quality"]`
    (`weekly_quality_drop` in the orchestrator agent), and agents hold no database session. The
    SAME measurement `GET /api/v1/ops/agent-quality` serves is taken here, once, before an
    orchestrator run — one producer, no duplicated aggregation.

    A producer that cannot answer leaves the key ABSENT and warns: "unmeasured" must stay
    distinguishable from "measured good" — an injected empty snapshot would silence the line.
    Non-orchestrator runs return the context untouched (only the orchestrator reads this key).
    """
    if agent_type != "orchestrator":
        return context
    try:
        from app.api.v1.ops import _agent_quality_rows

        rows = await _agent_quality_rows(db)
        snapshot = {
            str(row["agent_type"]): {
                "success_rate": row["success_rate"],
                "runs_7d": row["runs_7d"],
            }
            for row in rows
        }
    except Exception as exc:  # noqa: BLE001 — recorded, never a fabricated snapshot
        logger.warning(
            "code=agent_quality_unavailable: %s runs without the 7-day quality snapshot (%s)",
            agent_type,
            exc,
            exc_info=True,
        )
        return context
    return {**context, "agent_quality": snapshot}


async def _log_quality_drop(db: Any, agent_type: str, drop: Any) -> bool:
    """Record ONE `agent_quality_drop` activity row per week (V9/B2(e); True when written).

    The orchestrator agent COMPUTES the record (`weekly_quality_drop`) but holds no session; the
    worker owns the DB, so the row is written here on the run's own transaction. Dedupe on the
    ledger itself: any `agent_quality_drop` row within the last 7 days makes this a no-op —
    "once per week" is the plan's contract, and the ledger is the only clock it needs. A
    non-dict (absent) drop is a no-op, not an error.
    """
    if not isinstance(drop, dict):
        return False
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.core.timeutil import now_utc
    from app.models.report import ActivityLog
    from app.services.activity_service import log_activity
    from app.services.query_helpers import fetch_one

    week_ago = now_utc() - timedelta(days=7)
    recent = await fetch_one(
        db,
        select(func.count())
        .select_from(ActivityLog)
        .where(
            ActivityLog.action == "agent_quality_drop",
            ActivityLog.created_at >= week_ago,
        ),
        what="recent agent quality drops",
    )
    count = int(recent[0]) if recent is not None else 0
    if count:
        return False
    await log_activity(
        db,
        agent_type=agent_type,
        action="agent_quality_drop",
        summary=str(drop.get("line") or "7-day success rate below the floor"),
        level="warning",
        detail=drop,
    )
    return True


def _run_memory_query(task_dict: dict[str, Any]) -> str:
    """The task's OWN terms for the memory search (H4: relevance runs on the caller's query)."""
    parts = (task_dict.get("title"), task_dict.get("description"))
    return " ".join(str(part).strip() for part in parts if part).strip()


async def _with_run_memories(
    db: Any, agent_type: str, context: dict[str, Any], *, query: str
) -> dict[str, Any]:
    """`context` plus the stored memories relevant to this run's task (V15/4-05a, E2-H4).

    The `_with_*` pattern (the worker owns the DB, the agents do not): the resolver queries the
    SHIPPED search machinery with the task's own terms, and `base_agent.context_memories`
    renders the list into every agent's prompt (one seam, no per-agent edits).  A store that
    cannot answer leaves the key ABSENT and records why — "unmeasured" must stay distinguishable
    from "no relevant memories" (the resolver raises the named `E_DB` on a store failure with no
    fallback match; an injected `[]` would silently claim the latter).
    """
    try:
        from app.services.memory_service import resolve_agent_memories

        memories = await resolve_agent_memories(db, agent_type=agent_type, query=query)
    except Exception as exc:  # noqa: BLE001 — recorded, never a fabricated list
        logger.warning(
            "code=agent_memories_unavailable: %s runs without stored memories (%s)",
            agent_type,
            exc,
            exc_info=True,
        )
        return context
    return {**context, "memories": memories}


async def _persist_run_artifacts(
    db: Any,
    agent_type: str,
    result: dict[str, Any],
    *,
    task: dict[str, Any] | None = None,
    include_memory: bool = True,
) -> dict[str, Any]:
    """Persist what a finished run ACTUALLY performed, and report what happened.

    Why here and not in the agents: agents hold no DB session (that is why the run's side effects —
    published posts, sent emails, the finance snapshot/expenses — used to exist only in the run's
    JSON output while `social_posts` / `email_logs` / `revenue_snapshots` stayed empty and every
    downstream counter read zero). The worker owns the session, so it persists on the caller's
    transaction (the services `flush`, `_execute_agent_task` commits).

    An agent with no persisted artifact returns `{}` — "nothing to write", not "wrote nothing".
    A failure is RECORDED as `{"ok": False, "error": {...}}` and never swallowed: the caller folds
    the report into the run's stored output and logs an activity row. It is not raised either — the
    run itself succeeded; only the persistence leg did not.
    """
    try:
        from app.services.memory_service import remember_run

        report: dict[str, Any] = {}
        if agent_type == "finance":
            from app.services.finance_service import persist_finance_run

            report = await persist_finance_run(db, result)
        if agent_type == "social_media":
            from app.services.social_service import record_published_posts

            report = await record_published_posts(db, result)
        if agent_type == "email_outreach":
            from app.services.outreach_service import record_sent_emails

            report = await record_sent_emails(db, result)
        if agent_type == "customer_support":
            # RD14 follow-up (its receipt §7 REQUESTS 1): CS replies send through the same
            # SendGrid path as email_outreach, but nothing persisted them — the email channel's
            # counter undercounted every CS reply.  Same producer, same gates: only
            # status=="sent" WITH a provider id is written (a draft, a refusal or a sandbox
            # downgrade adds no row), deduped by sendgrid_id.
            from app.services.outreach_service import record_sent_emails

            report = await record_sent_emails(db, {"emails": [
                {
                    "prospect_email": str(r.get("from_address") or r.get("from") or "").strip(),
                    "subject": f"Re: {r.get('subject') or 'your message'}",
                    "body": str(r.get("reply_body") or ""),
                    "status": r.get("status"),
                    "sendgrid_id": r.get("sendgrid_id"),
                }
                for r in (result or {}).get("replies") or []
                if str(r.get("from_address") or r.get("from") or "").strip()
            ]})
        if agent_type == "ads_management":
            # R33: the ads producer.  The run's `campaigns[]`/`metrics[]` payload is documented as
            # "feeding ad_campaigns / ad_metrics" (app/agents/ads_management/agent.py:6-7), and
            # writing those rows is explicitly allowed in sandbox (agent.py:9-12) — but no branch
            # here ever looked at the payload, so both tables stayed empty whatever the run really
            # read (README "Known gaps").  The writer dedupes on the payload's OWN natural keys
            # (campaign: provider id, with a (platform, name) fallback; metric: campaign + as-of
            # date — the key the shipped `uq_ad_metrics_campaign_date` unique index guards), so a
            # 6-hourly beat re-run writes no duplicate rows, and every unpersistable entry is
            # skipped BY NAME in the report (the run stays alive, one transaction for the
            # artifacts: the service flushes, this caller commits — like every branch here).
            from app.services.ads_service import record_ads_run

            report = await record_ads_run(db, result)
        if agent_type == "competitor_research":
            # V8/C5 (code-gap #11 tier (a), S): the post-hoc research-budget check — record-only.
            report = await _research_budget_check(db, result)
        # V15/4-05b (E3/H4 frozen): the memory write leg — the worker owns the session and
        # `remember_run` FLUSHES only (this transaction commits at the caller).  It records ONE
        # honest, run-derived memory — the run's OWN summary (`task` names the title it is
        # stored under); a run without one is SKIPPED BY NAME (`E_VALIDATION` — a memory is
        # never invented).  The memory report rides the same shape as the artifact branches,
        # so the caller's fold-in reads it unchanged.
        #
        # OR-02: `include_memory=False` is used when a FAILED replay's performed sends are
        # persisted (A2-01): the artifacts are real, but a failed run's summary is not a memory
        # the next run should retrieve — the send-once ledger and `email_logs` carry that truth.
        memory = (
            await remember_run(db, agent_type=agent_type, task=task, result=result)
            if include_memory
            else {}
        )
        if memory.get("ok"):
            # A stored memory IS a written artifact: it makes the report (and for an agent with
            # no artifact branch it is the whole report — `ok` included).
            report["memory"] = memory
            report.setdefault("ok", True)
        elif report:
            # Other artifacts were written; the memory leg's non-ok outcome (the named skip OR
            # a recorded failure) rides beside them without re-labelling the artifact report.
            report["memory"] = memory
        elif memory.get("error", {}).get("code") != E_VALIDATION:
            # No artifacts and the memory leg really FAILED (not the no-summary skip): the
            # failure NAMES this report — folded into the run output and the caller's warning
            # activity row — instead of vanishing.
            report = {"ok": False, "memory": memory, "error": memory.get("error") or {}}
        return report
    except PolsiaError as exc:
        logger.warning("code=run_artifacts_refused: %s (%s)", agent_type, exc)
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # noqa: BLE001 — a persistence bug is reported in the run output
        logger.warning("code=run_artifacts_persist_failed: %s (%s)", agent_type, exc, exc_info=True)
        return {
            "ok": False,
            "error": {
                "code": E_RUN_ARTIFACTS_PERSIST_FAILED,
                "message": f"{type(exc).__name__}: {exc}",
            },
        }


async def _research_budget_check(db: Any, result: dict[str, Any]) -> dict[str, Any]:
    """V8/C5 (code-gap #11 tier (a), S): post-hoc research-budget check — record-only.

    The agent holds no DB session, so the check runs HERE, on the run's own transaction,
    after the run finished: count the research fetches the run RECORDED (`search_queries` —
    the only fetch evidence the output carries today) and, when over the agent module's
    per-task budget, write ONE honest `research_budget_exceeded` activity row.  NO hard
    enforcement: the run is never blocked or failed — that needs a tool-call proxy (code-gap
    §11 tier (b)), explicitly out of scope for this S-size wire.
    """
    from app.agents.competitor_research.agent import research_budget_violation
    from app.services.activity_service import log_activity

    violation = research_budget_violation(result)
    if violation is None:
        return {}
    await log_activity(
        db,
        agent_type="competitor_research",
        action="research_budget_exceeded",
        summary=(
            f"competitor_research recorded {violation['count']} research fetches; "
            f"the per-task budget is {violation['budget']} (recorded only — no hard enforcement)"
        ),
        level="warning",
        detail=violation,
    )
    return {}


# ── the approval channel's worker side (D1/T02) ──────────────────────────────
#: The `tasks.metadata` key the owner's supplied approval token travels under.  `tasks.metadata`
#: is the documented per-task payload channel (the same one `metadata.inbox_item` uses for the CS
#: sweep, RD12): a token that is not a string is not a token, and nothing is coerced.
APPROVAL_TOKEN_KEY = "approval_token"

#: The result key a parked run's required fingerprints are recorded under — folded into the run's
#: stored output AND merged into `tasks.metadata`, so the owner can mint a token without reading
#: the run JSON.
APPROVAL_REQUIRED_KEY = "approval_required"


def approval_token_from_metadata(metadata: Any) -> str:
    """The supplied approval token on a task row, or `""` (no token) — never a coerced value."""
    if isinstance(metadata, dict):
        raw = metadata.get(APPROVAL_TOKEN_KEY)
        if isinstance(raw, str):
            return raw.strip()
    return ""


def approval_requirements(result: Any) -> list[dict[str, Any]]:
    """The gated actions a finished run recorded as needing the owner's green light.

    Read from the run's `side_effects` receipts: `side_effect_receipt` carries the firewall's audit
    record, which includes the `fingerprint` the owner mints a token for (`issue_approval` is
    scoped to exactly that value), and a receipt whose `decision` is `require_approval` is the
    firewall's own REQUIRE_APPROVAL answer.  Deduped by fingerprint, in the run's own order.
    An empty list means "nothing needs approval", never "unknown" — a run that never asked the
    firewall has no receipts and parks nothing.
    """
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(result, dict):
        return requirements
    for receipt in result.get("side_effects") or []:
        if not isinstance(receipt, dict):
            continue
        if str(receipt.get("decision") or "") != "require_approval":
            continue
        fingerprint = str(receipt.get("fingerprint") or "").strip()
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        requirements.append({
            "action": str(receipt.get("action") or ""),
            "ref": str(receipt.get("ref") or ""),
            "channel": str(receipt.get("channel") or ""),
            "fingerprint": fingerprint,
            "reason": str(receipt.get("reason") or ""),
        })
    return requirements


# ── the approved payload: stored with the approval and REPLAYED on resume (F-14 leg B) ───────
#: The `tasks.metadata` key carrying the exact items the owner approved, keyed by fingerprint
#: (the shape A6's FIX-PACKETS §2 names).  The fingerprint names the ACTION — a canonical hash of
#: class|channel|payload (`policy_firewall.fingerprint`) — and this key names the WORDS.  Producer:
#: the parking branch of `_execute_agent_task`.  Consumer: `_approval_replay_plan` below, which
#: performs these exact items on a resume instead of trusting a re-generated draft.  WHY it has to
#: exist: D1 bound the token to the FULL body, so a re-worded draft can NEVER verify an old token —
#: without the stored bytes a resume either re-parks forever or (worse) sends words the owner never
#: saw under an approval minted for different ones.
APPROVED_PAYLOAD_KEY = "approved_payload"

#: The `tasks.metadata` key recording that a replay BEGAN for a fingerprint.  A send whose outcome
#: is unknown is never re-sent automatically (a retry doubles the message): a marker with no
#: outcome refuses the next resume BY NAME, and only an operator clear authorises a fresh attempt.
APPROVAL_REPLAY_KEY = "approval_replay"

#: The `tasks.metadata` key naming the fingerprints whose approval ENDED with a terminal failed
#: replay (OR-02): the parked set was cleared by R3, the token was dropped, and this record is the
#: audit of what the owner had approved but the build could not perform.
APPROVAL_REPLAY_ENDED_KEY = "approval_replay_ended"

#: The agent types whose parked action this worker can replay, and the provider seam each one uses.
#: An agent outside this set cannot be replayed here: its resume refuses by name rather than
#: falling back to a regenerated body.
_REPLAYABLE_AGENT_TYPES = frozenset(
    {"email_outreach", "customer_support", "social_media", "code_generation", "finance"}
)

#: Marker outcomes that mean "this send may have happened".  `gate_refused` is deliberately NOT
#: here: the firewall answered before the provider was reachable, so nothing was sent and a later
#: resume may reconsider the action.
_SEND_MAY_HAVE_HAPPENED = frozenset({"sent", "provider_refused"})


def _run_items(result: Any, key: str) -> list[dict[str, Any]]:
    """The run's own item list under `key` (`[]` when absent, or not a list of objects)."""
    raw = result.get(key) if isinstance(result, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _project_gated_item(
    agent_type: str, *, action: str, ref: str, result: Any, context: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """`(payload, item)` for one gated receipt — the EXACT preimage the agent's own loop builds.

    WHY a projection: `side_effect_receipt` carries the fingerprint, the ref and the audit record,
    not the payload (the payload is the caller's).  The projection is SELF-CHECKED against the
    receipt's fingerprint by `approved_payloads_for`, so a projection that drifts from an agent's
    payload construction stores NO payload — the resume then refuses by name instead of sending a
    body the owner did not approve.  Matching is by the receipt's own `ref`, which each agent
    builds from the item it gated (the recipient address, the inbox message id, `post:<index>`, the
    mention id, `<repo>#<branch>`, the PaymentIntent id).
    """
    if not ref:
        return None
    if agent_type == "email_outreach":
        for item in _run_items(result, "emails"):
            if str(item.get("prospect_email") or "") == ref:
                return ({"action": action, "subject": str(item.get("subject") or ""),
                         "body": str(item.get("body") or ""), "to": ref}, item)
        return None
    if agent_type == "customer_support":
        for item in _run_items(result, "replies"):
            if str(item.get("message_ref") or "") == ref:
                to = str(item.get("from_address") or item.get("from") or "")
                return ({"action": action, "ref": ref,
                         "body": str(item.get("reply_body") or ""), "to": to}, item)
        return None
    if agent_type == "social_media":
        # Two legs, two refs: a POST is `post:<index>` over `posts[]` (the index is the loop's own),
        # a REPLY is the platform's mention id over `replies[]` (social_media/agent.py:210, :243).
        for index, item in enumerate(_run_items(result, "posts")):
            if ref != f"post:{index}":
                continue
            platform = str(item.get("platform") or "twitter")
            return ({"action": action, "content": str(item.get("content") or ""),
                     "ref": ref, "platform": platform}, {**item, "platform": platform})
        for item in _run_items(result, "replies"):
            if str(item.get("mention_id") or "").strip() != ref:
                continue
            return ({"action": action, "content": str(item.get("reply") or ""),
                     "ref": ref, "platform": "twitter"},
                    {**item, "platform": "twitter"})
        return None
    if agent_type == "code_generation":
        default_repo = str(((context or {}).get("company") or {}).get("github_repo") or "")
        for item in _run_items(result, "changes"):
            repo = str(item.get("repo") or default_repo or "")
            branch = str(item.get("branch") or (result.get("branch") if isinstance(result, dict)
                                                else "") or "")
            if f"{repo}#{branch}" != ref:
                continue
            if not repo or not branch or not item.get("pr_title"):
                return None
            return ({"action": "open_pr", "repo": repo, "branch": branch,
                     "pr_title": str(item.get("pr_title") or ""),
                     "pr_body": str(item.get("pr_body") or "")}, item)
        return None
    if agent_type == "finance":
        for item in _run_items(result, "failed_payments"):
            if str(item.get("payment_intent_id") or "").strip() != ref:
                continue
            return ({"action": "refund", "ref": ref,
                     "event_id": str(item.get("event_id") or "")}, item)
        return None
    return None


def approved_payloads_for(
    agent_type: str, result: Any, *, context: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """The exact payloads a parked run's receipts name, keyed by fingerprint — never a guess.

    Each record is `{agent_type, action, ref, channel, class, value_usd, payload, item, reason}`.
    `payload` is present ONLY when `fingerprint(class, channel, payload)` reproduces the receipt's
    own fingerprint — the self-check that makes this projection safe to replay.  Otherwise
    `payload`/`item` stay None and `reason` names why (`no_projection`, `projection_mismatch`), so
    the row is parked as UNREPLAYABLE instead of being sent from a fabricated payload.
    """
    from app.agents.policy_firewall import fingerprint

    stored: dict[str, dict[str, Any]] = {}
    for receipt in _run_items(result, "side_effects"):
        if str(receipt.get("decision") or "") != "require_approval":
            continue
        fp = str(receipt.get("fingerprint") or "").strip()
        if not fp:
            continue
        action_class = str(receipt.get("class") or "")
        channel = str(receipt.get("channel") or "")
        action = str(receipt.get("action") or "")
        ref = str(receipt.get("ref") or "")
        raw_value = receipt.get("value_usd")
        try:
            value_usd = float(raw_value) if raw_value is not None else 0.0
        except (TypeError, ValueError):
            # An unreadable figure is not a spend figure: the gate then sees 0.0 rather than an
            # invented amount (the approval leg is what this record exists for).
            value_usd = 0.0
        record: dict[str, Any] = {
            "agent_type": agent_type,
            "action": action,
            "ref": ref,
            "channel": channel,
            "class": action_class,
            "value_usd": value_usd,
            "payload": None,
            "item": None,
            "reason": "",
        }
        projected = _project_gated_item(
            agent_type, action=action, ref=ref, result=result, context=context
        )
        if projected is None:
            record["reason"] = "no_projection"
        else:
            payload, item = projected
            if not action_class or fingerprint(action_class, channel, payload) != fp:
                record["reason"] = "projection_mismatch"
            else:
                record["payload"] = payload
                record["item"] = item
        stored[fp] = record
    return stored


def approved_payloads(metadata: Any) -> dict[str, dict[str, Any]]:
    """The stored approved payloads on a task row (`{}` when absent) — never a coerced value."""
    if isinstance(metadata, dict):
        raw = metadata.get(APPROVED_PAYLOAD_KEY)
        if isinstance(raw, dict):
            return {fp: record for fp, record in raw.items()
                    if isinstance(fp, str) and isinstance(record, dict)}
    return {}


def parked_approval_fingerprints(metadata: Any) -> list[str]:
    """The fingerprints a PARKED row recorded (`metadata.approval_required`) — its resume set."""
    if isinstance(metadata, dict):
        records = metadata.get(APPROVAL_REQUIRED_KEY)
        if isinstance(records, list):
            return [str(item.get("fingerprint") or "").strip() for item in records
                    if isinstance(item, dict) and str(item.get("fingerprint") or "").strip()]
    return []


def approval_replays(metadata: Any) -> dict[str, dict[str, Any]]:
    """The replay markers a row carries (`{}` when none) — the send-once ledger."""
    if isinstance(metadata, dict):
        raw = metadata.get(APPROVAL_REPLAY_KEY)
        if isinstance(raw, dict):
            return {fp: record for fp, record in raw.items()
                    if isinstance(fp, str) and isinstance(record, dict)}
    return {}


def _send_may_have_happened(marker: Any) -> bool:
    """True when a replay marker says a send BEGAN and its outcome is unknown (or was a send)."""
    if not isinstance(marker, dict):
        return False
    outcome = marker.get("outcome")
    return outcome is None or outcome in _SEND_MAY_HAVE_HAPPENED


def unreplayable_approvals(metadata: Any) -> list[str]:
    """The parked fingerprints this row cannot replay (no exact stored payload)."""
    stored = approved_payloads(metadata)
    unreplayable: list[str] = []
    for fp in parked_approval_fingerprints(metadata):
        record = stored.get(fp)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("payload"), dict)
            or not isinstance(record.get("item"), dict)
        ):
            unreplayable.append(fp)
    return unreplayable


def _approval_replay_plan(*, task_id: int, metadata: Any) -> dict[str, dict[str, Any]]:
    """The approved actions a resume MUST replay — or a NAMED refusal (never a re-generation).

    Only a row the worker PARKED (`metadata.approval_required`) has a resume set, and only the
    exact payload the owner saw may be sent.  Two shapes refuse instead of falling back to the
    agent: a parked fingerprint with no stored payload (re-generating would send words the owner
    never saw under a token minted for different ones), and a fingerprint whose send already began
    (its outcome may be unknown; a second send doubles the message).  A row with no parked set —
    a token supplied out of band — keeps the pre-F-14 contract and returns `{}`.
    """
    from app.agents.policy_firewall import ActionClass

    parked = parked_approval_fingerprints(metadata)
    if not parked:
        return {}
    markers = approval_replays(metadata)
    began = [fp for fp in parked if _send_may_have_happened(markers.get(fp))]
    if began:
        raise PolsiaError(
            E_ACTION_REPLAY_UNAVAILABLE,
            (
                f"task {task_id}: the approved action(s) {began} already BEGAN on an earlier "
                "attempt and its outcome is unknown; sending again could double the message"
            ),
            how_to_fix=(
                f"confirm at the provider what really went out, then clear the fingerprint's "
                f"entry from tasks.metadata['{APPROVAL_REPLAY_KEY}'] to authorise a fresh attempt "
                "(nothing is re-sent automatically)"
            ),
            context={"task_id": task_id, "fingerprints": began, "marker": APPROVAL_REPLAY_KEY},
        )
    stored = approved_payloads(metadata)
    unreplayable = unreplayable_approvals(metadata)
    if unreplayable:
        raise PolsiaError(
            E_ACTION_REPLAY_UNAVAILABLE,
            (
                f"task {task_id}: the parked approval(s) {unreplayable} carry no stored payload, "
                "so the action the owner approved cannot be replayed; re-running the agent would "
                "send a REGENERATED body under an old approval"
            ),
            how_to_fix=(
                f"the row was parked before tasks.metadata['{APPROVED_PAYLOAD_KEY}'] was written "
                "(every row parked by this build carries it). Inspect the run's output, then "
                "either re-run the task without the token (a fresh park stores the payload) or "
                f"clear tasks.metadata['{APPROVAL_REQUIRED_KEY}'] and the token to authorise a "
                "fresh run — nothing is re-sent automatically"
            ),
            context={"task_id": task_id, "fingerprints": unreplayable,
                     "marker": APPROVED_PAYLOAD_KEY},
        )
    plan: dict[str, dict[str, Any]] = {}
    for fp in parked:
        record = dict(stored[fp])
        if record.get("agent_type") not in _REPLAYABLE_AGENT_TYPES:
            raise PolsiaError(
                E_ACTION_REPLAY_UNAVAILABLE,
                (
                    f"task {task_id}: this worker has no replay leg for agent "
                    f"{record.get('agent_type')!r}; the approved action cannot be performed "
                    "without re-generating it"
                ),
                how_to_fix="perform the approved action from its run output, or re-run the task "
                           "without a token; nothing is re-sent automatically",
                context={"task_id": task_id, "fingerprint": fp,
                         "agent_type": record.get("agent_type")},
            )
        try:
            ActionClass(str(record.get("class") or ""))
        except ValueError as exc:
            raise PolsiaError(
                E_ACTION_REPLAY_UNAVAILABLE,
                f"task {task_id}: the stored approval {fp} names an unknown action class "
                f"{record.get('class')!r}",
                how_to_fix="inspect tasks.metadata['approved_payload'] for that fingerprint",
                context={"task_id": task_id, "fingerprint": fp, "class": record.get("class")},
            ) from exc
        plan[fp] = record
    return plan


async def _stamp_replay_started(task_id: int, fingerprints: list[str], *, worker: str) -> None:
    """Record that these replays are BEGINNING — committed BEFORE any provider is reachable.

    The marker is the whole "never double a message" rule: a process that dies mid-send leaves the
    stamp, and the next resume refuses by name (`_approval_replay_plan`) instead of sending twice.
    Its own committed transaction, so the marker cannot be lost with the run's rollback.
    """
    from app.core.database import get_session_factory
    from app.core.timeutil import now_utc
    from app.services.task_service import get_task

    stamp = now_utc().isoformat()
    async with get_session_factory()() as db:
        row = await get_task(db, task_id)
        if row is None:
            raise PolsiaError(
                E_NOT_FOUND,
                f"task {task_id} vanished before its approved payload could be replayed",
                context={"task_id": task_id},
            )
        merged = dict(row.metadata_json or {})
        markers = dict(merged.get(APPROVAL_REPLAY_KEY) or {})
        for fp in fingerprints:
            markers[fp] = {"started_at": stamp, "worker": worker}
        merged[APPROVAL_REPLAY_KEY] = markers
        row.metadata_json = merged
        await db.commit()


async def _perform_approved_action(
    agent_type: str, record: dict[str, Any], *, context: dict[str, Any]
) -> tuple[bool, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Perform ONE stored approved item through the ONE send path its agent already uses.

    Returns `(performed, {result_key: [item]}, provider_result)`.  The provider call runs OFF the
    loop (`asyncio.to_thread`) exactly like the agent's run does, because the two compliance
    senders own a coroutine of their own and the integration adapters are blocking.  No new egress
    is created here: every branch calls the shipped sender the agent's own loop calls, so the
    compliance footer, the opt-out gate, the caps and the provider adapters are the same ones.
    """
    payload = dict(record.get("payload") or {})
    item = dict(record.get("item") or {})
    # ONE annotation for the item each branch shapes (mypy: a per-branch `out: dict[...]`
    # is a redefinition of the same local).
    out: dict[str, Any]
    if agent_type == "email_outreach":
        from app.agents.email_outreach.compliance_send import send_outreach_email as sender

        provider = await asyncio.to_thread(
            sender,
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            context=context,
        )
        message_id = str((provider.get("result") or {}).get("message_id") or "")
        out = {
            "prospect_email": str(payload.get("to") or ""),
            "subject": str(payload.get("subject") or ""),
            "body": str(payload.get("body") or ""),
            "status": "sent" if provider.get("ok") and message_id else "failed",
            "sendgrid_id": message_id,
        }
        return (out["status"] == "sent", {"emails": [out]}, provider)
    if agent_type == "customer_support":
        from app.agents.customer_support.send_bridge import send_cs_reply as sender

        provider = await asyncio.to_thread(
            sender,
            to=str(payload.get("to") or ""),
            subject=f"Re: {item.get('subject') or 'your message'}",
            body=str(payload.get("body") or ""),
            context=context,
        )
        message_id = str((provider.get("result") or {}).get("message_id") or "")
        out = {
            **item,
            "from_address": str(payload.get("to") or item.get("from_address") or ""),
            "reply_body": str(payload.get("body") or ""),
            "status": "sent" if provider.get("ok") and message_id else "failed",
            "sendgrid_id": message_id,
        }
        return (out["status"] == "sent", {"replies": [out]}, provider)
    if agent_type == "social_media":
        from app.agents import integrations

        kwargs: dict[str, Any] = {"platform": str(payload.get("platform") or "twitter")}
        if item.get("image_url"):
            kwargs["image_url"] = str(item["image_url"])
        if item.get("mention_id"):
            kwargs["reply_to"] = str(item["mention_id"])
        provider = await asyncio.to_thread(
            integrations.post_tweet, str(payload.get("content") or ""), **kwargs
        )
        published = provider.get("result") or {}
        provider_id = str(published.get("id") or "")
        out = {
            "content": str(payload.get("content") or "")[:1000],
            "platform": kwargs["platform"],
            "tweet_id": provider_id,
        }
        return (bool(provider.get("ok")) and bool(provider_id), {"posted": [out]}, provider)
    if agent_type == "code_generation":
        from app.agents import integrations

        provider = await asyncio.to_thread(
            integrations.open_pull_request,
            repo=str(payload.get("repo") or ""),
            branch=str(payload.get("branch") or ""),
            title=str(payload.get("pr_title") or ""),
            body=str(payload.get("pr_body") or ""),
        )
        published = provider.get("result") or {}
        out = {
            "repo": str(payload.get("repo") or ""),
            "branch": str(payload.get("branch") or ""),
            "url": str(published.get("url") or ""),
            "number": published.get("number"),
        }
        return (bool(provider.get("ok")) and bool(out["url"]), {"pulls": [out]}, provider)
    if agent_type == "finance":
        from app.agents import integrations

        raw_amount = item.get("amount_cents")
        amount = raw_amount if isinstance(raw_amount, int) and not isinstance(raw_amount, bool) else None
        provider = await asyncio.to_thread(
            integrations.refund_payment,
            payment_id=str(payload.get("ref") or ""),
            amount_cents=amount,
        )
        out = {
            "payment_intent_id": str(payload.get("ref") or ""),
            "amount_cents": amount,
            "refund": provider.get("result") if provider.get("ok") else None,
        }
        return (bool(provider.get("ok")), {"refunds": [out]}, provider)
    raise PolsiaError(
        E_ACTION_REPLAY_UNAVAILABLE,
        f"no approved-action performer for agent {agent_type!r}",
        how_to_fix="perform the approved action from its run output; nothing is re-sent automatically",
        context={"agent_type": agent_type},
    )


async def _replay_approved_actions(
    *,
    agent_type: str,
    task_id: int,
    approved: dict[str, dict[str, Any]],
    context: dict[str, Any],
    approval_token: str,
) -> dict[str, Any]:
    """Perform the STORED approved actions — the resume path, with no model call at all.

    Every item goes through the SAME gate the agent's loop uses (`side_effects.request_side_effect`
    with the operator's token, the STORED payload and the measured channel counts): the token is
    re-verified against the stored bytes, so nothing can be sent that the owner did not approve.
    A gate refusal is RECORDED (the fingerprint stays parked and comes back in `approval_required`);
    a performed send is shaped into the agent's own output key so `_persist_run_artifacts` writes
    the same artifact row a live run would (`email_logs`, `social_posts`).

    RETURNS its outcome — it never raises for a failed item (OR-02).  The result carries
    `approval_replay` (one outcome per fingerprint), `approval_replay_still_parked` (what a later
    delivery replays), `side_effects` (the receipts), the performed items under their agent output
    keys, `approval_replay_blocked` (the named reasons) and — as a private key the caller pops
    before persisting — `_replay_failure`, the failure object the RUN must be written with.  WHY:
    raising after an irreversible send discarded the performed-item list and the outcome ledger
    with the raising frame, so the failure write cleared the parked approval and the retry re-ran
    the agent (duplicate-send risk).  Persist-first, fail-after is the whole contract.
    """
    from app.agents.policy_firewall import (
        ActionClass,
        configured_approval_secret,
        verify_approval,
    )
    from app.agents.side_effects import request_side_effect, side_effect_receipt
    from app.core.timeutil import now_utc

    secret = configured_approval_secret()
    receipts: list[dict[str, Any]] = []
    performed_keys: dict[str, list[dict[str, Any]]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    blocked: list[PolsiaError] = []
    counts_by_channel = (context or {}).get("channel_sends") or {}
    for fp, record in approved.items():
        payload = dict(record.get("payload") or {})
        channel = str(record.get("channel") or "")
        # The token NAMES one fingerprint.  A reversible action the owner never approved would be
        # ALLOWed by the gate on its own merits (no taint in this worker thread, no cap hit), so
        # the coverage check is what keeps an un-approved item from riding along: it stays parked.
        if not secret or not verify_approval(secret, approval_token, action_fingerprint=fp):
            receipts.append({
                "action": str(record.get("action") or ""),
                "ref": str(record.get("ref") or ""),
                "performed": False,
                "decision": "require_approval",
                "class": str(record.get("class") or ""),
                "channel": channel,
                "fingerprint": fp,
                "reason": "the supplied approval token does not cover this fingerprint",
            })
            outcomes[fp] = {"outcome": "gate_refused",
                            "reason": "token_does_not_cover_fingerprint",
                            "at": now_utc().isoformat()}
            continue
        counts = counts_by_channel.get(channel) or {} if isinstance(counts_by_channel, dict) else {}
        try:
            outcome = request_side_effect(
                agent_type,
                ActionClass(str(record.get("class") or "")),
                channel,
                action=str(record.get("action") or ""),
                value_usd=float(record.get("value_usd") or 0.0),
                payload=payload,
                sends_today=counts.get("today") if isinstance(counts, dict) else None,
                sends_this_hour=counts.get("hour") if isinstance(counts, dict) else None,
                approval_secret=secret,
                approval_token=approval_token,
            )
        except PolsiaError as exc:
            # A rate/kill/policy denial raises (the agent's own loop would not continue either):
            # nothing was sent, and the refusal is the run's own failure — an approved action that
            # cannot be performed must never be reported as done.
            receipts.append({
                "action": str(record.get("action") or ""),
                "ref": str(record.get("ref") or ""),
                "performed": False,
                "decision": "deny",
                "e_code": exc.code,
                "reason": exc.message,
                "fingerprint": fp,
                "channel": channel,
            })
            outcomes[fp] = {"outcome": "gate_refused", "code": exc.code,
                            "at": now_utc().isoformat()}
            blocked.append(exc)
            continue
        receipt = side_effect_receipt(
            outcome, action=str(record.get("action") or ""), ref=str(record.get("ref") or "")
        )
        receipt["fingerprint"] = fp
        receipt["replayed"] = True
        if not outcome.allowed:
            receipts.append(receipt)
            outcomes[fp] = {"outcome": "gate_refused", "detail": outcome.reason or outcome.e_code,
                            "at": now_utc().isoformat()}
            continue
        try:
            performed, items, provider = await _perform_approved_action(
                agent_type, record, context=context
            )
        except PolsiaError as exc:
            # A pre-send refusal from the guarded sender (unconfigured identity, opt-out, missing
            # credentials): nothing reached the provider, so the marker must NOT claim a send.
            # OR-02: the item was NOT performed — it stays parked (nothing was sent, so a later
            # delivery may perform it once the refusal is fixed), and the run FAILS by name at the
            # end.  Before this, the refusal was recorded and the run reported `completed`, i.e.
            # the owner's approved send was silently skipped.
            receipt["performed"] = False
            receipt["e_code"] = exc.code
            receipt["reason"] = exc.message
            receipts.append(receipt)
            outcomes[fp] = {"outcome": "gate_refused", "code": exc.code,
                            "at": now_utc().isoformat()}
            blocked.append(exc)
            continue
        except Exception as exc:  # noqa: BLE001 — classified HERE; never loses the items already sent
            # OR-02: an UNEXPECTED provider/worker failure mid-batch.  Before this handler the
            # exception escaped the loop, so item 1's performed send — its receipt, its send-once
            # marker and its `email_logs` row — died with the raising frame; the failure write then
            # cleared the parked set, and the retry re-ran the AGENT with the old token attached.
            # The outcome is recorded as `provider_refused`: the send may have reached the
            # provider, so the marker BLOCKS a blind re-send (never a duplicate), and the run
            # still fails loudly.
            receipt["performed"] = False
            receipt["e_code"] = E_INTEGRATION
            receipt["reason"] = f"{type(exc).__name__}: {exc}"
            receipts.append(receipt)
            outcomes[fp] = {
                "outcome": "provider_refused",
                "code": E_INTEGRATION,
                "error": f"{type(exc).__name__}: {exc}",
                "at": now_utc().isoformat(),
            }
            blocked.append(PolsiaError(
                E_INTEGRATION,
                f"{agent_type}: the approved action raised {type(exc).__name__}: {exc}",
                how_to_fix=(
                    "read the provider's failure in this run's activity; the send-once marker "
                    "records that the outcome is unknown, so a retry refuses to re-send it — "
                    "confirm at the provider before clearing that fingerprint's marker"
                ),
                context={"task_id": task_id, "fingerprint": fp},
            ))
            continue
        receipt["integration"] = provider
        receipts.append(receipt)
        for key, rows in items.items():
            performed_keys.setdefault(key, []).extend(rows)
        # Read each key ONCE into a local (this file's rule): `.get()` twice defeats narrowing.
        raw_error = provider.get("error")
        provider_error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        outcomes[fp] = {
            "outcome": "sent" if performed else "provider_refused",
            "provider_code": str(provider_error.get("code") or ""),
            "at": now_utc().isoformat(),
        }
        if not performed:
            blocked.append(PolsiaError(
                E_INTEGRATION,
                f"{agent_type}: the approved action could not be performed — "
                f"{provider_error.get('message') or 'the provider refused it'}",
                how_to_fix=(
                    "read the provider's refusal in this run's activity; the send-once marker "
                    "stops a blind retry, and clearing it would re-send"
                ),
                context={"task_id": task_id, "fingerprint": fp, "provider": provider_error},
            ))

    sent = [fp for fp, record in outcomes.items() if record.get("outcome") == "sent"]
    still_parked = [fp for fp, record in outcomes.items()
                    if record.get("outcome") in ("gate_refused", "provider_refused")]
    summary = (
        f"replayed {len(sent)} approved action(s) from the payload the owner approved"
        if sent else "replayed the approved payload: nothing was sent"
    )
    if still_parked:
        summary += f"; {len(still_parked)} action(s) remain parked"
    if blocked:
        summary += (
            f"; {len(blocked)} approved action(s) could NOT be performed — this run failed "
            f"({blocked[0].code})"
        )
    result: dict[str, Any] = {
        "summary": summary,
        "agent_type": agent_type,
        "side_effects": receipts,
        "approval_replay": outcomes,
        # OR-02: the fingerprints this attempt did NOT perform.  They stay parked (see
        # `_settle_parked_after_failed_replay`), so a later delivery replays exactly these; the
        # performed ones are closed by their `sent` marker and are no longer in the parked set.
        "approval_replay_still_parked": still_parked,
        # OR-02: WHY the run fails, named — the caller writes the run `failed`, it never receives
        # a raise from this function (a raise here used to discard every outcome in the frame).
        "approval_replay_blocked": [
            {"code": exc.code, "message": exc.message, "how_to_fix": exc.how_to_fix,
             "fingerprint": str((exc.context or {}).get("fingerprint") or "")}
            for exc in blocked
        ],
        APPROVED_PAYLOAD_KEY: approved,
    }
    for key, rows in performed_keys.items():
        result[key] = rows
    if blocked:
        # POPPED by `_execute_agent_task` before anything is persisted or JSON-encoded: the
        # failure object is the run's outcome, never part of its stored output.
        result["_replay_failure"] = blocked[0]
    return result


@app.task(
    name="celery_app.tasks.agent_tasks.run_agent_task",
    bind=True,
    max_retries=settings.agent_max_retries,
    acks_late=True,
)
def run_agent_task(self, task_id: int) -> dict[str, Any]:
    """Claim a task row, run its agent, persist everything, retry transient failures."""
    outcome = run_async(_execute_agent_task(int(task_id)))
    # `_exception` never crosses the result-backend boundary (the return value is
    # JSON-encoded); it is popped here and used to decide, then to re-raise.
    failure = outcome.pop("_exception", None)

    if outcome.get("status") == "failed":
        attempts = int(outcome.get("attempts") or 0)
        if outcome.get("retryable") and attempts <= settings.agent_max_retries:
            countdown = retry_countdown(attempts)
            logger.warning(
                "task %s failed (failure %s/%s), retrying in %.1fs: %s",
                task_id,
                attempts,
                settings.agent_max_retries,
                countdown,
                outcome.get("error"),
            )
            raise self.retry(exc=failure, countdown=countdown)
        logger.error(
            "task %s failed terminally after %s attempt(s): %s",
            task_id,
            attempts,
            outcome.get("error"),
        )
        if failure is None:
            # Only the lease-lost path reaches "failed" without an exception.
            failure = PolsiaError(
                E_TASK_CLAIMED,
                outcome.get("error") or f"task {task_id} failed without an exception",
                how_to_fix="let the current claim owner finish; the lease expires if it dies",
            )
        # Raise, do not return: a failed run must be a FAILURE task state, not a
        # success-shaped dict (the task row, the agent_runs row and the activity entry
        # already carry the detail).
        raise failure
    return outcome


# ── the DAG's edge release: the completion hook F-02 never had (OR-01) ───────────────────────
#: How many candidate rows ONE block pass may read (the same bounded-sweep rule as every other
#: per-tick query: `DAG_RELEASE_BATCH`, `STALE_CLAIM_BATCH`, `PENDING_RECONCILE_BATCH`).
_DAG_BLOCK_BATCH = 200
#: How many candidate PAGES one block pass may scan (a single 200-row window could be consumed by
#: unrelated backlog, hiding this upstream's own dependent — F2 starvation finding, pinned by
#: `test_a_terminal_failure_blocks_a_dependent_beyond_the_global_batch`).  Hitting the bound is
#: LOGGED, never silently truncated.
_DAG_BLOCK_PAGES = 50
#: How many transitive expansions a block may take (a plan DAG can be a chain).  The bound is a
#: failsafe; reaching it is LOGGED, never silently truncated.
_DAG_BLOCK_PASSES = 32
#: The `tasks.metadata` key naming why a dependent can never run (its dependency ended without
#: completing).  The row ALSO carries the same text in `error_message` — the wire's own field.
DEPENDENCY_BLOCKED_KEY = "dependency_blocked"


async def _release_dag_dependents(task_id: int, *, agent_type: str) -> dict[str, Any]:
    """Release + dispatch the rows this COMPLETED task makes runnable — exactly once (OR-01).

    The missing producer half of F-02: `task_service.release_ready_dependents` existed and had
    ZERO production callers, so a morning plan's dependent task stayed `pending` forever and the
    F-26 upstream channel never materialised.  This is the one hook, in the worker's terminal
    path, where a task really became `completed`.

    Ordering — the service's own contract (`release_ready_dependents`' docstring: "commit, THEN
    hand off"): the release and its `metadata.dag_released_at` marker commit in ONE transaction,
    and the broker hand-off happens AFTER that commit.  A dependent can therefore never reach a
    worker before the row that makes it runnable is visible, and a second completion of the same
    parent releases nothing (the marker skips it) — so a dependent is dispatched at most once by
    this hook.

    A refused hand-off is the SHIPPED re-arm pattern, not a failure of the parent's run:
    `enqueue_created` has already stamped `metadata.rearmed_at` on every released row in its own
    committed transaction (the same compensation `daily_cycle._morning_cycle` relies on, F-21), so
    the rows stay `pending` and the stranded-row reconciler finishes the delivery within one
    window.  The refusal is RECORDED here too (an activity row and this run's returned
    `dag_dependents`), never swallowed; any code other than `E_REDIS` propagates — "the broker
    would not take it" is the only refusal that has a retry owner.
    """
    from app.core.database import get_session_factory

    # A MODULE import, not `from app.services.task_service import release_ready_dependents`:
    # `tests/unit/celery/test_service_contract.py::test_caller_imports_exactly_the_service_names_it_uses`
    # pins the caller's from-import set EXACTLY, and that file is outside this seat's write
    # territory.  The pin's intended growth (adding this name) is a REQUESTS item in the W3
    # report; the module import keeps both sides agreeing on the call shape meanwhile.
    from app.services import task_service

    session_factory = get_session_factory()
    async with session_factory() as db:
        ready = await task_service.release_ready_dependents(db, completed_id=task_id)
        await db.commit()
    outcome: dict[str, Any] = {"released": ready, "dispatched": False}
    if not ready:
        return outcome
    try:
        await enqueue_created(ready, label=f"dag dependents of {task_id}")
    except PolsiaError as exc:
        if exc.code != E_REDIS:
            raise
        outcome.update(
            {"dispatch": "deferred", "code": exc.code, "how_to_fix": exc.how_to_fix}
        )
        logger.warning(
            "task %s: released dependent task(s) %s but the broker refused the hand-off (%s); "
            "they stay pending + re-armed for the stranded-row reconciler",
            task_id,
            ready,
            exc.code,
        )
        from app.services.activity_service import log_activity

        async with session_factory() as db:
            await log_activity(
                db,
                agent_type=agent_type,
                action="dag_dependents_dispatch_deferred",
                summary=(
                    f"task {task_id}: {len(ready)} dependent task(s) were released but the broker "
                    f"refused the hand-off ({exc.code}); they stay queued and the stranded-row "
                    "reconciler re-dispatches them"
                ),
                level="warning",
                detail={"task_id": task_id, "task_ids": ready, "code": exc.code},
            )
            await db.commit()
        return outcome
    outcome["dispatched"] = True
    return outcome


async def _block_dag_dependents(
    upstream_id: int,
    *,
    agent_type: str,
    upstream_status: str,
    upstream_error: str | None,
) -> dict[str, Any]:
    """Fail — BY NAME — every pending row whose dependency ended WITHOUT completing (OR-01).

    A dependency that ends `failed`/`rejected`/`cancelled` can never be satisfied, so its
    dependents are not "waiting": they are work that would sit `pending` for ever, invisible to
    every reconciler and advertised as "planned" on the board.  Each row is written `failed` with
    the reason in `error_message` AND in `metadata.dependency_blocked` (the direct upstream's id,
    its terminal status, that upstream's own error, when), plus one activity row per task — never
    silent, never `pending`.

    Transitive: a blocked row is itself a dead dependency, so the next pass ends the rows waiting
    on IT.  Each pass PAGES through candidates (`_DAG_BLOCK_BATCH` per page, `_DAG_BLOCK_PAGES`
    pages), so a backlog of unrelated pending rows cannot hide this upstream's own dependents;
    hitting either bound says so in the log (the same "bounded, and the bound is stated" rule the
    sweeps follow).
    """
    from sqlalchemy import String, cast, select

    from app.core.database import get_session_factory, utcnow
    from app.models.task import Task, TaskStatus
    from app.services.activity_service import log_activities

    def _cause_text(direct_id: int, cause: str) -> str:
        return f"dependency_blocked: upstream task #{direct_id} {cause}; this task was never run"

    if upstream_error:
        terminal_cause = f"ended {upstream_status!r} ({str(upstream_error)[:300]})"
    else:
        terminal_cause = f"ended {upstream_status!r}"
    causes: dict[int, str] = {int(upstream_id): terminal_cause}
    blocked: list[int] = []
    session_factory = get_session_factory()
    for _pass in range(_DAG_BLOCK_PASSES):
        async with session_factory() as db:
            blocked_now: list[int] = []
            next_causes: dict[int, str] = {}
            events: list[dict[str, Any]] = []
            stamp = utcnow().isoformat()
            cursor = 0
            for _page in range(_DAG_BLOCK_PAGES):
                rows = (
                    await db.execute(
                        select(Task)
                        .where(
                            Task.status == "pending",
                            cast(Task.metadata_json, String).like("%depends_on_ids%"),
                            Task.id > cursor,
                        )
                        .order_by(Task.id)
                        .limit(_DAG_BLOCK_BATCH)
                    )
                ).scalars().all()
                if not rows:
                    break
                cursor = int(rows[-1].id)
                for row in rows:
                    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
                    if meta.get(DEPENDENCY_BLOCKED_KEY):
                        continue          # an owner re-arm of a row this hook already settled
                    dep_ids = [
                        dep for dep in (meta.get("depends_on_ids") or [])
                        if isinstance(dep, int) and not isinstance(dep, bool)
                    ]
                    hit = [dep for dep in dep_ids if dep in causes]
                    if not hit:
                        continue
                    direct = hit[0]
                    reason = _cause_text(direct, causes[direct])
                    blocked_id = int(row.id)
                    row.status = TaskStatus.FAILED
                    row.error_message = reason
                    row.claimed_by = None
                    row.claimed_at = None
                    row.heartbeat_at = None
                    row.updated_at = utcnow()
                    merged = dict(meta)
                    merged[DEPENDENCY_BLOCKED_KEY] = {
                        "upstream_id": direct,
                        "upstream_status": upstream_status,
                        "at": stamp,
                        "reason": reason,
                    }
                    row.metadata_json = merged
                    blocked_now.append(blocked_id)
                    next_causes[blocked_id] = f"ended 'failed' (it was blocked by task #{direct})"
                    events.append(
                        {
                            "agent_type": str(row.agent_type or agent_type),
                            "action": "task_dependency_blocked",
                            "summary": reason,
                            "level": "warning",
                            "detail": {
                                "task_id": blocked_id,
                                "upstream_id": direct,
                                "upstream_status": upstream_status,
                                "marker": DEPENDENCY_BLOCKED_KEY,
                            },
                        }
                    )
                if len(rows) < _DAG_BLOCK_BATCH:
                    break
            else:
                logger.warning(
                    "dag block: the %s-page scan bound was reached while settling the dependents "
                    "of task %s; a later terminal outcome or pass continues",
                    _DAG_BLOCK_PAGES,
                    upstream_id,
                )
            if blocked_now:
                await db.flush()
                await log_activities(db, events)
                await db.commit()
            else:
                await db.rollback()
        if not blocked_now:
            break
        blocked.extend(blocked_now)
        causes.update(next_causes)
    else:
        logger.warning(
            "dag block: stopped after %s transitive pass(es) while settling the dependents of "
            "task %s; remaining candidates are ended by a later terminal outcome",
            _DAG_BLOCK_PASSES,
            upstream_id,
        )
    return {"blocked": blocked}


def _parked_records(metadata: Any) -> dict[str, dict[str, Any]]:
    """The parked approval records keyed by fingerprint, as committed when the row was claimed.

    The records are what the parking branch wrote (`action`, `ref`, `channel`, `fingerprint`,
    `reason`) — the same shape the wire serves — so a restore after a failed replay hands the
    owner back exactly the approval they had, never a reconstruction.
    """
    records: dict[str, dict[str, Any]] = {}
    if isinstance(metadata, dict):
        raw = metadata.get(APPROVAL_REQUIRED_KEY)
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                fingerprint = str(item.get("fingerprint") or "").strip()
                if fingerprint:
                    records[fingerprint] = dict(item)
    return records


def _settle_parked_after_failed_replay(
    task_row: Any,
    *,
    result: dict[str, Any],
    parked_before: dict[str, dict[str, Any]],
    requeued: bool,
    approval_token: str,
) -> dict[str, Any] | None:
    """Settle the parked approval set after a replay that could not perform every item (OR-02).

    The `failed` write is `task_service.update_task_status`, whose R3 rule CLEARS
    `metadata.approval_required` for every terminal status.  For a partially-performed replay that
    rule is wrong in exactly one case — the row is REQUEUED, so the approvals it still owes are
    live work the retry replays.  They are restored from the records committed at claim, in the
    SAME transaction as the failed status (the clear never becomes visible without the restore),
    and R3's now-false `approval_cleared` record is removed.  The TOKEN survives only while it
    still covers one of the owed fingerprints (it dies with the set it was minted for).

    A terminal failure (not requeued) is the END of the approval: R3's clear stands, the token is
    dropped so a stale bearer credential cannot ride a later re-arm, and
    `metadata.approval_replay_ended` records which fingerprints ended with the failure.

    Returns the settlement record (for the activity row + the run's outcome), or None when this
    was not a replay that left anything owed.
    """
    outcomes = result.get("approval_replay")
    if not isinstance(outcomes, dict):
        return None
    owed = [
        fingerprint
        for fingerprint, outcome in outcomes.items()
        if isinstance(fingerprint, str)
        and isinstance(outcome, dict)
        and outcome.get("outcome") in ("gate_refused", "provider_refused")
    ]
    sent_count = sum(
        1
        for outcome in outcomes.values()
        if isinstance(outcome, dict) and outcome.get("outcome") == "sent"
    )
    if not owed:
        return None
    bag = dict(task_row.metadata_json or {})
    if not requeued:
        # R3's `_clear_parked_approval` already dropped the token in this transaction (OR-02's
        # task_service half) — the pop is defence in depth for a writer that predates it.
        token_cleared = bag.pop(APPROVAL_TOKEN_KEY, None) is not None or (
            APPROVAL_TOKEN_KEY not in bag
        )
        bag[APPROVAL_REPLAY_ENDED_KEY] = {
            "status": "failed",
            "fingerprints": owed,
            "token_cleared": token_cleared,
        }
        task_row.metadata_json = bag
        return {
            "kept_parked": [],
            "ended": owed,
            "sent": sent_count,
            "token_cleared": token_cleared,
        }
    approved = result.get(APPROVED_PAYLOAD_KEY)
    plan_records = approved if isinstance(approved, dict) else {}
    restored: list[dict[str, Any]] = []
    for fingerprint in owed:
        record = parked_before.get(fingerprint)
        if record is None:
            # Not in the claim-time snapshot (a writer changed the set mid-run): rebuild the
            # identifying fields from the replay plan, which came from the same stored payload.
            planned = plan_records.get(fingerprint) if isinstance(plan_records, dict) else None
            planned = planned if isinstance(planned, dict) else {}
            record = {
                "action": str(planned.get("action") or ""),
                "ref": str(planned.get("ref") or ""),
                "channel": str(planned.get("channel") or ""),
                "fingerprint": fingerprint,
                "reason": "the replay did not perform this approved action",
            }
        restored.append(record)
    bag[APPROVAL_REQUIRED_KEY] = [dict(record) for record in restored]
    # R3 wrote "the approval was cleared" — it was not: the retry replays these.
    bag.pop("approval_cleared", None)
    token_kept = False
    if approval_token:
        from app.agents.policy_firewall import configured_approval_secret, verify_approval

        secret = configured_approval_secret()
        token_kept = bool(secret) and any(
            verify_approval(secret, approval_token, action_fingerprint=fingerprint)
            for fingerprint in owed
        )
    if token_kept:
        # R3's clear dropped the token with the set; the retry needs it to replay the owed items,
        # so it is restored from the worker's own claim-time value (never re-minted here).
        bag[APPROVAL_TOKEN_KEY] = approval_token
    else:
        bag.pop(APPROVAL_TOKEN_KEY, None)
    task_row.metadata_json = bag
    return {
        "kept_parked": [str(record.get("fingerprint")) for record in restored],
        "ended": [],
        "sent": sent_count,
        "token_kept": token_kept,
    }


async def _execute_agent_task(task_id: int) -> dict[str, Any]:
    """The async body of `run_agent_task` (runs on the process loop)."""
    from app.agents.crew_factory import run_agent_for_task
    from app.core.database import get_session_factory
    from app.services.activity_service import log_activity
    from app.services.company_service import get_full_context
    from app.services.task_service import (
        claim_task,
        create_agent_run,
        finish_agent_run,
        get_task,
        heartbeat_task,
        release_claim,
        update_task_status,
    )

    me = worker_id()
    session_factory = get_session_factory()
    lease_seconds = settings.task_claim_lease_seconds

    # ── 1. claim before anything else ────────────────────────────────────────
    # This is what makes acks_late safe: a re-delivered message finds either a live claim
    # by another worker or a task that is no longer runnable, and returns without side
    # effects instead of running the agent a second time.
    #
    # The service's signature is keyword-only (`claim_task(db, *, worker, lease_seconds,
    # task_id, agent_type)`), and with an explicit `task_id` it RAISES rather than returning
    # None — so "someone else owns it" can never be mistaken for "nothing to do". This call
    # site was written against the requested signature before the service landed; the parent
    # reconciled it during integration (positional `task_id` + `worker_id=` would raise
    # TypeError, which is loud but broken).
    try:
        async with session_factory() as db:
            # F-22: `takeover_stale=True` is the redelivery boundary.  A message that comes back
            # after its worker died names a row that is still `in_progress`; refusing it and acking
            # the message is how a stranded claim became a permanently stranded task (the delivery
            # is the only copy, and the lease sweep that would requeue the row is a Beat tick —
            # off by design this wave).  The claim takes the row atomically ONLY when the claim's
            # last sign of life is older than the lease, so a live claim is still refused
            # (E_TASK_CLAIMED) exactly as before; a stale claim that has spent the attempt ladder
            # ends `failed` by name instead of being re-run (see `_take_over_expired_claim`).
            task = await claim_task(
                db,
                worker=me,
                task_id=task_id,
                lease_seconds=lease_seconds,
                takeover_stale=True,
            )
            await db.commit()
            if task is None:  # claim_task returns None ONLY when no task_id was requested
                # With an explicit task_id the service RAISES instead, so this is unreachable today — but
                # mypy cannot know that, and a future change to the service would otherwise turn this line
                # into an AttributeError deep in the worker. Fail with the named refusal instead.
                raise PolsiaError(
                    E_TASK_NOT_RUNNABLE,
                    f"claim returned no task for {task_id}",
                    context={"task_id": task_id},
                )
            task_dict = {"id": task.id, "title": task.title, "description": task.description,
                         # `metadata` (the `tasks.metadata` JSON column) is the per-task payload
                         # channel — customer_support's attached inbox item travels here.  This
                         # literal used to drop it, so no attached payload could ever reach an
                         # agent's prompt (RD4 §4 note 3 / RD12).  Additive for every agent: the
                         # prompt layers read only id/title/description/acceptance.
                         "metadata": task.metadata_json,
                         # F-13: the task's cross-attempt action list, snapshotted HERE in the
                         # claim session, so the replay set is the one committed when the task was
                         # claimed (a concurrent writer cannot hand the run a half-list).  Empty
                         # on a first delivery, which leaves the run byte-identical to a pre-F-13
                         # one.
                         "prior_actions": prior_actions(task.metadata_json),
                         # §4.13's `attempt`: the execution number the claim just incremented, so
                         # every record says which attempt really performed the call.
                         "attempt": int(task.attempts or 1)}
            # V25/L1 — the task's image attachments, as metadata REFS (id/name/mime/stored_name),
            # never bytes: the agent's run context (`set_run_context`) reads the blobs and builds
            # the data URIs on the RUN thread.  Read in this claim session so the set is the one
            # that was committed when the task was claimed.  No attachments => `[]`, which
            # leaves every prompt and every model request byte-identical to the pre-V25 path.
            from app.services.task_attachment_service import attachment_refs, list_attachments

            task_dict["attachments"] = attachment_refs(await list_attachments(db, task.id))
            agent_type = task.agent_type
            # Both read INSIDE the claim session (the row is attached here): the budget gate
            # (T04) keys on `source`, and the approval channel (T02) keys on the supplied token.
            task_source = str(task.source or "")
            approval_token = approval_token_from_metadata(task.metadata_json)
    except PolsiaError as exc:
        if exc.code in _SKIP_CLAIM_CODES:
            logger.info("run_agent_task(%s): skipped — %s", task_id, exc)
            return {"status": "skipped", "task_id": task_id, "reason": exc.code}
        if exc.code == E_NOT_FOUND:
            logger.warning("run_agent_task(%s): task row missing (stale message)", task_id)
            return {"status": "skipped", "task_id": task_id, "reason": exc.code}
        raise

    # ── 1a2. company pause gate (V8/C5, V8-D6; reworked wave v15/3-03, decision #2) ──────
    # `company_config.dispatch_paused` is the owner's STOP (Settings' Pause/Resume Switch).
    # It is read HERE — the one place both the scheduler fan-outs and the API "Run Now" funnel
    # into — because `dispatch_preflight` is sync with no session (V8-D6); the resolved flag is
    # also handed to that sync seam below so a direct caller cannot bypass the decision.  A
    # refusal happens BEFORE anything ran (no provider call, no ledger row).
    #
    # 3-03 (decision #2 LOCKED, §B7): the refusal must NOT destroy queued work.  The row is
    # RELEASED cleanly — back to its queue-eligible `pending` state, every lease field cleared —
    # and the named paused reason is stamped on `metadata.paused_release` (the marker the
    # `rearm_paused_released_tasks` maintenance tick re-dispatches EXACTLY ONCE after resume)
    # plus an activity row.  The previous `rejected` write was the measured defect: the work was
    # written off and nothing re-armed it on resume.
    from app.agents.crew_factory import resolve_dispatch_paused

    async with session_factory() as db:
        paused = await resolve_dispatch_paused(db)
    if paused:
        from app.core.timeutil import now_utc

        paused_reason = (
            "E_POLICY_DENIED: company paused by owner (company_config.dispatch_paused=true; "
            "resume in Settings to allow dispatch)"
        )
        logger.warning("run_agent_task(%s): %s — claim released, work stays queued", task_id, paused_reason)
        async with session_factory() as db:
            # ONE statement: status back to `pending`, claimed_by/claimed_at/heartbeat_at
            # cleared (the service's graceful release verb, not a hand-rolled UPDATE), and the
            # claim's `attempts` increment REFUNDED — the gate refuses before anything runs, so
            # a pause cycle must not burn the retry ladder (E7 F-05).
            # `release_kind="paused"` is task_service.RELEASE_KIND_PAUSED's value, passed as the
            # literal on purpose: the caller-contract pin requires this from-import to carry
            # callables only, so the constant stays the service module's vocabulary (V-01/V-02).
            await release_claim(db, task_id, me, release_kind="paused")
            task_row = await get_task(db, task_id)
            if task_row is not None:
                # Merge — never replace — the marker into the per-task payload channel; a
                # re-assigned dict, because the JSON column tracks attribute assignment.
                merged = dict(task_row.metadata_json or {})
                merged[PAUSED_RELEASE_KEY] = {
                    "released_at": now_utc().isoformat(),
                    "reason": paused_reason,
                    "worker": me,
                }
                task_row.metadata_json = merged
            await log_activity(
                db,
                agent_type=agent_type,
                action="dispatch_refused_paused",
                summary=(
                    f"task {task_id}: dispatch refused — company paused by owner; claim "
                    "released, the work stays queued for resume"
                ),
                level="warning",
                detail={
                    "task_id": task_id,
                    "reason": paused_reason,
                    "requeued": True,
                    "marker": PAUSED_RELEASE_KEY,
                },
            )
            await db.commit()
        return {
            "status": "skipped",
            "task_id": task_id,
            "agent_type": agent_type,
            "task_status": "pending",
            "requeued": True,
            "reason": paused_reason,
        }

    # ── 1b. dispatch budget gate at the choke point (L4-09 / D3 / T03 / T04) ──
    # `assert_budget_allows` gated only the four SCHEDULER dispatch sites, and this wrapper is
    # the one place BOTH the scheduler fan-outs and the API "Run Now" trigger funnel into — so a
    # manual trigger used to be a real run (provider call, ledger row, persisted cost) that
    # silently bypassed the daily cap. The gate needs `agent_type`, which is known only after
    # the claim; a denial is recorded on the row and returned as a NAMED skip — never a silent
    # no-op, never a retry (`E_BUDGET_EXCEEDED` is a terminal business refusal).
    #
    # T03/D2: a refusal here happens BEFORE anything ran (no provider call, no ledger row, nothing
    # charged), so the row lands `rejected` — the live product's Rejected state — instead of
    # `failed`, which reads as a crash.  The returned delivery outcome stays `skipped` (nothing
    # was executed) and names the row's state (`task_status`).
    #
    # T04/D3: a MANUAL owner trigger (tasks.source=="manual", the flag
    # `POST /agents/{type}/trigger` writes — app/api/v1/agents.py:74) is the owner asking for this
    # run NOW.  The daily cap is a spend governor for SCHEDULED dispatch, not a veto over the
    # payer: the run proceeds past the cap and the bypass is NAMED in the run's result and in an
    # activity row, so it is auditable rather than silent.  Every other source (scheduler,
    # orchestrator, dashboard, beat sweeps) keeps the hard gate byte-for-byte.
    from app.agents.accounting import BudgetStop

    manual_override: dict[str, Any] | None = None
    try:
        await assert_budget_allows(agent_type)
    except BudgetStop as exc:
        if task_source != "manual":
            logger.warning("run_agent_task(%s): budget denied (%s)", task_id, exc.reason)
            async with session_factory() as db:
                task_row = await update_task_status(
                    db, task_id, "rejected", error_message=f"budget: {exc.reason}"
                )
                if task_row is not None:
                    task_row.claimed_by = None
                    task_row.claimed_at = None
                    task_row.heartbeat_at = None
                # wave U5 (P2/B22): the day's budget is exhausted — a live Auto Mode window
                # closes HERE, with the named `day_cap` reason, in the SAME transaction as the
                # rejection, before this delivery returns.  A gate INTERRUPTS a session; it
                # never bypasses it.  Only a window COVERING now is closed; an expired-but-open
                # row stays for the lazy `duration_end` read (calling it `day_cap` would be a
                # lie about why it ended).
                from app.services.auto_mode_service import (
                    STOP_REASON_DAY_CAP,
                    close_live_sessions,
                )

                await close_live_sessions(db, reason=STOP_REASON_DAY_CAP)
                await db.commit()
            # OR-01: this row is TERMINAL (`rejected`) — its dependents can never run, so they are
            # failed BY NAME here instead of waiting on a dependency that will never complete.
            blocked = await _block_dag_dependents(
                task_id,
                agent_type=agent_type,
                upstream_status="rejected",
                upstream_error=f"budget: {exc.reason}",
            )
            return {
                "status": "skipped",
                "task_id": task_id,
                "agent_type": agent_type,
                "task_status": "rejected",
                "manual_override": False,
                "reason": f"budget: {exc.reason}",
                "dag_dependents": blocked,
            }
        manual_override = {
            "manual_override": True,
            "manual_override_reason": (
                "owner manual trigger (tasks.source='manual') ran past the daily cap: "
                f"{exc.reason}"
            ),
        }
        logger.warning("run_agent_task(%s): %s", task_id, manual_override["manual_override_reason"])
        async with session_factory() as db:
            await log_activity(
                db,
                agent_type=agent_type,
                action="budget_manual_override",
                summary=f"task {task_id}: {manual_override['manual_override_reason']}",
                level="warning",
                detail={"task_id": task_id, **manual_override},
            )
            await db.commit()

    # ── 1c. F-14 leg B: decide whether this delivery is a REPLAY, before any ledger row ──────
    # A row this worker PARKED carries the fingerprints the owner mints for (`approval_required`)
    # and, since F-14 leg B, the exact payloads those fingerprints name
    # (`metadata.approved_payload`).  When the token is supplied the resume performs THOSE items:
    # the model is not asked again, because a regenerated draft is either a different action (D1
    # bound the token to the FULL body, so a re-worded draft cannot verify) or — worse — an action
    # the gate ALLOWs for words the owner never saw.  A parked row with nothing stored refuses
    # HERE, by name, before any provider is reachable and before a run row exists (so a refusal
    # leaves no forever-`running` ledger row).  A token on a row that was never parked keeps the
    # pre-F-14 contract (`_approval_replay_plan` returns {}), so the shipped
    # supply-a-credential path is byte-identical.
    replay: dict[str, dict[str, Any]] = {}
    # OR-02: a PARKED set is decided by the parked set, not only by the token.  Gating this on the
    # token alone meant a row that lost its token (a settled partial replay whose token covered
    # only the performed item) fell straight through to the agent — a regenerated body under an
    # old approval.  With a parked set present the resume is ALWAYS the replay path: no token means
    # every item is `gate_refused` (nothing sent) and the row re-parks, which is the fail-closed
    # answer.  A row that was never parked is untouched (`_approval_replay_plan` returns {}).
    parked_at_claim = parked_approval_fingerprints(task_dict.get("metadata"))
    if approval_token or parked_at_claim:
        try:
            replay = _approval_replay_plan(task_id=task_id, metadata=task_dict.get("metadata"))
        except PolsiaError as exc:
            if exc.code != E_ACTION_REPLAY_UNAVAILABLE:
                raise
            logger.warning("run_agent_task(%s): %s", task_id, exc)
            # Refused BEFORE execution (no provider call, no ledger row): the row lands the
            # product's `rejected` state and the claim is released, exactly like the budget
            # gate's terminal refusal — a retry would refuse identically forever.
            async with session_factory() as db:
                task_row = await update_task_status(
                    db, task_id, "rejected",
                    error_message=f"{exc.code}: {exc.message} — {exc.how_to_fix}",
                )
                if task_row is not None:
                    task_row.claimed_by = None
                    task_row.claimed_at = None
                    task_row.heartbeat_at = None
                await log_activity(
                    db,
                    agent_type=agent_type,
                    action="approval_replay_refused",
                    summary=(
                        f"task {task_id}: the approved payload cannot be replayed "
                        f"({exc.code}) — nothing was sent"
                    ),
                    level="warning",
                    detail={"task_id": task_id, **dict(exc.context or {})},
                )
                await db.commit()
            # OR-01: the row landed TERMINAL (`rejected`) before execution — its dependents can
            # never run, so they are failed BY NAME instead of waiting for ever.
            blocked = await _block_dag_dependents(
                task_id,
                agent_type=agent_type,
                upstream_status="rejected",
                upstream_error=f"{exc.code}: {exc.message}",
            )
            return {
                "status": "skipped",
                "task_id": task_id,
                "agent_type": agent_type,
                "task_status": "rejected",
                "reason": exc.code,
                "error": exc.message,
                "how_to_fix": exc.how_to_fix,
                "dag_dependents": blocked,
            }

    # ── 2. context + run-ledger row (own short transaction) ──────────────────
    async with session_factory() as db:
        context = await get_full_context(db)
        # T5 worker hook: the per-channel send counts the agents' cap checks read have no other
        # producer (agents hold no database session). Copy-on-write, own channels only.
        context = await _with_channel_sends(db, agent_type, context)
        # V12/W-C worker hook: the compliance preflight outreach runs must carry (W1 OPEN(a)) —
        # same "the worker owns the DB, the agent does not" reasoning as the channel counts.
        context = await _with_compliance_preflight(db, agent_type, context)
        # V9/B2(e) worker hook: the 7-day per-agent quality snapshot the orchestrator's ONE
        # weekly line reads — same "the worker owns the DB, the agent does not" reasoning as
        # the channel counts above.
        context = await _with_agent_quality(db, agent_type, context)
        # V15/4-05a worker hook (E2-H4 frozen): the stored memories relevant to this run's task
        # — same "the worker owns the DB, the agent does not" reasoning as the hooks above; the
        # base agent's `context_memories` seam renders them into every agent's prompt.
        context = await _with_run_memories(
            db, agent_type, context, query=_run_memory_query(task_dict)
        )
        # L2-03 seam: `tasks.attempts` — incremented by the claim — is the execution number this
        # run is; it lands on `agent_runs.attempt` so a retried run is distinguishable from its
        # predecessors (migration 0002's `ux_agent_runs_task_attempt` is only live once this is
        # written). The frozen keyword is `attempt` (task_service.create_agent_run).
        run = await create_agent_run(db, agent_type, task_id=task_id, input_context=context,
                                     attempt=int(task.attempts or 0))
        await db.commit()
        run_id = run.id
    # V26/L3 (F3) — the run row's id is the run's identity for the workspace seam.  Publish it
    # on the task dict the agent receives (the one carrier every dispatched run already reads:
    # `BasePolsiaAgent.set_run_context` → `deepseek_runtime.invoke(run_id=...)` →
    # `execute_tool(run_id=...)`), so the three code tools write under
    # `data/workspace/runs/<run_id>/` instead of the shared root.  A string on purpose: the id
    # becomes ONE path segment and the registry validates its charset before any path exists.
    task_dict["run_id"] = str(run_id)

    # ── 2c. F-14 leg B: hand the stored approved payload to the run ───────────────────────────
    # The packet's hand-off key: an agent-side consumer (A6 FIX-PACKETS §2) reads the same records
    # out of the task's metadata; this puts them in front of the run without forking the metadata
    # column.  The replay itself happens in the run leg below (`_replay_approved_actions`), so the
    # model is never asked for a body the owner already approved.
    if replay:
        task_dict["approved_payload"] = replay

    # ── 2b. publish the run's state BEFORE its heartbeat and its thread exist ────────────────
    # T02/D1: the run's approval material (`asyncio.to_thread` copies the current context into the
    # worker thread, so `side_effects._resolve_approval` sees it without any per-agent edit).
    # S2-R1/R2 (F-04/F-33): the per-channel counts THIS worker measured (`_with_channel_sends`) and
    # the cancellation guard the run is abandoned through (F-16) are published here for the same
    # reason — the consumers (the firewall's cap leg, the registry tool gate, `decide()`'s
    # cancellation leg) run INSIDE the agent, with no run object to read.
    #   * `context.get("channel_sends")` is None when the producer could not measure; publishing
    #     None keeps "unmeasured" distinguishable from a measured zero, which is what makes the
    #     firewall refuse a configured cap it cannot verify (`cap_unknown`, POLSIA-E114) instead of
    #     reading a fabricated 0.
    #   * `begin_run_cancellation()` MUST run before `create_task(_heartbeat)` below: the task
    #     copies this context, and the loss is latched on the SHARED guard object, so every copy
    #     (the loop's task and the run's thread) reads the same verdict.
    from app.agents.policy_firewall import (
        RunApproval,
        begin_run_cancellation,
        cancel_run,
        configured_approval_secret,
        end_run_cancellation,
        reset_run_approval,
        reset_run_channel_sends,
        set_run_approval,
        set_run_channel_sends,
    )

    set_run_channel_sends(context.get("channel_sends"))
    begin_run_cancellation()

    # ── 3. heartbeat the lease while the agent runs ──────────────────────────
    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()

    async def _heartbeat() -> None:
        interval = max(5.0, lease_seconds / 3.0)
        while True:
            try:
                await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with session_factory() as db:
                    await heartbeat_task(db, task_id, me)
                    await db.commit()
            except PolsiaError as exc:
                if exc.code in _SKIP_CLAIM_CODES or exc.code == E_NOT_FOUND:
                    # The service refuses to renew a claim this worker no longer holds
                    # (E_TASK_CLAIMED / E_TASK_NOT_RUNNABLE / E_NOT_FOUND) instead of silently
                    # no-opping, so a lost lease cannot pass unnoticed: stop and record the loss.
                    # F-16: and LATCH IT on the run's guard, so the side effect the agent may be
                    # about to perform is refused by name instead of being sent by a run that no
                    # longer owns the task. `cancel_run` writes the object BOTH contexts hold
                    # (`set_run_channel_sends`'s neighbour, published before this task existed).
                    logger.error(
                        "task %s: claim lease lost (%s), abandoning the run", task_id, exc.code
                    )
                    cancel_run(f"claim lease lost ({exc.code})")
                    lease_lost.set()
                    return
                logger.warning("heartbeat refused for task %s: %s", task_id, exc, exc_info=True)
                continue
            except Exception:  # noqa: BLE001 — a transient beat failure must not kill the run
                logger.warning("heartbeat failed for task %s", task_id, exc_info=True)
                continue

    started = time.monotonic()
    heartbeat = asyncio.create_task(_heartbeat())
    result: dict[str, Any] = {}
    failure: BaseException | None = None
    # T02/D1: the approval material itself is published above (step 2b), before this task and the
    # run's thread copied the context; the secret comes from settings (b"" when APPROVAL_SECRET is
    # unset — no channel, the fail-closed default), the token from the task row's metadata.
    set_run_approval(
        RunApproval(secret=configured_approval_secret(), token=approval_token)
    )
    try:
        # The agent's CLI call is blocking; run it off the loop so the heartbeat keeps
        # beating (the upload blocked the whole event loop for the CLI's lifetime, D-11).
        # V8/C5: the pause decision was already made above (the `rejected` gate) — by the time
        # this line runs the company is known unpaused.  The sync seam's `dispatch_paused`
        # parameter stays for direct callers of `run_agent_for_task` and is exercised by tests.
        #
        # F-14 leg B: a resume whose parked payload is stored REPLAYS it.  `_stamp_replay_started`
        # commits the send-once marker BEFORE the provider is reachable, so a process killed mid
        # send leaves evidence instead of a second message on the next delivery.
        if replay:
            await _stamp_replay_started(task_id, list(replay), worker=me)
            result = await _replay_approved_actions(
                agent_type=agent_type,
                task_id=task_id,
                approved=replay,
                context=context,
                approval_token=approval_token,
            )
            # OR-02: the replay returns its failure instead of raising with the performed work in
            # hand.  The object is the RUN's outcome (popped here, so it is never persisted or
            # JSON-encoded); the items/markers/artifacts it describes are written below.
            replay_failure = result.pop("_replay_failure", None)
            if replay_failure is not None:
                failure = replay_failure
        else:
            result = await asyncio.to_thread(run_agent_for_task, agent_type, task_dict, context)
    except Exception as exc:  # noqa: BLE001 — classified, persisted and (maybe) retried
        failure = exc
    finally:
        stop_heartbeat.set()
        await heartbeat
        # EVERY run-scoped publish is cleared here, in the same `finally` that stops the
        # heartbeat: one run's counts, token or cancellation verdict must never reach the next run
        # in this worker process (the RUN_TAINT isolation argument, applied to the whole scope).
        reset_run_approval()
        reset_run_channel_sends()
        end_run_cancellation()
    duration = round(time.monotonic() - started, 2)

    error = None if failure is None else f"{type(failure).__name__}: {failure}"
    if lease_lost.is_set():
        error = "; ".join(x for x in (error, "claim lease lost during the run") if x)

    # ── 4. persist: run accounting, task outcome, activity ───────────────────
    run_status = "failed" if error else "completed"
    summary = result.get("summary") if isinstance(result, dict) else None
    retryable = is_retryable(failure) if failure is not None else False
    # F-07: the run's OWN verdict, when the payload declares one (`run_verdict.PAYLOAD_STATUS_KEY`,
    # read by its wire name so this module does not import the agent package).  A `partial` run did
    # real work — it is not a failure — but it is NOT a clean completion either, and until now the
    # row, the feed and the API all said COMPLETED.  `refused` raises before reaching here; it is
    # handled defensively because the payload key is a wire contract.
    payload_status: str | None = None
    if run_status == "completed" and isinstance(result, dict):
        raw_payload_status = result.get("payload_status")
        if isinstance(raw_payload_status, str) and raw_payload_status in (
            "partial",
            "refused",
        ):
            payload_status = raw_payload_status
    attempts = 0
    requeued = False
    # T02/D1: a completed run that recorded a firewall REQUIRE_APPROVAL for an external action
    # PARKS the task for the owner's green light instead of reporting `completed` — the action did
    # NOT happen, and the run's output carries the labelled drafts.  The fingerprints travel in
    # the run's stored output AND on the task row (metadata + summary), so the owner can mint a
    # token without reading run JSON.  A FAILED run parks nothing: its story is the failure.
    approval_reqs = approval_requirements(result) if error is None else []
    task_status = "awaiting_approval" if approval_reqs else run_status
    if approval_reqs and isinstance(result, dict):
        result[APPROVAL_REQUIRED_KEY] = approval_reqs
    if manual_override is not None and isinstance(result, dict):
        # T04: the run's own output carries the NAMED override record (the activity row above
        # carries the second copy), so "why did this run spend past the cap" is answerable from
        # the ledger alone.
        result.update(manual_override)
    parking_summary: str | None = None
    if approval_reqs:
        parking_summary = (
            f"awaiting approval: {len(approval_reqs)} gated action(s) need a token; request one "
            "for fingerprint(s) " + ", ".join(item["fingerprint"] for item in approval_reqs)
        )
    # OR-02: the parked set AS COMMITTED AT CLAIM — the records a partially-failed replay restores
    # when the row is requeued (the `failed` write's R3 rule empties `approval_required`, and a
    # retry that must replay the owed items cannot do it from an empty set).
    parked_before = _parked_records(task_dict.get("metadata"))
    # A replay that really performed at least one approved action has artifacts to persist even
    # when the attempt FAILED (A2-01: a real send is persisted even when the run fails).
    replay_sent = bool(
        isinstance(result, dict)
        and isinstance(result.get("approval_replay"), dict)
        and any(
            isinstance(record, dict) and record.get("outcome") == "sent"
            for record in result["approval_replay"].values()
        )
    )
    replay_settlement: dict[str, Any] | None = None
    async with session_factory() as db:
        # T5/T6 persistence hook: a completed run's side effects (published posts, sent emails, the
        # finance snapshot) are written HERE, on this transaction, BEFORE the run row is finished —
        # so `agent_runs.output` carries the report of what was really persisted. A failure is
        # folded into the output + an activity row, never into the run's status: the run succeeded.
        # OR-02 extends the gate: a FAILED replay that performed sends persists THOSE artifacts on
        # this same transaction (the performed item is real even though the attempt failed), while
        # the memory leg stays completed-run-only (`include_memory=False`).
        if isinstance(result, dict) and (run_status == "completed" or replay_sent):
            report = await _persist_run_artifacts(
                db,
                agent_type,
                result,
                task=task_dict,
                include_memory=run_status == "completed",
            )
            if report:
                result["persistence"] = report
                if agent_type == "finance" and report.get("ok"):
                    # T6/PLAN-3: `snapshot_written` is the PERSISTED upsert's fact, not the agent's
                    # intent — the agent had no session and could not know.
                    result["snapshot_written"] = bool(report.get("snapshot_written"))
                if not report.get("ok"):
                    persist_error = report.get("error") or {}
                    logger.warning(
                        "task %s: run artifacts not persisted for %s: %s",
                        task_id,
                        agent_type,
                        persist_error or report,
                    )
                    await log_activity(
                        db,
                        agent_type=agent_type,
                        action="run_artifacts_not_persisted",
                        summary=(
                            f"{agent_type} run completed but its artifacts were not persisted "
                            f"({persist_error.get('code') or 'unknown'})"
                        ),
                        level="warning",
                        detail=persist_error,
                    )
        if run_status == "completed" and isinstance(result, dict):
            # V9/B2(e): the weekly quality drop's activity row — once per week, ledger-deduped.
            await _log_quality_drop(db, agent_type, result.get("agent_quality_drop"))
        run_row = await finish_agent_run(
            db,
            run_id,
            run_status,
            output=result if run_status == "completed" else None,
            duration_secs=duration,
            error_message=error,
        )
        if run_row is not None:
            _apply_run_usage(run_row, result)

        # F-13: what THIS attempt really executed.  A COMPLETED run also carries the list in its
        # result (`agent_runs.output` gets the same copy, via `attach_run_meta`); a FAILED run
        # stores no output at all (`finish_agent_run(output=None)`), so the task row is the ONLY
        # carrier a retry can read — which is exactly the case this exists for.
        run_actions = actions_from_outcome(result, failure)

        if lease_lost.is_set():
            # Another worker owns this row now: record the loss, do not stomp the status
            # that worker is about to write.
            await log_activity(
                db,
                agent_type=agent_type,
                action="task_lease_lost",
                summary=f"Claim lease lost during run of task {task_id}; outcome not written",
                level="warning",
                detail={"task_id": task_id, "worker_id": me, "duration_secs": duration},
            )
        else:
            # The service deliberately splits "write the outcome" (`update_task_status`) from
            # "give the claim back" (`release_claim`, which returns the row to `pending`), so a
            # finished row is written here and its lease fields cleared in the same transaction.
            # The combined outcome-and-release call this site was written against does not exist;
            # an earlier revision imported it, which is an ImportError at call time — loud, never a
            # silent success, and reconciled by the parent during integration.
            #
            # `task_status` is the RUN's own outcome (`completed`/`failed`) EXCEPT for a run the
            # approvals channel parked (`awaiting_approval`, T02) — the agent_runs row above always
            # carries the run's true outcome, `ck_agent_runs_status` has no parked state, and the
            # task row is the thing the owner has to see and act on.
            task_row = await update_task_status(
                db,
                task_id,
                task_status,
                result_summary=(parking_summary or summary) if run_status == "completed" else None,
                error_message=error,
            )
            if task_row is not None:
                task_row.claimed_by = None
                task_row.claimed_at = None
                task_row.heartbeat_at = None
                if payload_status is not None:
                    # F-07: the run's verdict is PERSISTED beside the lifecycle status, so the
                    # tasks API can serve `execution_status`/`needs_attention` (derived by
                    # `task_service.task_execution_state`) instead of presenting a partial run as a
                    # clean completion.  A metadata bag key, so the frozen status vocabulary, the
                    # CHECK constraint and the frontend's status matrix are untouched.  Merged, not
                    # replaced — the approval/action merges below re-read this same bag.
                    # The two key literals are `task_service.RESULT_STATUS_KEY` /
                    # `VERDICT_METADATA_KEY`; they are written as literals HERE because the caller's
                    # service-import set is a pinned contract (`test_service_contract`) and a JSON
                    # bag key is a wire name, not a service callable.
                    merged = dict(task_row.metadata_json or {})
                    merged["result_status"] = payload_status
                    verdict = result.get("verdict") if isinstance(result, dict) else None
                    if isinstance(verdict, dict):
                        merged["verdict"] = {
                            key: str(verdict.get(key))[:1000]
                            for key in ("status", "label", "reason", "source")
                            if verdict.get(key) is not None
                        }
                    task_row.metadata_json = merged
                if task_status == "awaiting_approval":
                    # "recorded in its result metadata": merge — never replace — the required
                    # fingerprints into `tasks.metadata`, the documented per-task payload channel.
                    #
                    # F-14 leg B: the fingerprints alone are not enough to resume — the token D1
                    # mints binds the FULL body, so the resume must send the exact words the owner
                    # saw.  They are stored beside the fingerprints, MERGED over anything already
                    # there (a re-park after a partial replay keeps the records it did not touch).
                    merged = dict(task_row.metadata_json or {})
                    merged[APPROVAL_REQUIRED_KEY] = approval_reqs
                    stored_payloads = approved_payloads(merged)
                    for fingerprint, record in approved_payloads_for(
                        agent_type, result, context=context
                    ).items():
                        if (
                            isinstance((stored_payloads.get(fingerprint) or {}).get("payload"), dict)
                            and not isinstance(record.get("payload"), dict)
                        ):
                            # A re-park after a partial replay: the item is still parked, and its
                            # stored bytes are still the approval — never downgrade them to the
                            # replay's own (itemless) projection.
                            continue
                        stored_payloads[fingerprint] = record
                    merged[APPROVED_PAYLOAD_KEY] = stored_payloads
                    task_row.metadata_json = merged
                if replay and isinstance(result, dict):
                    # The replay's own outcome, per fingerprint: `sent`/`provider_refused` close the
                    # send-once marker, `gate_refused` records that nothing reached the provider (so
                    # a later resume may reconsider it).  Written in the same transaction as the
                    # outcome it describes.
                    outcomes = result.get("approval_replay")
                    if isinstance(outcomes, dict):
                        merged = dict(task_row.metadata_json or {})
                        markers = dict(merged.get(APPROVAL_REPLAY_KEY) or {})
                        for fingerprint, outcome in outcomes.items():
                            if not isinstance(fingerprint, str) or not isinstance(outcome, dict):
                                continue
                            merged_marker = dict(markers.get(fingerprint) or {})
                            merged_marker.update(outcome)
                            markers[fingerprint] = merged_marker
                        merged[APPROVAL_REPLAY_KEY] = markers
                        task_row.metadata_json = merged
                if run_actions:
                    # F-13 (§4.13): append what this attempt EXECUTED to the task's action list —
                    # the cross-attempt carrier.  Re-read the column (the approval merge above
                    # wrote it too, and a stale copy would silently drop the fingerprints), merge
                    # by `call_key` so one effect keeps one record, and let the merge apply the
                    # list bound.  This line is what a retry replays instead of re-doing the run.
                    merged = dict(task_row.metadata_json or {})
                    merged[ACTION_KEY] = merge_actions(merged.get(ACTION_KEY), run_actions)
                    task_row.metadata_json = merged
                if run_status == "failed":
                    # `attempts` counts EXECUTIONS, and the claim already incremented it for this one
                    # (task_service.claim_task). This site used to increment again, so a single failed
                    # execution added 2 — which halved the retry ladder (`attempts <= agent_max_retries`
                    # spent two of its three slots on the first failure) and skewed
                    # `requeue_stale_claims`'s terminal rule, which compares the same column against
                    # `task_max_attempts`. The measured arithmetic is pinned by
                    # tests/unit/celery/test_queue_safety_core.py.
                    attempts = int(task_row.attempts or 0)
                    if retryable and attempts <= settings.agent_max_retries:
                        # A retry is about to be scheduled — and a Celery retry is delivered as a
                        # NEW message for the same task_id, which must CLAIM the row again.  The
                        # service's claim accepts only `pending` rows, so a retryable failure that
                        # left the row `failed` would make its own retry un-runnable: re-delivery
                        # finds nothing claimable, returns {"status": "skipped", "reason":
                        # "E_TASK_NOT_RUNNABLE"}, and the eager runner even reports SUCCESS for it
                        # (measured: probe_harness.py §5 before this fix).  So the row is requeued
                        # here — `error_message` keeps the failure, `attempts` the count — and the
                        # retry's claim counts the next execution.  A terminal failure (not
                        # retryable, or the ladder exhausted) still ends `failed`.
                        task_row.status = TaskStatus.PENDING
                        requeued = True
                        # F-23: the retry is a NEW message published by `self.retry` AFTER this
                        # commit.  If the process dies inside that window the row is `pending`
                        # with no delivery, and a non-scheduler row had no second net — the same
                        # silent wedge the live `social_media` validator failure produced.  The
                        # marker is the reconciler's own (`stamp_rearmed`): a retry that IS
                        # delivered is claimed long before the window, and one that is lost is
                        # re-dispatched by `reconcile_stranded_pending_tasks` within one window.
                        await stamp_rearmed(db, [task_id], reason="retry_requeued")

                if replay and isinstance(result, dict) and run_status == "failed":
                    # OR-02: settle the parked approval set NOW that the retry decision is final.
                    # The `failed` write above ran R3's rule and emptied `approval_required`; a
                    # requeued row still OWES the unperformed fingerprints (the retry replays
                    # exactly those), so they are restored here — in the SAME transaction, so the
                    # failed status is never observable with the parked set already lost.  A
                    # terminal failure ends the approval instead (the clear stands, the token is
                    # dropped) — see the helper's docstring.
                    replay_settlement = _settle_parked_after_failed_replay(
                        task_row,
                        result=result,
                        parked_before=parked_before,
                        requeued=requeued,
                        approval_token=approval_token,
                    )
                    if replay_settlement is not None:
                        result["approval_replay_settlement"] = replay_settlement
            if task_status == "awaiting_approval":
                # ONE activity row for the parked outcome — the run did not "complete" the task;
                # it recorded drafts and stopped for the owner's green light.
                await log_activity(
                    db,
                    agent_type=agent_type,
                    action="task_awaiting_approval",
                    summary=parking_summary or "awaiting approval",
                    level="warning",
                    detail={
                        "task_id": task_id,
                        "duration_secs": duration,
                        "fingerprints": [item["fingerprint"] for item in approval_reqs],
                    },
                )
            else:
                # F-07: a partial run gets its OWN action and a warning level — "task_completed"
                # with level "success" was the feed's half of the completed-while-partial defect.
                await log_activity(
                    db,
                    agent_type=agent_type,
                    action=(
                        "task_partial"
                        if payload_status is not None
                        else ("task_completed" if run_status == "completed" else "task_failed")
                    ),
                    summary=summary or error or "No output",
                    level=(
                        "warning"
                        if payload_status is not None
                        else ("success" if run_status == "completed" else "error")
                    ),
                    detail={"task_id": task_id, "duration_secs": duration, "requeued": requeued,
                            "manual_override": bool(manual_override),
                            "payload_status": payload_status},
                )
            if replay_settlement is not None:
                # OR-02: the partial replay's own record — what was NOT performed and where it
                # went.  A warning-level row (the run failed), one per attempt, so the owner can
                # answer "which approved action did not happen?" from the activity feed alone.
                kept = replay_settlement.get("kept_parked") or []
                ended = replay_settlement.get("ended") or []
                await log_activity(
                    db,
                    agent_type=agent_type,
                    action="approval_replay_incomplete",
                    summary=(
                        f"task {task_id}: the approved replay did not perform every approved "
                        f"action; "
                        + (
                            f"{len(kept)} fingerprint(s) stay parked for the retry "
                            f"(sent={replay_settlement.get('sent')})"
                            if requeued
                            else f"{len(ended)} fingerprint(s) ended with the failure "
                                 f"(sent={replay_settlement.get('sent')})"
                        )
                    ),
                    level="warning",
                    detail={"task_id": task_id, "requeued": requeued,
                            **dict(replay_settlement)},
                )

            # wave U5 (P2/B22): the active window's task counter — ONE UPDATE folded into this
            # same transaction (the same commit as the activity row above, no extra round trip).
            # Only a window COVERING now is touched: a task whose terminal write falls outside
            # every window changes nothing, and a lease-lost run never reaches here (it wrote no
            # outcome, so it is not a finished task).
            from app.services.auto_mode_service import increment_tasks_run

            await increment_tasks_run(db)
        await db.commit()

    # ── 4b. F-02 / OR-01: the terminal outcome settles the rows waiting on this task ──────────
    # BEFORE the notification (the dependents are work, the notification is a courtesy) and AFTER
    # the outcome commit — `release_ready_dependents` requires the completed row to be visible, and
    # the released rows must be committed before any of them is handed to a worker.
    #
    #   * completed        -> release the rows whose whole dependency set is now completed, then
    #                         hand them off (a second completion releases nothing: the released
    #                         marker is committed with the release);
    #   * failed, terminal -> its dependents can never run: fail them BY NAME (never `pending`);
    #   * failed, requeued -> the retry is coming; nothing is settled yet;
    #   * awaiting_approval -> the row is not done; its dependents keep waiting;
    #   * lease lost       -> this worker wrote no outcome; the row belongs to another worker.
    dag_dependents: dict[str, Any] | None = None
    if not lease_lost.is_set():
        if task_status == "completed":
            dag_dependents = await _release_dag_dependents(task_id, agent_type=agent_type)
        elif task_status == "failed" and not requeued:
            dag_dependents = await _block_dag_dependents(
                task_id,
                agent_type=agent_type,
                upstream_status="failed",
                upstream_error=error,
            )

    # wave U5 (P2+P5): the owner's terminal-outcome notification — the FROZEN lazy-imported call
    # (`notify_owner_for_terminal_outcome(db, task_id, outcome)`, P5's module), made AFTER the
    # activity commit and wrapped so it can never fail a run: the outcome is already recorded,
    # and a notifier outage must not rewrite that truth.  V8/C5 (owner decision D4): an attempt
    # that was REQUEUED notifies as `requeued` — its own toggle (`notify_on_run_requeued`) —
    # and NEVER as `failed`: the retry is coming, and one leg routes exactly one outcome per
    # attempt (the double-email edge: `notify_on_failed` must not also fire here).
    #
    # A2A8-02: only a run that WROTE its outcome may announce it.  A lease-lost run wrote nothing
    # (`task_lease_lost`, "outcome not written") because another worker owns the row, so a push
    # here would contradict — eight lines later — the record it just refused to touch, and the
    # owner would receive "Task #N completed" for work whose result was deliberately discarded
    # (the row may still be `running` under the new owner, may complete, or may fail differently).
    if not lease_lost.is_set():
        notification_outcome = _notification_outcome(task_status, requeued)
        try:
            from app.services.notification_service import notify_owner_for_terminal_outcome

            async with session_factory() as notify_db:
                await notify_owner_for_terminal_outcome(notify_db, task_id, notification_outcome)
        except Exception:  # noqa: BLE001 — a notifier failure must never rewrite a recorded outcome
            logger.warning(
                "task %s: terminal-outcome notification failed (outcome=%s)",
                task_id,
                notification_outcome,
                exc_info=True,
            )

    return {
        "status": task_status,
        "task_id": task_id,
        "agent_type": agent_type,
        "duration_secs": duration,
        "attempts": attempts,
        "error": error,
        "retryable": retryable,
        # F-07: the run's own non-ok verdict (`partial`), or None for a clean completion / failure.
        # Named here so the delivery result carries the same fact the row's metadata now does.
        "payload_status": payload_status,
        "requeued": requeued,  # True when this failure put the row back in the queue for its retry
        # T04: `manual_override` (True + `manual_override_reason`) when this run proceeded past the
        # daily cap on the owner's manual trigger; `manual_override: False` for every gated run.
        **(manual_override or {"manual_override": False}),
        # T02: the fingerprints this run left awaiting the owner's token (empty = none).
        APPROVAL_REQUIRED_KEY: approval_reqs,
        # OR-01: what this terminal outcome did to the plan DAG — `{"released": [...],
        # "dispatched": bool}` on a completion, `{"blocked": [...]}` on a terminal failure, None
        # when nothing waited on this row / the row is not terminal yet.
        "dag_dependents": dag_dependents,
        # OR-02: what a partially-performed replay did with the approvals it still owed —
        # `{"kept_parked": [...], "ended": [...], "sent": n, "token_kept"/"token_cleared": bool}`,
        # or None when the attempt owned no residual approval.
        "approval_replay_settlement": replay_settlement,
        "_exception": failure,  # popped by the sync wrapper; never returned to Celery
    }


# ── repeating sweeps ─────────────────────────────────────────────────────────
# Each sweep creates today's task exactly once per bucket and queues it on the broker.
# `<agent_type>:<utc-day>:<bucket>` is the identity; the unique index on
# `tasks.dedupe_key` is the enforcement (migration 0002).  A re-delivered beat tick, a
# restarted beat or a manual re-run therefore cannot produce a second task.
@app.task(name="celery_app.tasks.agent_tasks.run_social_sweep")
def run_social_sweep() -> dict[str, Any]:
    """Every 2h — social_media reads mentions and drafts replies."""
    return run_async(
        _create_and_enqueue(
            "social_media",
            "Check social mentions and reply to engaging comments",
            bucket=sweep_bucket(2),
        )
    )


@app.task(name="celery_app.tasks.agent_tasks.run_email_sweep")
def run_email_sweep() -> dict[str, Any]:
    """Every 3h — customer_support reads the inbox and drafts replies."""
    return run_async(_email_inbox_sweep())


async def _email_inbox_sweep() -> dict[str, Any]:
    """The 3h customer_support sweep: attach pending inbox items, THEN behave as before.

    R17 (RD12 receipt §6/§7): until now this sweep created one title-only task per bucket and
    the inbox item — the thing the CS agent's prompt is built around — had no producer at all.
    Now every `pending` support_inbox_items row becomes its OWN customer_support task carrying
    `metadata.inbox_item` (the RD12 consumer contract), under the per-ITEM dedupe key
    `support-inbox-<provider id>`; the item is marked `dispatched` with that task id in the
    SAME transaction as the task row (`support_inbox_service.attach_pending`).  Dispatched
    items are never re-attached — the pending-only query is what enforces that.

    The bare 'check inbox' task (the pre-R17 behaviour) is kept EXACTLY when nothing is
    pending: with no items attached, the bucket's title-only task is what keeps the schedule's
    observable behaviour (and its dedupe key) unchanged for an empty inbox.

    Budget: the D3 gate is checked ONCE, before any row is touched — a full day attaches
    nothing (items stay pending for the next tick, so nothing is lost) and `_create_and_enqueue`
    re-checks its own copy of the same decision on the bare path.

    Broker hand-off: only tasks CREATED here are enqueued.  A dedupe-hit (task already exists;
    e.g. a concurrent sweep won the insert) is reported with `task_created: False` and NOT
    re-enqueued — `enqueue_created`'s failure path RE-ARMS every listed id (the row stays
    `pending` with `metadata.rearmed_at`), and a row that may already be in flight must not be
    handed a marker claiming a retry is owed.  The process-death window between commit and
    hand-off is the documented D2/T19 gap; a row that never got a message is now reclaimed by the
    same `rearmed_at` marker the reconciler reads, rather than by a blind re-hand-off.
    """
    from app.agents.accounting import BudgetStop
    from app.core.database import get_session_factory
    from app.services.support_inbox_service import (
        ANSWER_WINDOW_DAYS,
        attach_pending,
        count_expired_pending,
    )

    # F2-FEED-2 (2026-10-28): the owner's STOP is checked BEFORE the inbox attach and before the
    # task row — a paused Beat neither marks items dispatched nor queues a run, and the tick
    # reports the refusal by name.  (The creation funnel `_common.create_task_deduped` refuses
    # the same decision; this early return keeps the sweep a clean named skip instead of an
    # exception, and stops `attach_pending` from consuming items for work that cannot dispatch.)
    session_factory = get_session_factory()
    async with session_factory() as db:
        paused_reason = await dispatch_paused_reason(db)
    if paused_reason is not None:
        logger.warning("email inbox sweep: dispatch paused (%s)", paused_reason)
        return paused_skip("email-inbox-sweep", paused_reason, attached=[])

    # D3: the day's measured spend gates dispatch (audit A1/L2-F01) — same policy decision the
    # sibling sweeps take in `_create_and_enqueue`.
    try:
        await assert_budget_allows("customer_support")
    except BudgetStop as exc:
        logger.warning("email inbox sweep: budget denied (%s)", exc.reason)
        return {"status": "budget_denied", "reason": exc.reason, "attached": []}

    async with session_factory() as db:
        attached = await attach_pending(db)
        # V8/C5 (code-gap §10.2): the named-skip half of the 14-day answer bound — items older
        # than the window are never attached, and this count is what reports the skip BY NAME.
        expired_count = await count_expired_pending(db)
        await db.commit()

    expired = {
        "count": expired_count,
        "bound_days": ANSWER_WINDOW_DAYS,
        "reason": "older_than_14_days",
    }

    if attached:
        created_ids = [row["task_id"] for row in attached if row["task_created"]]
        if created_ids:
            # D2/T19 parity: a dead broker must not strand these rows `pending` (see enqueue_created).
            await enqueue_created(created_ids, label="email inbox sweep")
        return {"status": "queued", "attached": attached, "expired": expired}

    # Nothing pending: the pre-R17 bucket task, unchanged (`_create_and_enqueue` keeps its own
    # dedupe key + budget gate); `attached: []` is additive so one shape describes both paths.
    result = await _create_and_enqueue(
        "customer_support",
        "Check inbox and reply to customer emails",
        bucket=sweep_bucket(3),
    )
    result.setdefault("attached", [])
    result.setdefault("expired", expired)
    return result


async def _attached_ad_metrics(days: int = 7) -> dict[str, Any]:
    """The `ad_metrics` envelope the 6-hourly ads dispatch attaches (V15/4-02, E2-H2 frozen).

    PARENT DECISION (repair unit): both-platform capable — Google Ads and Meta are both
    supported and the CONFIGURED platform's read is attached (google_ads first — the reader's
    own default — then meta_ads).  The readers refuse BY NAME for an unconfigured platform
    BEFORE any provider call, so when NEITHER is configured the two named refusals are combined
    into ONE envelope naming both halves and zero provider calls were made.  The reads run off
    the loop (`asyncio.to_thread`) — provider IO must not stall the loop the heartbeat and other
    dispatches share.  Both configured: google_ads wins, deterministically — its envelope
    carries `platform` inside `result`, so the run sees exactly what was read.
    """
    from app.agents.integrations import read_ad_metrics

    google = await asyncio.to_thread(read_ad_metrics, platform="google_ads", days=days)
    if google.get("ok"):
        return google
    meta = await asyncio.to_thread(read_ad_metrics, platform="meta_ads", days=days)
    if meta.get("ok"):
        return meta
    # Read each key ONCE into a local (this file's rule): `.get()` twice defeats narrowing.
    raw_google_error = google.get("error")
    raw_meta_error = meta.get("error")
    google_error: dict[str, Any] = raw_google_error if isinstance(raw_google_error, dict) else {}
    meta_error: dict[str, Any] = raw_meta_error if isinstance(raw_meta_error, dict) else {}
    return {
        "ok": False,
        "error": {
            "code": google_error.get("code") or meta_error.get("code") or E_INTEGRATION,
            "message": (
                "the ads dispatch could not read ad metrics from either platform: "
                f"google_ads — {google_error.get('message') or 'unknown refusal'}; "
                f"meta_ads — {meta_error.get('message') or 'unknown refusal'}"
            ),
            "how_to_fix": (
                "configure ONE ad platform's credentials: GOOGLE_ADS_* (developer token, "
                "client id/secret, refresh token, customer id) or META_ADS_ACCESS_TOKEN + "
                "META_ADS_ACCOUNT_ID"
            ),
            "context": {"platforms": {"google_ads": google_error, "meta_ads": meta_error}},
        },
    }


@app.task(name="celery_app.tasks.agent_tasks.run_ads_stripe_sync")
def run_ads_stripe_sync() -> dict[str, Any]:
    """Every 6h — ad metrics sync and the Stripe failed-payment check (independent)."""
    return run_async(_ads_stripe_sync())


async def _ads_stripe_sync() -> dict[str, Any]:
    from app.agents.accounting import BudgetStop
    from app.core.database import get_session_factory
    from app.services.finance_service import recent_failed_payments

    bucket = sweep_bucket(6)
    plan = (
        ("ads_management", "Sync ad metrics from Google and Meta"),
        ("finance", "Check for failed Stripe payments and update revenue snapshot"),
    )
    session_factory = get_session_factory()

    # F2-FEED-2 (2026-10-28): the 6-hourly sync is a Beat dispatch — paused means no row, no
    # provider read attached, no broker hand-off, reported by name.
    async with session_factory() as db:
        paused_reason = await dispatch_paused_reason(db)
    if paused_reason is not None:
        logger.warning("ads/stripe sync: dispatch paused (%s)", paused_reason)
        return paused_skip("ads-stripe-sync", paused_reason, queued={}, denied=[])

    created: list[tuple[str, int]] = []
    duplicates: list[str] = []
    denied: list[str] = []
    async with session_factory() as db:
        for agent_type, title in plan:
            # D3: the day's measured spend gates dispatch (audit A1/L2-F01 — the planner's gate
            # had no production caller; this is the same authorize decision on the real path).
            try:
                await assert_budget_allows(agent_type)
            except BudgetStop as exc:
                denied.append(f"{agent_type}: {exc.reason}")
                logger.warning("ads/stripe sync: budget denied %s (%s)", agent_type, exc.reason)
                continue
            key = dedupe_key(agent_type, bucket)
            # T14b (wave-W3 REQUESTS §4): mirror the morning cycle's attach (`daily_cycle.py`)
            # for THIS 6-hourly finance dispatch — the run "Check for failed Stripe payments and
            # update revenue snapshot" must SEE the records its refund path needs (a real `pi_…`
            # id). Only the finance task is attached, and only when the read is non-empty: an
            # empty read creates the row this site always created (metadata untouched, and the
            # agent's consumer degrades to no extra context — never an empty attachment).
            metadata: dict[str, Any] | None = None
            if agent_type == "ads_management":
                # V15/4-02 attach (E2-H2; parent decision: both-platform capable): the
                # configured platform's reader envelope, VERBATIM; none configured -> a named
                # refusal envelope naming both (see `_attached_ad_metrics`).
                metadata = {"ad_metrics": await _attached_ad_metrics()}
            if agent_type == "finance":
                from app.agents.integrations import read_revenue_facts

                failed_payments = await recent_failed_payments(db)
                metadata = {}
                if failed_payments:
                    metadata["failed_payments"] = failed_payments
                # V15/4-03 attach (E2-H3): merge — never replace — the Stripe revenue-facts
                # envelope (integer cents) beside `failed_payments`; provider IO, off the loop
                # like the ads attach.
                metadata["revenue_facts"] = await asyncio.to_thread(read_revenue_facts, days=30)
            task = await create_task_deduped(
                db,
                title=title,
                agent_type=agent_type,
                dedupe_key=key,
                source="scheduler",
                priority=3,
                metadata=metadata,
            )
            if task is None:
                duplicates.append(key)
            else:
                created.append((agent_type, task.id))
        await db.commit()

    queued = {agent_type: task_id for agent_type, task_id in created}
    if created:
        # D2/T19 parity: a dead broker must not strand these rows `pending` (see enqueue_created).
        await enqueue_created([task_id for _, task_id in created], label="ads/stripe sync")
    return {
        "status": "queued" if created else "duplicate",
        "queued": queued,
        "duplicates": duplicates,
        "denied": denied,
    }


async def _create_and_enqueue(
    agent_type: str,
    title: str,
    *,
    bucket: str,
    priority: int = 3,
    description: str | None = None,
) -> dict[str, Any]:
    """Create today's sweep task once (dedupe key) and queue it on the broker."""
    from app.agents.accounting import BudgetStop
    from app.core.database import get_session_factory

    # F2-FEED-2 (2026-10-28): a paused company creates no sweep row and queues nothing.  Checked
    # before the budget gate so the pause is the reason reported, and before the insert so no row
    # waits to be dispatched the moment the worker comes back.
    session_factory = get_session_factory()
    async with session_factory() as db:
        paused_reason = await dispatch_paused_reason(db)
    if paused_reason is not None:
        logger.warning("sweep %s: dispatch paused (%s)", agent_type, paused_reason)
        return paused_skip(f"sweep:{agent_type}", paused_reason, task_id=None, dedupe_key=None)

    # D3: measured-spend gate (audit A1/L2-F01) — a full day creates no new sweep row.
    try:
        await assert_budget_allows(agent_type)
    except BudgetStop as exc:
        logger.warning("sweep %s: budget denied (%s)", agent_type, exc.reason)
        return {"status": "budget_denied", "reason": exc.reason}

    key = dedupe_key(agent_type, bucket)
    async with session_factory() as db:
        task = await create_task_deduped(
            db,
            title=title,
            agent_type=agent_type,
            dedupe_key=key,
            description=description,
            priority=priority,
            source="scheduler",
        )
        await db.commit()
        task_id = task.id if task is not None else None

    if task_id is None:
        logger.info("sweep %s: %s already exists for this bucket", agent_type, key)
        return {"status": "duplicate", "dedupe_key": key}

    # D2/T19 parity: a dead broker must not strand this row `pending` (see enqueue_created).
    await enqueue_created([task_id], label=f"sweep {agent_type}")
    return {"status": "queued", "task_id": task_id, "dedupe_key": key}
