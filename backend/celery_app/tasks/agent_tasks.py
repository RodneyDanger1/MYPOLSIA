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
    E_AGENT_BAD_OUTPUT,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_TIMEOUT,
    E_DB,
    E_INTEGRATION,
    E_NOT_FOUND,
    E_REDIS,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    PolsiaError,
)
from app.models.task import TaskStatus
from celery_app.tasks._common import (
    assert_budget_allows,
    create_task_deduped,
    dedupe_key,
    enqueue_created,
    run_async,
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
        E_AGENT_BAD_OUTPUT,
        E_INTEGRATION,
        E_DB,
        E_REDIS,
    }
)

# A run whose outcome must never be written back to the task row: the row belongs to
# somebody else now.
_SKIP_CLAIM_CODES = (E_TASK_CLAIMED, E_TASK_NOT_RUNNABLE)


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
    """
    if not isinstance(result, dict):
        return

    from app.agents.base_agent import RUN_META_KEYS

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
        if key != "usage_json" and key in result:
            recorded[key] = result[key]
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


async def _persist_run_artifacts(db: Any, agent_type: str, result: dict[str, Any]) -> dict[str, Any]:
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
        if agent_type == "finance":
            from app.services.finance_service import persist_finance_run

            return await persist_finance_run(db, result)
        if agent_type == "social_media":
            from app.services.social_service import record_published_posts

            return await record_published_posts(db, result)
        if agent_type == "email_outreach":
            from app.services.outreach_service import record_sent_emails

            return await record_sent_emails(db, result)
        if agent_type == "customer_support":
            # RD14 follow-up (its receipt §7 REQUESTS 1): CS replies send through the same
            # SendGrid path as email_outreach, but nothing persisted them — the email channel's
            # counter undercounted every CS reply.  Same producer, same gates: only
            # status=="sent" WITH a provider id is written (a draft, a refusal or a sandbox
            # downgrade adds no row), deduped by sendgrid_id.
            from app.services.outreach_service import record_sent_emails

            return await record_sent_emails(db, {"emails": [
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

            return await record_ads_run(db, result)
        return {}
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
        heartbeat_task,
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
            task = await claim_task(db, worker=me, task_id=task_id, lease_seconds=lease_seconds)
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
                         "metadata": task.metadata_json}
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
                await db.commit()
            return {
                "status": "skipped",
                "task_id": task_id,
                "agent_type": agent_type,
                "task_status": "rejected",
                "manual_override": False,
                "reason": f"budget: {exc.reason}",
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

    # ── 2. context + run-ledger row (own short transaction) ──────────────────
    async with session_factory() as db:
        context = await get_full_context(db)
        # T5 worker hook: the per-channel send counts the agents' cap checks read have no other
        # producer (agents hold no database session). Copy-on-write, own channels only.
        context = await _with_channel_sends(db, agent_type, context)
        # L2-03 seam: `tasks.attempts` — incremented by the claim — is the execution number this
        # run is; it lands on `agent_runs.attempt` so a retried run is distinguishable from its
        # predecessors (migration 0002's `ux_agent_runs_task_attempt` is only live once this is
        # written). The frozen keyword is `attempt` (task_service.create_agent_run).
        run = await create_agent_run(db, agent_type, task_id=task_id, input_context=context,
                                     attempt=int(task.attempts or 0))
        await db.commit()
        run_id = run.id

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
                    logger.error(
                        "task %s: claim lease lost (%s), abandoning the run", task_id, exc.code
                    )
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
    # T02/D1: publish the run's approval material BEFORE the agent runs.  `asyncio.to_thread`
    # copies the current context into the worker thread, so the side-effect path INSIDE the agent
    # (`side_effects._resolve_approval`) sees it without any per-agent edit.  The secret comes from
    # settings (b"" when APPROVAL_SECRET is unset — no channel, the fail-closed default), the
    # token from the task row's metadata.  The `finally` clears it so one run's token cannot leak
    # into the next run in this worker process.
    from app.agents.policy_firewall import (
        RunApproval,
        configured_approval_secret,
        reset_run_approval,
        set_run_approval,
    )

    set_run_approval(
        RunApproval(secret=configured_approval_secret(), token=approval_token)
    )
    try:
        # The agent's CLI call is blocking; run it off the loop so the heartbeat keeps
        # beating (the upload blocked the whole event loop for the CLI's lifetime, D-11).
        result = await asyncio.to_thread(run_agent_for_task, agent_type, task_dict, context)
    except Exception as exc:  # noqa: BLE001 — classified, persisted and (maybe) retried
        failure = exc
    finally:
        stop_heartbeat.set()
        await heartbeat
        reset_run_approval()
    duration = round(time.monotonic() - started, 2)

    error = None if failure is None else f"{type(failure).__name__}: {failure}"
    if lease_lost.is_set():
        error = "; ".join(x for x in (error, "claim lease lost during the run") if x)

    # ── 4. persist: run accounting, task outcome, activity ───────────────────
    run_status = "failed" if error else "completed"
    summary = result.get("summary") if isinstance(result, dict) else None
    retryable = is_retryable(failure) if failure is not None else False
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
    async with session_factory() as db:
        # T5/T6 persistence hook: a completed run's side effects (published posts, sent emails, the
        # finance snapshot) are written HERE, on this transaction, BEFORE the run row is finished —
        # so `agent_runs.output` carries the report of what was really persisted. A failure is
        # folded into the output + an activity row, never into the run's status: the run succeeded.
        if run_status == "completed" and isinstance(result, dict):
            report = await _persist_run_artifacts(db, agent_type, result)
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
                if task_status == "awaiting_approval":
                    # "recorded in its result metadata": merge — never replace — the required
                    # fingerprints into `tasks.metadata`, the documented per-task payload channel.
                    merged = dict(task_row.metadata_json or {})
                    merged[APPROVAL_REQUIRED_KEY] = approval_reqs
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
                await log_activity(
                    db,
                    agent_type=agent_type,
                    action="task_completed" if run_status == "completed" else "task_failed",
                    summary=summary or error or "No output",
                    level="success" if run_status == "completed" else "error",
                    detail={"task_id": task_id, "duration_secs": duration, "requeued": requeued,
                            "manual_override": bool(manual_override)},
                )
        await db.commit()

    return {
        "status": task_status,
        "task_id": task_id,
        "agent_type": agent_type,
        "duration_secs": duration,
        "attempts": attempts,
        "error": error,
        "retryable": retryable,
        "requeued": requeued,  # True when this failure put the row back in the queue for its retry
        # T04: `manual_override` (True + `manual_override_reason`) when this run proceeded past the
        # daily cap on the owner's manual trigger; `manual_override: False` for every gated run.
        **(manual_override or {"manual_override": False}),
        # T02: the fingerprints this run left awaiting the owner's token (empty = none).
        APPROVAL_REQUIRED_KEY: approval_reqs,
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
    re-enqueued — `enqueue_created`'s failure path marks every listed id `failed`, which must
    not stomp a row that may already be in flight.  The process-death window between commit
    and hand-off is the documented D2/T19 gap (nothing reconciles an undelivered row), not
    something a blind re-hand-off makes honest.
    """
    from app.agents.accounting import BudgetStop
    from app.core.database import get_session_factory
    from app.services.support_inbox_service import attach_pending

    # D3: the day's measured spend gates dispatch (audit A1/L2-F01) — same policy decision the
    # sibling sweeps take in `_create_and_enqueue`.
    try:
        await assert_budget_allows("customer_support")
    except BudgetStop as exc:
        logger.warning("email inbox sweep: budget denied (%s)", exc.reason)
        return {"status": "budget_denied", "reason": exc.reason, "attached": []}

    session_factory = get_session_factory()
    async with session_factory() as db:
        attached = await attach_pending(db)
        await db.commit()

    if attached:
        created_ids = [row["task_id"] for row in attached if row["task_created"]]
        if created_ids:
            # D2/T19 parity: a dead broker must not strand these rows `pending` (see enqueue_created).
            await enqueue_created(created_ids, label="email inbox sweep")
        return {"status": "queued", "attached": attached}

    # Nothing pending: the pre-R17 bucket task, unchanged (`_create_and_enqueue` keeps its own
    # dedupe key + budget gate); `attached: []` is additive so one shape describes both paths.
    result = await _create_and_enqueue(
        "customer_support",
        "Check inbox and reply to customer emails",
        bucket=sweep_bucket(3),
    )
    result.setdefault("attached", [])
    return result


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
            if agent_type == "finance":
                failed_payments = await recent_failed_payments(db)
                if failed_payments:
                    metadata = {"failed_payments": failed_payments}
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

    # D3: measured-spend gate (audit A1/L2-F01) — a full day creates no new sweep row.
    try:
        await assert_budget_allows(agent_type)
    except BudgetStop as exc:
        logger.warning("sweep %s: budget denied (%s)", agent_type, exc.reason)
        return {"status": "budget_denied", "reason": exc.reason}

    key = dedupe_key(agent_type, bucket)
    session_factory = get_session_factory()
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
