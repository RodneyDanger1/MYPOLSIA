"""F-W4-TM-1 (S2, wave 4 -> wave 5) — a run that made ZERO model provider calls settles $0.0000.

MEASURED DEFECT (live, 2026-10-28): run 318 (task 324, sentinel's approval-replay drill) FINISHED
with zero model provider calls — the replay performs the STORED approved actions and never asks
the model — so nothing was measured and `agent_runs.cost_usd` stayed NULL.  `finance_service.
day_spend_truth` cannot tell "no provider call -> $0.0000" from "a call happened and its cost was
lost", so it reported the whole UTC day as UNMEASURED and `planner_service._gate_planner_budget`
refused every paid planner dispatch for the rest of the day.  The refusal was SAFE but
operationally wrong: one model-free run blocked the cap.

THE RULE PINNED HERE (and nothing weaker):

  * a run whose own PATH DECLARES zero model provider calls settles a measured `$0.0000`, stamped
    `usage_json.cost_source="no_provider_calls"` + the declaration record (never NULL);
  * ABSENCE of a declaration is NOT zero: a run whose provider call happened and whose usage was
    lost keeps its NULL cost, and the day cap keeps refusing it BY NAME (F-19/F-20's rule);
  * a real measurement always wins — the settlement only fills a cost that is still unknown;
  * an IN-FLIGHT run still does not refuse the gate (it is counted, not guessed into the total);
  * junk declarations (the string "0", True, a negative) are UNKNOWN and settle nothing.

RED leg (receipt `raw/w5/RED-A.txt`): disconnecting `_settle_no_provider_calls` from the settle
path reddens tests 1 and 2 by name; removing the replay's `provider_calls: 0` declaration reddens
test 1 the same way (the settlement then has nothing to act on).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents.policy_firewall import fingerprint, reset_run_approval
from app.core.errors import E_POLICY_DENIED, MeridianError
from app.models.task import AgentRun
from celery_app.tasks import agent_tasks

BODY = "the approved body, never re-generated"


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_run_context():
    reset_run_approval()
    yield
    reset_run_approval()


# ── the parked row: a fingerprint, its stored payload (the replay path's input) ────────────────


def _record(to: str = "w5-drill@example.invalid") -> dict:
    payload = {"action": "send", "subject": "W5 zero-call drill", "body": BODY, "to": to}
    return {
        "agent_type": "email_outreach",
        "action": "send",
        "ref": to,
        "channel": "email",
        "class": "external_reversible",
        "value_usd": 0.0,
        "payload": payload,
        "item": {"prospect_email": to, "subject": "W5 zero-call drill", "body": BODY},
    }


def _parked_metadata() -> dict:
    record = _record()
    fp = fingerprint("external_reversible", "email", record["payload"])
    return {
        "approval_required": [
            {"action": "send", "ref": record["ref"], "channel": "email", "fingerprint": fp,
             "reason": "context tainted"}
        ],
        agent_tasks.APPROVED_PAYLOAD_KEY: {fp: record},
    }


def _create_parked_task(queue_db, metadata: dict) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db,
                title="W5 zero-call settlement",
                agent_type="email_outreach",
                source="scheduler",
                status="pending",
                metadata=metadata,
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _runs(queue_db) -> list[dict]:
    return queue_db.query("SELECT * FROM agent_runs ORDER BY id")


def _json_column(raw):
    """A JSON column as a dict; SQLAlchemy writes JSON `null` for an absent value."""
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _usage(row: dict) -> dict:
    return _json_column(row.get("usage_json"))


def _output(row: dict) -> dict:
    return _json_column(row.get("output"))


def _install_model_spy(monkeypatch) -> list[dict]:
    """Record any `run_agent_for_task` call: the replay must never ask the model."""
    import app.agents.crew_factory as crew_factory

    calls: list[dict] = []

    def _spy(agent_type, task_dict, context):  # noqa: ANN001
        calls.append({"agent_type": agent_type, "task_id": (task_dict or {}).get("id")})
        return {"summary": "the model was asked instead of replaying the stored payload"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return calls


# ── 1. THE FIX: a zero-call replay settles a MEASURED zero, labelled ──────────────────────────


def test_a_zero_provider_call_replay_settles_a_measured_zero_not_null(queue_db, monkeypatch):
    """RED (pre-fix): run 318's shape — completed, `cost_usd` NULL, `usage_json` NULL.

    The parked row with NO token replays exactly as run 318 did: the token covers nothing, every
    item is `gate_refused`, nothing is sent, and the MODEL IS NEVER ASKED.  The run must therefore
    settle a measured 0 with its own provenance — not the NULL that made the day unmeasurable.
    """
    model_calls = _install_model_spy(monkeypatch)
    task_id = _create_parked_task(queue_db, _parked_metadata())

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    # The row is re-parked (nothing was sent and nothing may be called): the RUN still finished.
    assert outcome["status"] in ("awaiting_approval", "completed"), (
        outcome,
        queue_db.row(task_id)["error_message"],
    )
    assert model_calls == [], "the replay asked the model instead of performing the stored payload"
    runs = _runs(queue_db)
    assert len(runs) == 1, runs
    run = runs[0]
    assert run["status"] == "completed"
    assert run["cost_usd"] is not None, (
        "a finished zero-provider-call run settled a NULL cost — the F-W4-TM-1 defect: the whole "
        "UTC day becomes UNMEASURED and paid planner dispatch refuses"
    )
    assert Decimal(str(run["cost_usd"])) == Decimal("0.0000"), run["cost_usd"]

    usage = _usage(run)
    assert usage["cost_source"] == "no_provider_calls", usage
    assert usage["provider_calls"] == 0, usage
    settlement = usage["no_provider_calls"]
    assert settlement["declared_by"] == "approval_replay", settlement
    assert settlement["settled_by"] == "celery_app.tasks.agent_tasks", settlement
    assert settlement["recorded_at"], "a derived zero must record WHEN it was settled"
    assert "MEASURED $0.0000" in settlement["note"], settlement

    # Tokens stay NULL: no provider reported any, and a `0` would claim a measurement that never
    # happened (the cost is the budget-gate quantity; the tokens are provider reports).
    assert run["tokens_input"] is None and run["tokens_output"] is None, run
    assert run["tokens_used"] is None, run

    # The PRODUCER's declaration rides the completed run's stored output.
    assert _output(run)["provider_calls"] == 0, _output(run)
    assert _output(run)["provider_calls_source"] == "approval_replay"


# ── 2. the day truth: the settled zero is MEASURED, so the planner gate can run ────────────────


@pytest.mark.asyncio
async def test_a_settled_zero_makes_the_day_measurable_and_the_gate_allows(async_db_session):
    """RED (pre-fix): the zero-call row was NULL -> `day_spend_truth.total is None` -> refusal.

    The gate is the consumer that broke live: with the zero-call row settled as a MEASURED 0 the
    day sums, and a normal dispatch is authorized (nothing was spent, nothing may be refused).
    """
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    async_db_session.add(
        AgentRun(
            agent_type="email_outreach",
            status="completed",
            cost_usd=Decimal("0.0000"),          # the settled measured zero
            started_at=started,
            ended_at=started + timedelta(minutes=1),
            usage_json={"cost_source": "no_provider_calls", "provider_calls": 0},
        )
    )
    await async_db_session.commit()

    truth = await day_spend_truth(async_db_session)
    assert truth.total == Decimal("0.0000"), truth
    assert truth.finished_unpriced == 0, "a settled zero is NOT an unpriced run"
    assert truth.measured_runs == 1

    await planner_service._gate_planner_budget(
        async_db_session, "company_planner", step="propose"
    )  # must not raise


@pytest.mark.asyncio
async def test_a_genuine_unpriced_run_beside_a_settled_zero_still_refuses_by_name(async_db_session):
    """CONTROL: the fix must not weaken the F-19 rule — a real lost measurement still refuses."""
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    async_db_session.add_all(
        [
            AgentRun(
                agent_type="email_outreach",
                status="completed",
                cost_usd=Decimal("0.0000"),
                started_at=started,
                ended_at=started + timedelta(minutes=1),
                usage_json={"cost_source": "no_provider_calls", "provider_calls": 0},
            ),
            # A provider call DID happen; its usage never reached the ledger.  No declaration.
            AgentRun(
                agent_type="social_media",
                status="completed",
                cost_usd=None,
                started_at=started,
                ended_at=started + timedelta(minutes=1),
            ),
        ]
    )
    await async_db_session.commit()

    truth = await day_spend_truth(async_db_session)
    assert truth.total is None, "a day with one unmeasured run is still UNMEASURED"
    assert truth.finished_unpriced == 1

    with pytest.raises(MeridianError) as excinfo:
        await planner_service._gate_planner_budget(
            async_db_session, "company_planner", step="propose"
        )
    assert excinfo.value.code == "E_BUDGET_EXCEEDED"
    assert "UNMEASURED" in excinfo.value.message


@pytest.mark.asyncio
async def test_an_in_flight_run_beside_a_settled_zero_still_allows(async_db_session):
    """CONTROL: an open run neither refuses the gate nor adds a fabricated number (wave-4 rule)."""
    from app.services import planner_service
    from app.services.finance_service import day_spend_truth

    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    async_db_session.add_all(
        [
            AgentRun(
                agent_type="email_outreach",
                status="completed",
                cost_usd=Decimal("0.0000"),
                started_at=started,
                ended_at=started + timedelta(minutes=1),
            ),
            AgentRun(
                agent_type="social_media",
                status="running",
                cost_usd=None,
                started_at=started,
            ),
        ]
    )
    await async_db_session.commit()

    truth = await day_spend_truth(async_db_session)
    assert truth.total == Decimal("0.0000")
    assert truth.in_flight == 1

    await planner_service._gate_planner_budget(
        async_db_session, "company_planner", step="propose"
    )  # must not raise


# ── 3. the settlement never overwrites a real measurement ─────────────────────────────────────


def test_a_measured_run_is_never_overwritten_by_the_zero_settlement(queue_db, monkeypatch):
    """CONTROL: `_apply_run_usage` runs FIRST and a real cost always wins."""
    import app.agents.crew_factory as crew_factory

    measured = {
        "summary": "a run that really called the model",
        "cost_usd": 0.0042,
        "tokens_input": 1200,
        "tokens_output": 300,
        "tokens_used": 1500,
        "usage_json": {"cost_source": "token_estimate", "pricing_version": "test-pin"},
        # A pathological producer that BOTH measured a cost and declared zero calls: the
        # measurement must win (never turn real spend into a fabricated zero).
        "provider_calls": 0,
        "provider_calls_source": "contradictory_test_double",
    }
    monkeypatch.setattr(
        crew_factory, "run_agent_for_task", lambda *a, **k: {**measured}
    )
    task_id = queue_db.create_task(title="measured run", agent_type="social_media")

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "completed", outcome
    run = _runs(queue_db)[0]
    assert Decimal(str(run["cost_usd"])) == Decimal("0.0042"), run["cost_usd"]
    usage = _usage(run)
    assert usage.get("cost_source") != "no_provider_calls", usage
    assert "no_provider_calls" not in usage, usage


# ── 4. absence of a declaration keeps today's fail-safe behaviour ──────────────────────────────


def test_a_failed_run_with_no_declaration_keeps_its_cost_unknown(queue_db, monkeypatch):
    """CONTROL (F-19/F-20): a failure may have spent on a call whose usage was lost -> NULL.

    This is the run the day cap MUST keep refusing: turning it into a measured 0 would be the
    mirror-image lie (a fabricated cheap day).
    """
    import app.agents.crew_factory as crew_factory

    def _boom(*args, **kwargs):
        raise RuntimeError("the provider call died before its usage reached us")

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _boom)
    task_id = queue_db.create_task(title="unmeasured failure", agent_type="social_media")

    queue_db.run(agent_tasks._execute_agent_task(task_id))

    run = _runs(queue_db)[0]
    assert run["status"] == "failed", run
    assert run["cost_usd"] is None, "an unmeasured failure must stay NULL (never a fabricated $0)"
    usage = _usage(run)
    assert usage.get("cost_source") != "no_provider_calls", usage
    assert "no_provider_calls" not in usage, usage


def test_a_FAILED_replay_that_declares_zero_still_settles_the_measured_zero(queue_db, monkeypatch):
    """A replay that FAILS still made no model call — its outcome declaration rides the failure.

    The failure path is the one where "unpriced" usually means "a call's usage was lost"; a replay
    is the exception it can PROVE.  The settlement reads the run's outcome dict, not only its
    success, so the failed replay settles a measured $0.0000 too (and the day cap is not poisoned
    by a run that could not even start an action).
    """
    import app.agents.crew_factory as crew_factory  # noqa: F401 — the replay path is exercised below

    failure = MeridianError(
        "E_INTEGRATION", "the approved action could not be performed — the provider refused it"
    )
    async def _failed_replay(**kwargs):
        return {
            "summary": "replayed the approved payload: nothing was sent",
            "provider_calls": 0,
            "provider_calls_source": "approval_replay",
            "_replay_failure": failure,
        }

    monkeypatch.setattr(agent_tasks, "_replay_approved_actions", _failed_replay)
    task_id = _create_parked_task(queue_db, _parked_metadata())

    queue_db.run(agent_tasks._execute_agent_task(task_id))

    run = _runs(queue_db)[0]
    assert run["status"] == "failed", run
    assert run["cost_usd"] is not None, "a failed replay made ZERO calls: settle a measured 0"
    assert Decimal(str(run["cost_usd"])) == Decimal("0.0000"), run["cost_usd"]
    assert _usage(run)["no_provider_calls"]["declared_by"] == "approval_replay"


def test_a_refusal_that_DECLARES_zero_provider_calls_is_settled_too(queue_db, monkeypatch):
    """A path that refuses before a provider is reachable declares 0 on the refusal's context."""
    import app.agents.crew_factory as crew_factory
    from app.agents import accounting

    def _refuse(*args, **kwargs):
        raise MeridianError(
            E_POLICY_DENIED,
            "dispatch refused for social_media: kill switch active",
            how_to_fix="clear the kill switch",
            context={
                "agent_type": "social_media",
                accounting.PROVIDER_CALLS_KEY: 0,
                accounting.PROVIDER_CALLS_SOURCE_KEY: "dispatch_preflight",
            },
        )

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _refuse)
    task_id = queue_db.create_task(title="preflight refusal", agent_type="social_media")

    queue_db.run(agent_tasks._execute_agent_task(task_id))

    run = _runs(queue_db)[0]
    assert run["status"] == "failed", run
    assert run["cost_usd"] is not None, "a pre-dispatch refusal made ZERO calls: settle a measured 0"
    assert Decimal(str(run["cost_usd"])) == Decimal("0.0000"), run["cost_usd"]
    settlement = _usage(run)["no_provider_calls"]
    assert settlement["declared_by"] == "dispatch_preflight", settlement


def test_the_real_dispatch_preflight_refusal_declares_zero_calls(monkeypatch):
    """The PRODUCER, exercised on the real `run_agent_for_task` seam (not a double of it)."""
    import app.agents.crew_factory as crew_factory
    from app.agents import accounting

    monkeypatch.setattr(
        crew_factory, "dispatch_preflight", lambda agent_type, dispatch_paused=False: [
            "kill switch active: dispatch denied"
        ]
    )

    with pytest.raises(MeridianError) as excinfo:
        crew_factory.run_agent_for_task("social_media", {"id": 1, "title": "t"}, {})

    assert excinfo.value.code == E_POLICY_DENIED
    context = excinfo.value.context or {}
    assert context[accounting.PROVIDER_CALLS_KEY] == 0, context
    assert context[accounting.PROVIDER_CALLS_SOURCE_KEY] == "dispatch_preflight", context


# ── 5. the declaration reader: positive declarations only, junk is UNKNOWN ────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"provider_calls": "0"},          # a string is not a count
        {"provider_calls": 0.0},          # a float is not a count
        {"provider_calls": True},         # a flag is not a count (bool is an int subclass)
        {"provider_calls": -1},           # a negative count is nonsense
        {"provider_calls": None},
    ],
)
def test_the_declaration_reader_refuses_absence_and_junk(payload):
    from app.agents import accounting

    assert accounting.provider_call_declaration(payload) is None


def test_the_declaration_reader_returns_the_count_and_its_source():
    from app.agents import accounting

    assert accounting.provider_call_declaration({"provider_calls": 0}) == (0, "declared")
    assert accounting.provider_call_declaration(
        {"provider_calls": 0, "provider_calls_source": "approval_replay"}
    ) == (0, "approval_replay")
    assert accounting.provider_call_declaration({"provider_calls": 3}) == (3, "declared")
    # A blank source is "declared", never an empty-string provenance.
    assert accounting.provider_call_declaration(
        {"provider_calls": 0, "provider_calls_source": "   "}
    ) == (0, "declared")


# ── 6. the wire: the settled zero is visible on the run detail ────────────────────────────────


@pytest.mark.asyncio
async def test_the_settled_zero_serves_through_the_run_read_model(async_db_session):
    """The owner's view: a zero-call run is $0.0000 measured + attributable, not "unmeasured"."""
    from app.services.run_service import get_run

    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    async_db_session.add(
        AgentRun(
            agent_type="email_outreach",
            status="completed",
            cost_usd=Decimal("0.0000"),
            started_at=started,
            ended_at=started + timedelta(minutes=1),
            usage_json={
                "cost_source": "no_provider_calls",
                "provider_calls": 0,
                "no_provider_calls": {"declared_by": "approval_replay"},
            },
        )
    )
    await async_db_session.commit()
    run_id = (await async_db_session.execute(select(AgentRun.id))).scalar_one()

    detail = await get_run(async_db_session, run_id=run_id)
    assert detail is not None
    assert detail.cost_usd == 0.0, detail
    assert detail.cost_source == "no_provider_calls", detail
    # Not an interruption: the markers that mean "we lost part of this run" stay null.
    assert detail.cost_scope is None, detail
    assert detail.interrupted_reason is None, detail
