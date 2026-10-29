"""F-26 (wave 3) — an INTERRUPTED run must not record a NULL cost it really spent.

MEASURED DEFECT (live, 2026-10-28): `agent_runs` 311 (task 313) and 314 (task 315) were killed
mid-run when the worker process was stopped.  Run 314 had made **three** `POST
/chat/completions` calls and two `code_write` calls; both rows kept `cost_usd`, `tokens_input`,
`tokens_output` NULL.  Accounting used to be written exactly once, when the run SETTLED
(`finish_agent_run` + `_apply_run_usage`), so a process death between two provider calls erased
every measurement the run had already paid for — and the day cap sums that same column, so the
cap arithmetic under-counted by every interrupted run.

The fix has three legs, each pinned here:

  1. **The live report.**  `deepseek_runtime.invoke` hands a CUMULATIVE usage snapshot to the
     worker's sink after every finished provider call (`app.agents.run_progress`), inside a
     `finally` so the spend of a call that itself trips the cap is recorded too.
  2. **The durable write.**  The worker's sink persists that snapshot on the still-`running` row
     (`task_service.record_run_usage_progress`) — and REFUSES to touch a settled row, because a
     completed run's accounting must not change after the fact.
  3. **The interruption marker.**  When the stale-run closer (now also called on worker startup,
     because Beat is OFF) closes a stranded row, it records `usage_json.cost_scope`
     (`in_flight_partial` = measured until the kill, a LOWER BOUND; `unmeasured` = nothing was
     ever measured, NULL and never 0) plus the `interrupted` record itself.

RED legs (receipt): (a) remove the `report_usage_progress` call from the runtime → the live-report
tests fail by name; (b) drop the `status == "running"` filter from `record_run_usage_progress` →
the settled-row test fails (a completed run's cost rewritten); (c) drop the marker from the stale
closer → the interruption tests fail on `cost_scope`.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.agents import claude_cli, deepseek_runtime, run_progress
from app.config import settings
from app.core.errors import E_BUDGET_EXCEEDED, PolsiaError

SAFE_TEST_KEY = "sk-test-not-a-real-key"


class WorkerKilled(BaseException):
    """Stands in for `SIGKILL`: the worker is gone, nothing settles, nothing is caught."""


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the runtime half — the live report (no database)
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _make_inv(prompt: str = "do the thing") -> claude_cli.Invocation:
    return claude_cli.build_invocation(prompt=prompt, timeout_s=30, schema_name="test")


def _completion(content: str, *, usage: dict | None = None) -> dict:
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": usage if usage is not None else {
            "prompt_tokens": 1_000,
            "completion_tokens": 200,
            "total_tokens": 1_200,
            "prompt_cache_hit_tokens": 400,
        },
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _key_and_store(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "_resolve_api_key", lambda: SAFE_TEST_KEY)
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", 2.0)
    deepseek_runtime._reset_session_store()
    # Every test starts with NO sink: a leaked sink would attribute one run's numbers to another.
    token = run_progress.set_run_usage_sink(None)
    yield
    run_progress.reset_run_usage_sink(token)
    deepseek_runtime._reset_session_store()


def test_the_runtime_reports_the_measured_usage_of_a_live_run():
    """One provider call -> one cumulative snapshot, with the measured numbers (not estimates)."""
    seen: list[dict] = []
    token = run_progress.set_run_usage_sink(seen.append)
    try:
        result = deepseek_runtime.invoke(
            _make_inv(),
            client=_client(lambda request: httpx.Response(200, json=_completion("{}"))),
        )
    finally:
        run_progress.reset_run_usage_sink(token)

    envelope = json.loads(result.stdout)
    assert len(seen) == 1, "exactly one report per finished provider call"
    snapshot = seen[0]
    assert snapshot["provider_calls"] == 1
    assert snapshot["num_turns"] == 1
    # The snapshot carries the SAME measured token counts the settled envelope carries.
    assert snapshot["usage"]["input_tokens"] == envelope["usage"]["input_tokens"] == 1_000
    assert snapshot["usage"]["output_tokens"] == envelope["usage"]["output_tokens"] == 200
    assert snapshot["usage"]["cache_read_input_tokens"] == 400
    assert snapshot["cost_basis"] == "measured"
    assert snapshot["cost_usd"] == pytest.approx(envelope["cost_estimate_usd"])
    assert snapshot["measured_cost_usd"] == pytest.approx(envelope["cost_estimate_usd"])
    assert snapshot["model"] == "deepseek-flash"


def test_a_call_that_trips_the_cap_is_reported_before_the_refusal(monkeypatch):
    """The spend of the call that itself overruns the cap is REAL — it must reach the ledger.

    `record_call` raises `E_BUDGET_EXCEEDED` for that call; the report sits in a `finally`, so a
    capped run does not lose the very cost that capped it.
    """
    # A cap that admits the call's PROVABLE worst case but is passed by the usage the provider
    # actually reports — i.e. the post-call refusal path (the pre-call one spends nothing).
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", 0.01)
    huge_usage = {
        "prompt_tokens": 10_000_000,
        "completion_tokens": 1_000_000,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 10_000_000,
    }
    seen: list[dict] = []
    token = run_progress.set_run_usage_sink(seen.append)
    try:
        with pytest.raises(PolsiaError) as excinfo:
            deepseek_runtime.invoke(
                _make_inv(),
                client=_client(
                    lambda request: httpx.Response(200, json=_completion("{}", usage=huge_usage))
                ),
            )
    finally:
        run_progress.reset_run_usage_sink(token)

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert len(seen) == 1, "the over-cap call's own spend must be reported"
    assert seen[0]["provider_calls"] == 1
    assert seen[0]["measured_cost_usd"] > 0
    assert excinfo.value.context["measured_cost_usd"] == pytest.approx(
        seen[0]["measured_cost_usd"]
    ), "the refusal and the report must agree about what was spent"


def test_a_raising_sink_never_fails_the_run():
    """Best effort by contract: a ledger write failure must not turn a good run into a failure."""

    def broken(snapshot):  # noqa: ARG001 - the failure is the point
        raise RuntimeError("ledger unavailable")

    token = run_progress.set_run_usage_sink(broken)
    try:
        result = deepseek_runtime.invoke(
            _make_inv(),
            client=_client(lambda request: httpx.Response(200, json=_completion("{}"))),
        )
    finally:
        run_progress.reset_run_usage_sink(token)

    envelope = json.loads(result.stdout)
    assert envelope["subtype"] == "success" and envelope["is_error"] is False


def test_no_sink_is_a_no_op_and_a_reset_sink_never_reaches_the_next_run():
    """The CLI path / direct callers install nothing; a finished run's sink must not leak."""
    seen: list[dict] = []
    token = run_progress.set_run_usage_sink(seen.append)
    deepseek_runtime.invoke(
        _make_inv(), client=_client(lambda request: httpx.Response(200, json=_completion("{}")))
    )
    run_progress.reset_run_usage_sink(token)

    deepseek_runtime.invoke(
        _make_inv(), client=_client(lambda request: httpx.Response(200, json=_completion("{}")))
    )
    assert len(seen) == 1, "the reset sink is gone: a second run cannot report into it"
    assert run_progress.current_run_usage_sink() is None


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the ledger half — a running row, and the row a kill leaves behind
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _naive_utc(seconds_ago: float) -> datetime:
    """SQLite stores naive UTC wall-clock text (`timeutil.as_bound_instant`'s note)."""
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds_ago)


def _seed_run(
    qdb,
    *,
    status: str = "running",
    seconds_ago: float = 3 * 3600,
    cost_usd: Decimal | None = None,
    usage_json: dict | None = None,
) -> int:
    from app.models.task import AgentRun

    async def _make() -> int:
        async with qdb.factory() as db:
            run = AgentRun(
                agent_type="code_generation",
                run_type="task",
                status=status,
                started_at=_naive_utc(seconds_ago),
                cost_usd=cost_usd,
                usage_json=usage_json,
            )
            db.add(run)
            await db.commit()
            return int(run.id)

    return qdb.run(_make())


def _progress(qdb, run_id: int, snapshot: dict) -> bool:
    from app.services.task_service import record_run_usage_progress

    async def _write() -> bool:
        async with qdb.factory() as db:
            written = await record_run_usage_progress(db, run_id, snapshot)
            await db.commit()
            return written

    return qdb.run(_write())


def _stored_usage(qdb, run_id: int) -> dict:
    raw = qdb.query("SELECT usage_json FROM agent_runs WHERE id = ?", (run_id,))[0]["usage_json"]
    if not raw:
        return {}
    value = json.loads(raw)
    # The JSON column stores an explicit `null` for a NULL attribute; a non-object is "not
    # recorded", which is the same state as absent here.
    return value if isinstance(value, dict) else {}


def _measured_snapshot(*, cost: float = 0.0021, calls: int = 3) -> dict:
    return {
        "provider_calls": calls,
        "num_turns": calls,
        "usage": {"input_tokens": 23_112, "output_tokens": 2_129, "cache_read_input_tokens": 9_000},
        "cost_usd": cost,
        "cost_basis": "measured",
        "measured_cost_usd": cost,
        "upper_bound_usd": cost,
        "unmeasured_calls": 0,
        "model": "deepseek-flash",
        "pricing_version": "deepseek-pricing-2026-10-13",
    }


def test_a_live_run_persists_its_measured_usage_while_still_running(queue_db):
    run_id = _seed_run(queue_db)

    written = _progress(queue_db, run_id, _measured_snapshot())

    assert written is True
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    assert row["status"] == "running", "reporting must not settle the run — it is still live"
    assert row["tokens_input"] == 23_112
    assert row["tokens_output"] == 2_129
    assert row["tokens_used"] == 23_112 + 2_129, "both sides measured -> the sum is real"
    assert Decimal(str(row["cost_usd"])) == Decimal("0.0021")
    usage = _stored_usage(queue_db, run_id)
    assert usage["cost_source"] == "token_estimate", "a real cost never sits beside a NULL source"
    in_flight = usage["in_flight"]
    assert in_flight["provider_calls"] == 3
    assert in_flight["cost_basis"] == "measured"
    assert in_flight["recorded_at"], "the snapshot says WHEN it was measured"


def test_a_settled_run_is_never_rewritten_by_a_late_snapshot(queue_db):
    """A retry ladder's earlier dispatch reporting after the row closed must be refused."""
    run_id = _seed_run(queue_db, status="completed", cost_usd=Decimal("0.0050"))

    written = _progress(queue_db, run_id, _measured_snapshot(cost=99.0))

    assert written is False
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    assert Decimal(str(row["cost_usd"])) == Decimal("0.0050"), "final accounting is final"
    assert row["tokens_input"] is None
    assert _stored_usage(queue_db, run_id) == {}


def test_junk_input_cannot_fabricate_a_measurement(queue_db):
    run_id = _seed_run(queue_db)

    assert _progress(queue_db, run_id, {"usage": {"input_tokens": "many", "output_tokens": True}}) is True
    assert _progress(queue_db, run_id, {"cost_usd": "cheap"}) is True
    assert _progress(queue_db, 999_999, _measured_snapshot()) is False
    assert _progress(queue_db, run_id, ["not", "a", "dict"]) is False  # type: ignore[arg-type]

    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    assert row["tokens_input"] is None, "a string is not a token count"
    assert row["tokens_used"] is None
    assert row["cost_usd"] is None, "an unpriceable cost stays NULL, never 0"


def test_the_interruption_keeps_the_measured_cost_and_names_it_partial(queue_db):
    """The killed-worker shape: a live snapshot, then the worker dies, then the sweep closes it."""
    from celery_app.tasks import maintenance

    run_id = _seed_run(queue_db)
    _progress(queue_db, run_id, _measured_snapshot())

    result = queue_db.run(maintenance._close_stale_runs_in_session())

    assert result["closed"] == [run_id]
    assert result["cost_partial_ids"] == [run_id]
    assert result["cost_unmeasured_ids"] == []
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    assert row["status"] == "cancelled" and row["ended_at"] is not None
    assert "E_RUN_STALE" in (row["raw_log"] or "")
    assert Decimal(str(row["cost_usd"])) == Decimal("0.0021"), "the money spent is still on the ledger"
    usage = _stored_usage(queue_db, run_id)
    assert usage["cost_scope"] == "in_flight_partial"
    interrupted = usage["interrupted"]
    assert interrupted["cost_scope"] == "in_flight_partial"
    assert interrupted["usage_source"] == "in_flight"
    assert interrupted["provider_calls_persisted"] == 3
    assert interrupted["reason_code"] == "E_RUN_STALE"
    assert usage["in_flight"]["provider_calls"] == 3, "the last measured snapshot is preserved"


def test_a_legacy_interrupted_run_is_unmeasured_never_zero(queue_db):
    """The live 311/314 shape: nothing was ever measured -> NULL, and the row SAYS it is unknown."""
    from celery_app.tasks import maintenance

    run_id = _seed_run(queue_db)  # no cost, no usage_json at all

    result = queue_db.run(maintenance._close_stale_runs_in_session())

    assert result["cost_unmeasured_ids"] == [run_id]
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))[0]
    assert row["cost_usd"] is None, "an unknown cost is never written as 0"
    usage = _stored_usage(queue_db, run_id)
    assert usage["cost_scope"] == "unmeasured"
    assert usage["interrupted"]["usage_source"] == "none"


def test_the_day_cap_still_sees_the_interrupted_runs_measured_cost(queue_db, monkeypatch):
    """The arithmetic that made the defect expensive: the day sum must keep the partial cost."""
    from app.core.timeutil import today_utc, utc_day_bounds
    from celery_app.tasks import maintenance
    from celery_app.tasks._common import day_spend_by_agent

    start, _ = utc_day_bounds(today_utc())
    elapsed = (datetime.now(UTC) - start).total_seconds()
    if elapsed < 60:
        pytest.skip("first minute of the UTC day — a run cannot be both stale and inside today")
    monkeypatch.setattr(maintenance, "AGENT_RUN_STALE_AFTER_SECONDS", 30)
    run_id = _seed_run(queue_db, seconds_ago=elapsed - 5)
    _progress(queue_db, run_id, _measured_snapshot(cost=0.0021))

    async def _spend() -> float:
        async with queue_db.factory() as db:
            total, _per_agent = await day_spend_by_agent(db)
            return total

    before = queue_db.run(_spend())
    assert before == pytest.approx(0.0021), "a live run's measured spend counts for the cap"

    queue_db.run(maintenance._close_stale_runs_in_session())

    after = queue_db.run(_spend())
    assert after == pytest.approx(0.0021), "closing the row must not erase what it spent"


def test_the_budget_refusal_names_the_days_unmeasured_runs(queue_db, monkeypatch):
    """The day cap's blind spot is named in the refusal instead of being silently absent."""
    from app.core.timeutil import today_utc, utc_day_bounds
    from celery_app.tasks._common import assert_budget_allows

    start, _ = utc_day_bounds(today_utc())
    elapsed = (datetime.now(UTC) - start).total_seconds()
    if elapsed < 60:
        pytest.skip("first minute of the UTC day — cannot seed a settled run inside today")
    _seed_run(
        queue_db, status="cancelled", seconds_ago=elapsed - 5, cost_usd=Decimal("0.0100")
    )
    _seed_run(queue_db, status="cancelled", seconds_ago=elapsed - 5)  # cost unknown
    monkeypatch.setattr(settings, "agent_daily_cost_cap_usd", 0.011)

    with pytest.raises(PolsiaError) as excinfo:
        queue_db.run(assert_budget_allows("finance"))

    assert excinfo.value.code == E_BUDGET_EXCEEDED
    assert excinfo.value.context["day_runs_with_unmeasured_cost"] == 1


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the production caller — Beat is OFF, so the WORKER START is the recoverer
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_takeover_activity_never_names_the_winner_as_the_dead_claimant(queue_db):
    """Measured live 2026-10-28: the takeover summary said "expired claim on <new worker> taken
    over by <new worker>" — the `UPDATE … RETURNING` returns the NEW row, so the previous owner's
    name was never available.  The summary now states only what is known."""
    from app.services.task_service import claim_task

    task_id = queue_db.create_task(title="takeover truth", agent_type="finance")

    async def _claim_and_backdate() -> None:
        from sqlalchemy import select

        from app.models.task import Task

        async with queue_db.factory() as db:
            await claim_task(db, worker="worker-a", task_id=task_id)
            await db.commit()
        async with queue_db.factory() as db:
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
            row.heartbeat_at = stale
            row.claimed_at = stale
            await db.commit()

    async def _takeover() -> None:
        async with queue_db.factory() as db:
            await claim_task(db, worker="worker-b", task_id=task_id, takeover_stale=True)
            await db.commit()

    queue_db.run(_claim_and_backdate())
    queue_db.run(_takeover())

    rows = queue_db.query(
        "SELECT summary, detail FROM activity_log WHERE action = ?", ("task_claim_recovered",)
    )
    assert len(rows) == 1
    summary = rows[0]["summary"]
    assert "worker-b" in summary, "the worker that won is named"
    assert summary.count("worker-b") == 1, f"the winner must not be named twice: {summary!r}"
    assert "worker-a" not in summary, f"the dead worker's name is not known here: {summary!r}"
    assert '"worker": "worker-b"' in (rows[0]["detail"] or "")


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the production caller — Beat is OFF, so the WORKER START is the recoverer
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_worker_startup_hook_closes_stranded_runs_once(monkeypatch):
    from celery_app.tasks import maintenance

    calls: list[str] = []
    monkeypatch.setattr(maintenance, "_STARTUP_RECOVERY_DONE", False, raising=False)
    monkeypatch.setattr(
        maintenance, "_recover_orphaned_deliveries", lambda: _async_value({"status": "completed"})
    )
    monkeypatch.setattr(
        maintenance, "_close_stale_runs_in_session", lambda: _async_value({"closed": [7]})
    )
    real_close = maintenance._close_stale_runs_in_session

    def recording_close():
        calls.append("stale-sweep")
        return real_close()

    monkeypatch.setattr(maintenance, "_close_stale_runs_in_session", recording_close)

    maintenance._recover_deliveries_on_worker_startup()
    maintenance._recover_deliveries_on_worker_startup()  # once per process

    assert calls == ["stale-sweep"]


async def _async_value(value):
    return value


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the END-TO-END killed-worker path: the run reports, the worker dies, the sweep accounts
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_killed_worker_leaves_a_measured_cost_and_the_next_start_closes_it(
    queue_db, monkeypatch
):
    """The live 314 shape, replayed honestly: report -> BaseException (SIGKILL) -> startup sweep.

    Nothing catches a `BaseException`, so the run row is left `running` exactly as a killed
    process leaves it.  The measurement the run reported BEFORE the kill must survive, and the
    startup sweep must close the row naming the interruption as a PARTIAL cost.
    """
    from celery_app.tasks import maintenance

    task_id = queue_db.create_task(title="brand one-pager", agent_type="code_generation")
    reported: list[dict] = []

    def real_runtime_report(agent_type, task_dict, context):  # noqa: ARG001 - the spy seam
        """Stand in for the provider loop: report the live measurement (as `invoke` does)."""
        snapshot = _measured_snapshot(cost=0.0031, calls=3)
        reported.append(snapshot)
        # The sink the WORKER installed (this runs inside `asyncio.to_thread`, i.e. the run's
        # context) — this is the production path, not a stub of it.
        run_progress.report_usage_progress(snapshot)
        raise WorkerKilled("worker process stopped mid-run")

    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "run_agent_for_task", real_runtime_report)

    from celery_app.tasks._common import run_async
    from celery_app.tasks.agent_tasks import _execute_agent_task

    try:
        outcome = run_async(_execute_agent_task(int(task_id)))
    except WorkerKilled:
        pass
    else:  # pragma: no cover - the kill is the point of this test
        pytest.fail(f"the run settled instead of dying: outcome={outcome!r} reported={reported!r}")
    assert reported, "the run reported its live usage before the kill"

    runs = queue_db.query("SELECT * FROM agent_runs WHERE task_id = ?", (task_id,))
    assert len(runs) == 1
    killed = runs[0]
    assert killed["status"] == "running", "a killed worker settles nothing"
    assert Decimal(str(killed["cost_usd"])) == Decimal("0.0031"), (
        "the cost the run really spent survived the kill"
    )
    assert killed["tokens_input"] == 23_112
    usage = json.loads(killed["usage_json"]) if killed["usage_json"] else {}
    assert usage["in_flight"]["provider_calls"] == 3

    # Time passes; the next worker start runs the sweep (Beat is OFF in this deployment).
    monkeypatch.setattr(maintenance, "AGENT_RUN_STALE_AFTER_SECONDS", 0)
    closed = queue_db.run(maintenance._close_stale_runs_in_session())

    assert killed["id"] in closed["closed"]
    row = queue_db.query("SELECT * FROM agent_runs WHERE id = ?", (killed["id"],))[0]
    assert row["status"] == "cancelled"
    assert Decimal(str(row["cost_usd"])) == Decimal("0.0031")
    assert json.loads(row["usage_json"])["cost_scope"] == "in_flight_partial"
