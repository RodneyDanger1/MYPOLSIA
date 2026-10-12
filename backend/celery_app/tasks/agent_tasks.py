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
    E_RATE_LIMITED,
    E_REDIS,
    E_TASK_CLAIMED,
    E_TASK_NOT_RUNNABLE,
    PolsiaError,
)
from celery_app.tasks._common import create_task_deduped, dedupe_key, run_async, sweep_bucket
from celery_app.worker import app

logger = logging.getLogger(__name__)

# Failures worth another attempt: the run never produced a result and the cause is
# infrastructure (CLI transport, provider, database).  Everything else — a bad prompt, an
# unknown agent, a sandbox refusal, a budget stop, a validation error — is terminal by
# definition (SPEC-AGENTS D-07: "backoff for infrastructure errors only; business
# failures stay terminal").
RETRYABLE_CODES = frozenset(
    {
        E_CLI_TIMEOUT,
        E_CLI_EXIT,
        E_CLI_JSON,
        E_AGENT_BAD_OUTPUT,
        E_INTEGRATION,
        E_RATE_LIMITED,
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
    elif tokens_input is not None or tokens_output is not None:
        run_row.tokens_used = int(tokens_input or 0) + int(tokens_output or 0)
    if session_id:
        run_row.cli_session_id = str(session_id)[:120]
    if cost is not None:
        try:
            run_row.cost_usd = float(cost)
        except (TypeError, ValueError):  # pragma: no cover - defensive, logged not silent
            logger.warning("non-numeric cost in agent result: %r", cost)

    recorded: dict[str, Any] = dict(envelope_usage)
    if usage:
        recorded.setdefault("usage", usage)
    for key in RUN_META_KEYS:
        if key != "usage_json" and key in result:
            recorded[key] = result[key]
    if recorded:
        run_row.usage_json = recorded


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
            task_dict = {"id": task.id, "title": task.title, "description": task.description}
            agent_type = task.agent_type
    except PolsiaError as exc:
        if exc.code in _SKIP_CLAIM_CODES:
            logger.info("run_agent_task(%s): skipped — %s", task_id, exc)
            return {"status": "skipped", "task_id": task_id, "reason": exc.code}
        if exc.code == E_NOT_FOUND:
            logger.warning("run_agent_task(%s): task row missing (stale message)", task_id)
            return {"status": "skipped", "task_id": task_id, "reason": exc.code}
        raise

    # ── 2. context + run-ledger row (own short transaction) ──────────────────
    async with session_factory() as db:
        context = await get_full_context(db)
        run = await create_agent_run(db, agent_type, task_id=task_id, input_context=context)
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
    try:
        # The agent's CLI call is blocking; run it off the loop so the heartbeat keeps
        # beating (the upload blocked the whole event loop for the CLI's lifetime, D-11).
        result = await asyncio.to_thread(run_agent_for_task, agent_type, task_dict, context)
    except Exception as exc:  # noqa: BLE001 — classified, persisted and (maybe) retried
        failure = exc
    finally:
        stop_heartbeat.set()
        await heartbeat
    duration = round(time.monotonic() - started, 2)

    error = None if failure is None else f"{type(failure).__name__}: {failure}"
    if lease_lost.is_set():
        error = "; ".join(x for x in (error, "claim lease lost during the run") if x)

    # ── 4. persist: run accounting, task outcome, activity ───────────────────
    status = "failed" if error else "completed"
    summary = result.get("summary") if isinstance(result, dict) else None
    retryable = is_retryable(failure) if failure is not None else False
    attempts = 0
    requeued = False
    async with session_factory() as db:
        run_row = await finish_agent_run(
            db,
            run_id,
            status,
            output=result if status == "completed" else None,
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
            task_row = await update_task_status(
                db,
                task_id,
                status,
                result_summary=summary if status == "completed" else None,
                error_message=error,
            )
            if task_row is not None:
                task_row.claimed_by = None
                task_row.claimed_at = None
                task_row.heartbeat_at = None
                if status == "failed":
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
                        task_row.status = "pending"
                        requeued = True
            await log_activity(
                db,
                agent_type=agent_type,
                action="task_completed" if status == "completed" else "task_failed",
                summary=summary or error or "No output",
                level="success" if status == "completed" else "error",
                detail={"task_id": task_id, "duration_secs": duration, "requeued": requeued},
            )
        await db.commit()

    return {
        "status": status,
        "task_id": task_id,
        "agent_type": agent_type,
        "duration_secs": duration,
        "attempts": attempts,
        "error": error,
        "retryable": retryable,
        "requeued": requeued,  # True when this failure put the row back in the queue for its retry
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
    return run_async(
        _create_and_enqueue(
            "customer_support",
            "Check inbox and reply to customer emails",
            bucket=sweep_bucket(3),
        )
    )


@app.task(name="celery_app.tasks.agent_tasks.run_ads_stripe_sync")
def run_ads_stripe_sync() -> dict[str, Any]:
    """Every 6h — ad metrics sync and the Stripe failed-payment check (independent)."""
    return run_async(_ads_stripe_sync())


async def _ads_stripe_sync() -> dict[str, Any]:
    from app.core.database import get_session_factory

    bucket = sweep_bucket(6)
    plan = (
        ("ads_management", "Sync ad metrics from Google and Meta"),
        ("finance", "Check for failed Stripe payments and update revenue snapshot"),
    )
    session_factory = get_session_factory()
    created: list[tuple[str, int]] = []
    duplicates: list[str] = []
    async with session_factory() as db:
        for agent_type, title in plan:
            key = dedupe_key(agent_type, bucket)
            task = await create_task_deduped(
                db,
                title=title,
                agent_type=agent_type,
                dedupe_key=key,
                source="scheduler",
                priority=3,
            )
            if task is None:
                duplicates.append(key)
            else:
                created.append((agent_type, task.id))
        await db.commit()

    queued = {agent_type: task_id for agent_type, task_id in created}
    for _, task_id in created:
        run_agent_task.delay(task_id)  # broker, never .apply()
    return {"status": "queued" if created else "duplicate", "queued": queued, "duplicates": duplicates}


async def _create_and_enqueue(
    agent_type: str,
    title: str,
    *,
    bucket: str,
    priority: int = 3,
    description: str | None = None,
) -> dict[str, Any]:
    """Create today's sweep task once (dedupe key) and queue it on the broker."""
    from app.core.database import get_session_factory

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

    run_agent_task.delay(task_id)  # broker, never .apply() (D-03)
    return {"status": "queued", "task_id": task_id, "dedupe_key": key}
