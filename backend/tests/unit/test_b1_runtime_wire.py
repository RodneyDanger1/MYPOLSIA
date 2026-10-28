"""Wave B1 / seat D3 — the PRODUCER side of A3's meters and leases, and the broker's truth.

Three A3 receipts named gaps in these files, and one carry that cannot be proved without a broker:

  * S2-R1/R2 (F-04 / F-33) — `tool_registry._execute` hands `decide()` the run's measured
    `current_run_channel_sends()` and `_charge_metered_tool` bounds a run's paid provider calls,
    but NOTHING published either: `agent_tasks` never set the counts for a run and the runtime
    never opened a tool budget.  Every run therefore read `None`, a GitHub mutation was refused
    `cap_unknown` while the cap never charged, and `begin_run_tool_budget()` had no caller outside
    tests.  Pinned here through the REAL worker path and the REAL runtime.
  * S5-R3 (F-16) — a run that LOST its claim kept performing side effects.  The loss is discovered
    on the worker loop (the lease heartbeat) while the side effect is attempted on the run's
    THREAD, so a plain contextvar rebind cannot carry the verdict: `RUN_CANCELLED` publishes one
    MUTABLE guard before the run starts and the heartbeat latches it.  `decide()` refuses first,
    by name (`E_RUN_CANCELLED` / POLSIA-E120), for `request_side_effect` and the registry tool gate
    alike; with no cancellation the same request is allowed.
  * A2A6-03 / S3-2 — publisher confirmation and a visibility timeout longer than the 900 s claim
    lease.  There is no Redis or broker on this host, so the CONFIG VALUE is pinned (module + the
    real `app.conf` the worker reads) and the live broker leg is labelled UNRUN in the receipt.
  * Carried from A3: the replay journal and `stamp_rearmed` under a SECOND attempt.  One task is
    delivered twice: the tool executes ONCE (proved by a real file the first attempt wrote and the
    test then deletes — a second execution would re-create it), the row keeps exactly one
    `executed` record, and the `rearmed_at` marker written by the real `stamp_rearmed` survives.

What is doubled here and what is not: the ONLY doubles are the tree's own labelled seams — the
worker's `crew_factory.run_agent_for_task` (the MODEL: no provider is called from a unit test),
`tool_registry._RUNNERS["research_fetch"]` (the paid provider behind the meter under test), and the
model HTTP transport (`httpx.MockTransport`).  The code under test — the worker's publish/finally,
`decide()`, `enforce()`, `execute_tool`, `invoke()`'s tool loop, `_execute_tool_calls`, the replay
journal, `merge_actions`, `stamp_rearmed` and the real `code_write` runner — all runs for real.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime
from app.agents import policy_firewall as pf
from app.agents import side_effects as se
from app.agents import tool_registry as registry
from app.agents.policy_firewall import ActionClass
from app.config import settings
from app.core.errors import E_CLI_TIMEOUT, PolsiaError
from celery_app.tasks._common import ACTION_KEY, stamp_rearmed

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

#: A real, reversible email send: the class the firewall allows when nothing else is wrong.
EMAIL_ARGS: dict = dict(
    agent_type="email_outreach",
    action_class=ActionClass.EXTERNAL_REVERSIBLE,
    channel="email",
    payload={"action": "send_email", "subject": "b1-runtime-wire"},
)


# ── isolation: no test may inherit, or leave, run-scoped state ────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_run_scope():
    """The run-scoped contextvars are PROCESS-GLOBAL module state (the RD11/R33 leak class).

    Cleared on both sides so an order-dependent failure cannot be built here: a stray taint,
    approval token, counts snapshot or cancellation verdict from another module's test would
    otherwise decide THIS file's requests.
    """
    def _clear() -> None:
        pf.reset_run_taint()
        pf.reset_run_approval()
        pf.reset_run_channel_sends()
        pf.end_run_cancellation()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """No Redis on this host: the activity publish goes to the tree's labelled double.

    Exactly the seam `tests/unit/celery/conftest.py::quiet_redis` patches (that fixture is autouse
    only inside `tests/unit/celery/`); without it every worker-path test pays a real refused-socket
    retry ladder and opens the events breaker for its cooldown.
    """
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


def _model_client(handler) -> httpx.Client:
    """The model's HTTP seam: `httpx.MockTransport` (no socket ever leaves this process)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _completion(content: str | None, *, finish: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chat-b1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                  "prompt_cache_hit_tokens": 3},
    }


def _invocation() -> claude_cli.Invocation:
    """An Invocation from the ONLY argv builder — the runtime must accept it unchanged."""
    return claude_cli.build_invocation(
        prompt="use the tools", json_schema=SUMMARY_SCHEMA, system_prompt_static="",
        resume_session=None, timeout_s=30, schema_name="test",
    )


# ── (1) the counts producer: the worker publishes what it MEASURED, and clears it ─────────────

def test_the_worker_publishes_the_measured_channel_counts_to_the_run(
    queue_db, execute_body, monkeypatch
):
    """S2-R1/A2A8-01: the counts the gate reads are the worker's DB measurement, inside the run.

    RED before the fix: `set_run_channel_sends` had no production caller, so the run observed
    `None` — `tool_registry._execute` then handed `decide()` no count, and every configured cap
    answered `cap_unknown` (POLSIA-E114) while never charging.

    The measurement itself is REAL: one published twitter row counts, a draft in the same window
    must not (the A-T5 "counting drafts" trap), and a channel with no local rows is a MEASURED
    zero rather than unknown.
    """
    import app.agents.crew_factory as crew_factory
    from app.models.social import SocialPost

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(SocialPost(platform="twitter", content="really published",
                              status="published", published_at=datetime.now(UTC)))
            db.add(SocialPost(platform="twitter", content="still a draft", status="draft"))
            await db.commit()

    queue_db.run(_seed())

    seen: dict = {}

    def _run(agent_type, task_dict, context):
        # Read INSIDE the run, i.e. in the context the agent's tool calls inherit.
        seen["during"] = pf.current_run_channel_sends()
        seen["context_keys"] = context.get("channel_sends")
        return {"summary": "counts observed"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _run)
    task_id = queue_db.create_task(title="b1 counts", agent_type="social_media")

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    during = seen["during"]
    assert during is not None, "the run must observe published counts, not None"
    assert during == seen["context_keys"], (
        "the run must read EXACTLY the snapshot the worker measured for its context"
    )
    assert during["twitter"] == {"today": 1, "hour": 1}, (
        f"the published count must be the DB's measurement (published-only): {during['twitter']}"
    )
    assert during["linkedin"] == {"today": 0, "hour": 0}, (
        "a channel with a local send table and no rows is a MEASURED zero, never unknown"
    )

    # The `finally` clears it: the next run in this worker context must start from nothing.
    assert pf.current_run_channel_sends() is None, (
        "one run's counts must not leak into the next run in this worker process"
    )


# ── (2) the tool budget: the RUNTIME opens it, charges it, and closes it ─────────────────────

def test_the_runtime_opens_charges_and_closes_the_per_run_tool_budget(monkeypatch):
    """S2-R2/F-33: `begin_run_tool_budget()` had NO production caller.

    RED before the fix: 11 metered calls in one run were all permitted (`_RUN_METERED` stayed
    None, so `_charge_metered_tool` returned early) and nothing was charged.  Now the runtime's
    tool loop opens ONE budget per run: the 11th paid provider call is refused by name, and a
    SECOND run gets a fresh budget (the `finally` closed the first).
    """
    calls: list[str] = []

    def _paid_runner(*, agent_type: str, arguments: dict) -> dict:
        calls.append(str(arguments.get("url")))
        return {"ok": True, "tool": "research_fetch", "result": {"url": arguments.get("url")}}

    monkeypatch.setitem(registry._RUNNERS, "research_fetch", _paid_runner)
    # A pre-existing (outer) budget would make the run's own open/close invisible: pin the
    # starting state, then prove the run leaves it exactly as it found it.
    assert registry._RUN_METERED.get() is None

    tool_calls = [
        {"id": f"call-{index}", "type": "function",
         "function": {"name": "research_fetch",
                      "arguments": json.dumps({"url": f"https://example.test/{index}"})}}
        for index in range(registry.METERED_CALLS_PER_RUN + 1)
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        """Turn 1 asks for N+1 paid calls; turn 2 answers — one run, one provider reply each."""
        body = json.loads(request.content)
        if any(message.get("role") == "tool" for message in body["messages"]):
            return httpx.Response(200, json=_completion('{"summary": "done"}', finish="stop"))
        return httpx.Response(200, json=_completion(None, finish="tool_calls",
                                                    tool_calls=tool_calls))

    for run_number in (1, 2):
        calls.clear()
        result = deepseek_runtime.invoke(
            _invocation(), client=_model_client(_handler), api_key="b1-test-key",
            tools=[{"type": "function",
                    "function": {"name": "research_fetch", "parameters": {}}}],
            agent_type="competitor_research",
        )
        envelope = json.loads(result.stdout)
        tool_messages = [record for record in envelope[ACTION_KEY]]
        assert len(tool_messages) == registry.METERED_CALLS_PER_RUN + 1

        refusals = [record for record in tool_messages
                    if "E_BUDGET_EXCEEDED" in str(record.get("result"))]
        assert len(refusals) == 1, (
            f"run {run_number}: exactly the call past the per-run cap must be refused; "
            f"got {len(refusals)}"
        )
        assert "E_BUDGET_EXCEEDED" in str(tool_messages[-1].get("result")), (
            "the REFUSED call is the last one"
        )
        assert len(calls) == registry.METERED_CALLS_PER_RUN, (
            "the refused call must never reach the paid provider"
        )
        assert registry._RUN_METERED.get() is None, (
            f"run {run_number}: the runtime must CLOSE the budget it opened — otherwise the next "
            "run in this worker context is metered against the previous run's spend"
        )


# ── (3) F-16: a lease-lost run refuses its next side effect, by name ─────────────────────────

def test_the_lease_verdict_crosses_into_the_context_copy_the_run_executes_in():
    """The MECHANISM F-16 needs: the loss is latched in another context and read in this one.

    Why this test exists: `asyncio.create_task` and `asyncio.to_thread` COPY the context, so
    rebinding a contextvar on the heartbeat task would never reach the agent thread.  The guard is
    therefore one MUTABLE object published before the run.  Both halves are real here — a genuine
    `contextvars.copy_context()` (exactly what `create_task`/`to_thread` do) and a real thread.
    """
    guard = pf.begin_run_cancellation()
    assert pf.current_run_cancellation() is guard
    assert pf.run_cancelled_reason() == "", "a fresh run is not cancelled"

    copied = contextvars.copy_context()
    failures: list[str] = []

    def _heartbeat_in_its_own_context() -> None:
        try:
            assert copied.run(pf.current_run_cancellation) is guard, (
                "the copied context must hold the SAME guard object, not a copy of it"
            )
            # The worker's own verb, called from the task that owns the heartbeat.
            assert copied.run(pf.cancel_run, "claim lease lost (E_TASK_CLAIMED)") is True
        except AssertionError as exc:  # pragma: no cover - only on a broken mechanism
            failures.append(str(exc))

    thread = threading.Thread(target=_heartbeat_in_its_own_context)
    thread.start()
    thread.join(timeout=30)
    assert failures == []
    assert pf.run_cancelled_reason() == "claim lease lost (E_TASK_CLAIMED)", (
        "the verdict latched in the heartbeat's context must be visible to the run"
    )


def test_a_cancelled_run_is_refused_by_name_and_the_same_request_is_allowed_without_it(
    monkeypatch,
):
    """F-16: `request_side_effect` AND the registry tool gate refuse; no cancel -> allowed.

    RED before the fix: with the claim lost, `request_side_effect` returned a normal outcome and
    `execute_tool` ran the tool — a lease-lost run kept sending.

    The happy path is asserted FIRST and on the same request, so the refusal cannot be an artefact
    of the request itself (sandbox off, measured zero sends, a reversible channel).
    """
    monkeypatch.setattr(settings, "sandbox_mode", False)

    # The worker's publish step (agent_tasks step 2b): one guard for this run.
    pf.begin_run_cancellation()

    allowed = se.request_side_effect(sends_today=0, sends_this_hour=0, **EMAIL_ARGS)
    assert allowed.allowed is True, f"control: the request is permitted ({allowed.reason})"

    assert pf.cancel_run("claim lease lost (E_TASK_CLAIMED)") is True

    with pytest.raises(PolsiaError) as excinfo:
        se.request_side_effect(sends_today=0, sends_this_hour=0, **EMAIL_ARGS)
    refusal = excinfo.value
    assert refusal.code == pf.E_RUN_CANCELLED, (
        f"a lease-lost run must refuse by its OWN code, got {refusal.code}"
    )
    assert "lost its claim" in refusal.message
    assert "E_TASK_CLAIMED" in refusal.message, "the refusal must name the lease error"
    assert refusal.how_to_fix, "a named refusal always says how to proceed"
    assert refusal.context["policy_code"] == "POLSIA-E120"
    assert refusal.context["cancelled"] is True

    # The SAME gate governs the model's tool path (`tool_registry._execute` -> `decide()`).
    tool_payload = json.loads(registry.execute_tool(
        "signals_read", {"topics": ["ai"]}, agent_type="social_media"))
    assert tool_payload["ok"] is False, "the registry tool gate must refuse a cancelled run"
    assert tool_payload["error"]["context"]["policy_code"] == "POLSIA-E120"
    assert "lost its claim" in tool_payload["error"]["message"]
    assert tool_payload["error"]["how_to_fix"], "a named refusal always says how to proceed"

    # Per-run isolation: a NEW run's guard starts clean (the worker publishes one per run).
    pf.end_run_cancellation()
    pf.begin_run_cancellation()
    assert pf.run_cancelled_reason() == ""
    assert se.request_side_effect(
        sends_today=0, sends_this_hour=0, **EMAIL_ARGS).allowed is True


def test_the_worker_cancels_the_run_when_the_claim_lease_is_lost(
    queue_db, execute_body, steal_claim, monkeypatch
):
    """F-16 end to end, through the REAL worker: the loss reaches the side-effect gate.

    The row is taken over mid-run by a real second claim (release + claim through the service).
    The heartbeat fires only after its 5 s floor (`interval = max(5.0, lease/3)`), so the work is
    slowed past it and the takeover lands at t~0.5 s.  When the run wakes it must find its guard
    LATCHED and its next side effect refused — not sandbox-drafted, not sent.

    RED before the fix: the guard did not exist, `request_side_effect` returned a sandbox-labelled
    outcome (SANDBOX_MODE is on by default here) and the run looked like it kept working.  After
    the fix the refusal is the CANCELLATION even in sandbox mode, because `decide()` checks it
    before every other leg.
    """
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(settings, "task_claim_lease_seconds", 3)
    observed: dict = {}

    def _run(agent_type, task_dict, context):
        time.sleep(6.5)  # past the heartbeat's 5 s floor: the loss is discovered mid-run
        observed["reason"] = pf.run_cancelled_reason()
        observed["counts"] = pf.current_run_channel_sends()
        try:
            se.request_side_effect(
                agent_type="social_media",
                action_class=ActionClass.EXTERNAL_REVERSIBLE,
                channel="twitter",
                payload={"action": "tweet", "text": "must never be sent"},
                sends_today=0,
                sends_this_hour=0,
                sandbox_mode=False,   # the strictest leg: a real send was permitted here
            )
        except PolsiaError as exc:
            observed["refusal"] = exc.code
            observed["message"] = exc.message
        else:  # pragma: no cover - the defect this test exists for
            observed["refusal"] = ""
        return {"summary": "abandoned run"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _run)
    task_id = queue_db.create_task(title="b1 lease loss", agent_type="social_media")
    takeover = steal_claim(task_id, after_s=0.5, thief="thief-worker-b1")

    outcome = execute_body(task_id)
    takeover.result(timeout=30)

    assert observed["reason"] == "claim lease lost (E_TASK_CLAIMED)", observed
    assert observed["counts"] is not None, (
        "the counts stay published for the run's lifetime (only the `finally` clears them)"
    )
    assert observed["refusal"] == pf.E_RUN_CANCELLED, (
        f"the lease-lost run's side effect must be refused by name: {observed}"
    )
    assert "lost its claim" in observed["message"]

    # The existing lease-lost contract is unchanged: the abandoned run is a FAILURE that must not
    # stomp the new owner's row.
    assert outcome["status"] == "failed"
    assert "claim lease lost" in (outcome["error"] or "")
    row = queue_db.row(task_id)
    assert row["claimed_by"] == "thief-worker-b1", "the loser must not clear the new owner's claim"
    assert row["result_summary"] is None, "the abandoned result must not be written"
    assert pf.run_cancelled_reason() == "", "the worker's `finally` must clear the guard"


# ── (4) the replay journal + stamp_rearmed under a SECOND attempt ────────────────────────────

def test_two_attempts_execute_the_tool_once_and_keep_one_record(
    queue_db, eager_delivery, monkeypatch, tmp_path
):
    """A3's replay journal, verified end to end through the worker path.

    TWO real deliveries of the SAME task: attempt 1 executes `code_write` (a real file appears on
    disk) and then fails retryably; attempt 2 replays the recorded answer and must not execute the
    call again.  The proof that a second execution did NOT happen is physical, not inferred: the
    test deletes the file between attempts, so a re-execution would re-create it.

    The row must carry exactly ONE action record, `executed` (not over-written by the replay), and
    the `rearmed_at` marker the REAL `stamp_rearmed` wrote must survive both the run's metadata
    merge and the second attempt — one writer must not stomp the other.

    RED leg: pass `replay=None` (or drop `prior_actions` from the task dict) and the second attempt
    executes the tool again -> the file is back and the run records a second execution.
    """
    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(settings, "code_workspace_dir", str(tmp_path))
    proof = tmp_path / "b1-proof.txt"
    seen: dict = {"attempts": [], "prior_sizes": [], "tool_text": {}}

    def _model(agent_type, task_dict, context):
        attempt = int(task_dict.get("attempt") or 0)
        prior = task_dict.get("prior_actions") or []
        seen["attempts"].append(attempt)
        seen["prior_sizes"].append(len(prior))
        journal: list[dict] = []
        messages, _reached = deepseek_runtime._execute_tool_calls(
            [{"id": f"call-{attempt}", "type": "function",
              "function": {"name": "code_write",
                           "arguments": json.dumps({"path": "b1-proof.txt",
                                                    "content": "alpha\n"})}}],
            agent_type=agent_type,
            url="test://b1-replay",
            attempt=attempt,
            replay=deepseek_runtime.replay_index(prior),
            journal=journal,
        )
        seen["tool_text"][attempt] = messages[0]["content"]
        if attempt == 1:
            # A SECOND execution would re-create this file on attempt 2.
            proof.unlink()
            raise PolsiaError(E_CLI_TIMEOUT, "b1: test-only transport failure after the tool ran",
                              context={ACTION_KEY: journal})
        return {"summary": "attempt 2 replayed the recorded call"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _model)
    task_id = queue_db.create_task(title="b1 replay", agent_type="code_generation")

    # The REAL marker writer, before the first delivery (a sweep that owed a re-dispatch).
    async def _stamp() -> None:
        async with queue_db.factory() as db:
            await stamp_rearmed(db, [task_id], reason="b1: undelivered tick")
            await db.commit()

    queue_db.run(_stamp())

    result = eager_delivery(task_id)

    assert result.state == "SUCCESS", result.state
    assert seen["attempts"] == [1, 2], "the retry ladder made exactly two attempts"
    assert seen["prior_sizes"] == [0, 1], (
        "attempt 1 had no record; attempt 2 replayed the one attempt 1 wrote"
    )
    assert not proof.exists(), (
        "the tool must have run ONCE: the file deleted after attempt 1 must NOT reappear"
    )
    assert seen["tool_text"][2] == seen["tool_text"][1], (
        "attempt 2's tool answer must be the RECORDED result, replayed — not a fresh write"
    )

    metadata = json.loads(queue_db.row(task_id)["metadata"])
    actions = metadata[ACTION_KEY]
    assert len(actions) == 1, f"one effect keeps one record, however often it is replayed: {actions}"
    record = actions[0]
    assert record["status"] == "executed", "the effect really happened, exactly once"
    assert record["attempt"] == 1, "the record names the attempt that PERFORMED the call"
    assert record["call_key"].startswith("code_write|")
    assert json.loads(record["result"])["result"]["path"] == "b1-proof.txt"

    assert metadata["rearmed_at"], (
        "stamp_rearmed's marker must survive the run's metadata merge and the second attempt"
    )
    # WAVE-2 F-22/F-23 RE-PIN (2026-10-28): a retryable failure now stamps its OWN retry marker
    # (`retry_requeued`) so an accept-then-lost retry is reconciled; the sweep's `rearmed_at`
    # still survives (asserted above). OLD value pre-wave-2: "b1: undelivered tick"; NEW: the
    # most recent re-arm action name, "retry_requeued".
    assert metadata["rearm_reason"] == "retry_requeued"


# ── (5) the broker's truth: publisher confirmation + the visibility timeout ──────────────────

def test_the_broker_is_configured_for_confirmation_and_a_lease_safe_visibility_timeout():
    """A2A6-03 / S3-2 — the config VALUE, not the live broker.

    LIVE LEG: UNRUN.  This host has no Redis and no broker, so nothing here proves a message was
    confirmed or redelivered; the config values Celery reads are pinned instead, on the module AND
    on the real `app.conf` object the worker loads.
    Settle it with a broker up:
        docker compose up -d redis
        docker compose run --rm worker python -c "from celery_app.worker import app; \\
            print(app.conf.broker_transport_options)"
        docker compose up -d worker && docker compose logs -f worker   # a publish + its ack
    """
    from celery_app import celery_config
    from celery_app.worker import app

    # 1. publisher confirmation — the module value AND the value that reaches the app.
    assert celery_config.confirm_publish is True
    assert app.conf.confirm_publish is True
    assert app.conf.broker_transport_options["confirm_publish"] is True, (
        "the transport-options placement is the one kombu reads (pyamqp splats it into "
        "amqp.Connection), so the key must be THERE, not only at module level"
    )

    # 2. the visibility timeout must outlast the claim lease (the brief's requirement) and the
    #    task's own hard limit (below it, every long task is redelivered mid-run).
    visibility_timeout = app.conf.broker_transport_options["visibility_timeout"]
    assert visibility_timeout == celery_config.visibility_timeout, (
        "config_from_object must carry the value the module declares"
    )
    assert visibility_timeout > settings.task_claim_lease_seconds, (
        f"visibility_timeout={visibility_timeout}s must exceed the "
        f"{settings.task_claim_lease_seconds}s claim lease"
    )
    assert visibility_timeout >= settings.celery_task_time_limit, (
        f"visibility_timeout={visibility_timeout}s must not be shorter than the "
        f"{settings.celery_task_time_limit}s task time limit"
    )
    assert visibility_timeout == max(
        2 * settings.task_claim_lease_seconds, settings.celery_task_time_limit + 600
    ), "the declared derivation changed — re-read the block comment before retuning it"

    # 3. the retry policy a confirmed publish relies on.
    assert app.conf.task_publish_retry is True
    assert app.conf.task_publish_retry_policy["max_retries"] >= 1

    # 4. WHY the confirmation key is not theatre on AMQP: the installed stack really accepts it.
    import amqp

    assert "confirm_publish" in amqp.Connection.__init__.__code__.co_varnames, (
        "amqp.Connection no longer takes confirm_publish — the pyamqp transport would silently "
        "ignore the transport option and this config would be inert"
    )
