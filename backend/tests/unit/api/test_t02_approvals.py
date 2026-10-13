"""T02/D1 — the approvals channel: mint (owner API) -> verify -> the gated action PROCEEDS;
no channel -> the shipped fail-closed DENY, byte for byte.

The loop this file exercises (D1's ruling; the live product's "owner green light on anything that
moves money or commits code"):

1. `POST /api/v1/approvals` (owner key) mints a TTL-bounded HMAC token for ONE action fingerprint;
2. the run publishes its approval material (`settings.approval_secret` + the task's supplied
   `metadata.approval_token`) on the `RUN_APPROVAL` contextvar, and the side-effect path resolves
   it — a token that verifies turns the firewall's REQUIRE_APPROVAL into ALLOW;
3. a run WITHOUT a (valid) token keeps the action refused AND the task parks `awaiting_approval`
   with the fingerprint recorded in its result metadata — the value the owner mints for;
4. with `APPROVAL_SECRET` unset there is NO channel: the token cannot verify and the shipped DENY
   ("approval required but no approval channel configured") stands — this file pins its exact
   message, not a paraphrase.

RED legs (see RECEIPT.md): the resolver ignoring the run context fails (4)-adjacent leg 1; a
tampered token accepted fails leg 2; a default secret when none is configured fails leg 3.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import app.agents.crew_factory as crew_factory
from app.agents.policy_firewall import (
    ActionClass,
    Decision,
    RunApproval,
    configured_approval_secret,
    decide,
    fingerprint,
    issue_approval,
    reset_run_approval,
    set_run_approval,
    verify_approval,
)
from app.agents.side_effects import _resolve_approval as resolve_approval
from app.agents.side_effects import request_side_effect
from app.config import settings
from app.core.errors import E_CONFIG, E_POLICY_DENIED, PolsiaError
from tests.unit.celery.conftest import RunSpy

SECRET = "unit-test-approval-secret"
REFUND_PAYLOAD = {"action": "refund", "payment_intent": "pi_3TEST"}


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """No network: the worker legs publish activity through `events.get_redis` (the celery
    conftest's autouse guard is scoped to `tests/unit/celery/`)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def _clean_run_context():
    """No run context leaks between cases (the worker resets it in a `finally`; tests do too)."""
    reset_run_approval()
    yield
    reset_run_approval()


def _configure_secret(monkeypatch, value: str) -> None:
    """Point the live settings object at `value`.

    The frozen field (`config.py`, W2's T05) is read defensively by
    `policy_firewall.configured_approval_secret`; on a pre-W2 surface `approval_secret` does not
    exist yet and pydantic v2 refuses assignment to an undefined field, so the fallback writes the
    instance dict — the same place a defined field lives.
    """
    try:
        monkeypatch.setattr(settings, "approval_secret", value)
    except ValueError:  # pragma: no cover - only on a config surface without the field
        monkeypatch.setattr(settings, "__dict__", {**settings.__dict__, "approval_secret": value})


@pytest.fixture(autouse=True)
def _real_sends(monkeypatch):
    """The gated legs need the REAL-SENDS mode: in sandbox every external action is drafted
    before the approval machinery is reached (policy_firewall's sandbox branch), which is the
    shipped default and is exercised by its own tests."""
    monkeypatch.setattr(settings, "sandbox_mode", False)


def _refund_fingerprint() -> str:
    return fingerprint(ActionClass.EXTERNAL_IRREVERSIBLE.value, "stripe", REFUND_PAYLOAD)


# ── 1. the route: owner-authenticated, honest when unconfigured, verifiable when minted ──────


@pytest.mark.asyncio
async def test_the_mint_route_requires_the_owner_api_key(api_client):
    resp = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": _refund_fingerprint()}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_the_mint_route_refuses_by_name_when_no_channel_is_configured(
    api_client, auth_headers, monkeypatch
):
    _configure_secret(monkeypatch, "")
    assert configured_approval_secret() == b""

    resp = await api_client.post(
        "/api/v1/approvals",
        json={"action_fingerprint": _refund_fingerprint()},
        headers=auth_headers,
    )

    assert resp.status_code == 503, "an unconfigured channel cannot mint — no fake token"
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_CONFIG
    assert "APPROVAL_SECRET" in body["error"]["message"]
    assert body["error"]["how_to_fix"], "a refusal a human must act on carries its own fix"
    assert "token" not in body


@pytest.mark.asyncio
async def test_the_mint_route_returns_a_token_the_firewall_verifies(
    api_client, auth_headers, monkeypatch
):
    import time

    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()

    resp = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": fp}, headers=auth_headers
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["action_fingerprint"] == fp
    assert body["ttl_s"] == 3600
    assert abs(body["expires_at"] - (time.time() + 3600)) < 10, "1h default (D1)"

    assert verify_approval(SECRET.encode(), body["token"], action_fingerprint=fp) is True
    assert verify_approval(SECRET.encode(), body["token"], action_fingerprint="other") is False
    assert verify_approval(b"wrong-secret", body["token"], action_fingerprint=fp) is False


@pytest.mark.asyncio
async def test_the_mint_route_bounds_ttl_and_fingerprint(api_client, auth_headers, monkeypatch):
    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()

    too_long = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": fp, "ttl_s": 86401}, headers=auth_headers
    )
    assert too_long.status_code == 422, "a token that outlives the operator's attention is refused"

    zero = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": fp, "ttl_s": 0}, headers=auth_headers
    )
    assert zero.status_code == 422

    short = await api_client.post(
        "/api/v1/approvals", json={"action_fingerprint": "abc"}, headers=auth_headers
    )
    assert short.status_code == 422, "a fingerprint is not free text"


@pytest.mark.asyncio
async def test_a_route_minted_token_turns_decision_require_approval_into_allow(monkeypatch):
    """The two ends joined on one loop: the ROUTE mints, `decide()` verifies."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.v1 import approvals as approvals_route
    from app.main import create_app

    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()

    application: FastAPI = create_app()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/approvals",
            json={"action_fingerprint": fp, "ttl_s": 60},
            headers={"X-API-Key": settings.api_key},
        )
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    assert approvals_route.router.prefix == "/approvals"

    from app.agents.policy_firewall import ActionRequest

    request = ActionRequest(
        agent_type="finance",
        action_class=ActionClass.EXTERNAL_IRREVERSIBLE,
        channel="stripe",
        payload=REFUND_PAYLOAD,
        value_usd=30.0,
        approval_token=token,
        sends_today=0,
    )
    with_token = decide(request, approval_secret=SECRET.encode(), sandbox_mode=False)
    assert with_token.decision is Decision.ALLOW
    assert with_token.audit.get("approved") is True

    without = decide(
        ActionRequest(
            agent_type="finance",
            action_class=ActionClass.EXTERNAL_IRREVERSIBLE,
            channel="stripe",
            payload=REFUND_PAYLOAD,
            value_usd=30.0,
            sends_today=0,
        ),
        approval_secret=SECRET.encode(),
        sandbox_mode=False,
    )
    assert without.decision is Decision.REQUIRE_APPROVAL, "with a channel but no token: parked"


# ── 2. the run context: a valid token proceeds, an invalid one still refuses ──────────────────


def _request_refund(**overrides):
    return request_side_effect(
        "finance",
        ActionClass.EXTERNAL_IRREVERSIBLE,
        "stripe",
        action="refund",
        payload=dict(REFUND_PAYLOAD),
        value_usd=30.0,
        sends_today=0,
        **overrides,
    )


def test_a_run_publishing_a_valid_token_lets_the_gated_action_proceed():
    fp = _refund_fingerprint()
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    set_run_approval(RunApproval(secret=SECRET.encode(), token=token))

    outcome = _request_refund()

    assert outcome.allowed is True, "the token the run carries is what makes the gate open"
    assert outcome.requires_approval is False
    assert outcome.decision is not None and outcome.decision.decision is Decision.ALLOW
    assert outcome.audit.get("approved") is True


def test_an_invalid_token_still_refuses_and_records_require_approval():
    fp = _refund_fingerprint()
    good = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    set_run_approval(RunApproval(secret=SECRET.encode(), token=f"0.{good.split('.', 1)[1]}"))

    outcome = _request_refund()

    assert outcome.allowed is False
    assert outcome.requires_approval is True, "a bad token is a refusal, never a bypass"
    assert outcome.e_code == E_POLICY_DENIED


def test_an_expired_token_still_refuses():
    """Expiry is the token's own claim: a token minted with a past expiry cannot verify."""
    fp = _refund_fingerprint()
    expired = issue_approval(SECRET.encode(), action_fingerprint=fp, ttl_s=-60)
    assert verify_approval(SECRET.encode(), expired.token, action_fingerprint=fp) is False

    set_run_approval(RunApproval(secret=SECRET.encode(), token=expired.token))
    outcome = _request_refund()
    assert outcome.allowed is False
    assert outcome.requires_approval is True


def test_explicit_arguments_still_beat_the_run_context():
    """The pre-existing parameter path is untouched: an explicit secret/token wins."""
    fp = _refund_fingerprint()
    set_run_approval(RunApproval(secret=b"a-different-secret", token="0.deadbeef"))
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token

    outcome = _request_refund(approval_secret=SECRET.encode(), approval_token=token)

    assert outcome.allowed is True, "the explicit call the tests always made keeps working"


# ── 3. no channel: the shipped DENY, byte for byte ────────────────────────────────────────────


def test_no_channel_resolves_to_the_pre_channel_values():
    reset_run_approval()
    assert resolve_approval(b"", "") == (b"", ""), "no context must not invent a secret or token"


def test_no_channel_keeps_the_exact_shipped_refusal(monkeypatch):
    """The sandbox/no-channel fallback: THE pinned message, not a paraphrase (D1)."""
    _configure_secret(monkeypatch, "")
    reset_run_approval()

    with pytest.raises(PolsiaError) as excinfo:
        _request_refund()

    refusal = excinfo.value
    assert refusal.code == E_POLICY_DENIED
    assert refusal.message == (
        "finance action 'refund' refused: approval required but no approval channel configured"
    )

    # ...and the decision object itself is byte-identical to the pre-channel DENY
    direct = decide(
        __request_decision(),
        approval_secret=b"",
        sandbox_mode=False,
    )
    assert direct.decision is Decision.DENY
    assert direct.code == "POLSIA-E114"
    assert direct.reason == "approval required but no approval channel configured"


def __request_decision():
    from app.agents.policy_firewall import ActionRequest

    return ActionRequest(
        agent_type="finance",
        action_class=ActionClass.EXTERNAL_IRREVERSIBLE,
        channel="stripe",
        payload=dict(REFUND_PAYLOAD),
        value_usd=30.0,
        sends_today=0,
    )


# ── 4. the worker: context propagation, parking, and the token-supplied re-run ────────────────


def _create_task(queue_db, *, title: str, metadata: dict | None = None) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(db, title=title, agent_type="finance", metadata=metadata)
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _refund_delegate(seen: dict):
    """A stand-in for the finance agent's refund leg: the REAL firewall call the agent makes.

    Records the approval context it can see INSIDE the worker's run thread (the cross-thread
    propagation proof) and returns the run payload the worker scans for gated receipts.
    """
    from app.agents.policy_firewall import current_run_approval
    from app.agents.side_effects import side_effect_receipt

    def delegate(agent_type, task_dict, context):  # noqa: ANN001
        published = current_run_approval()
        seen["token"] = published.token if published is not None else None
        seen["secret_present"] = bool(published is not None and published.secret)
        outcome = request_side_effect(
            "finance",
            ActionClass.EXTERNAL_IRREVERSIBLE,
            "stripe",
            action="refund",
            payload=dict(REFUND_PAYLOAD),
            value_usd=30.0,
            sends_today=0,
        )
        receipt = side_effect_receipt(outcome, action="refund", ref="pi_3TEST")
        return {"summary": "refund proposed", "side_effects": [receipt]}

    return delegate


def _install_delegate(monkeypatch, queue_db, delegate) -> RunSpy:
    spy = RunSpy(queue_db, delegate=delegate)
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return spy


def test_the_worker_publishes_the_run_approval_into_the_run_thread(
    queue_db, monkeypatch
):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    task_id = _create_task(queue_db, title="T02 token run", metadata={"approval_token": token})

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert seen["token"] == token, "asyncio.to_thread must carry the context into the run thread"
    assert seen["secret_present"] is True, "the configured APPROVAL_SECRET must reach the run"
    assert outcome["status"] == "completed"
    assert outcome["approval_required"] == []
    stored = json.loads(queue_db.query("SELECT output FROM agent_runs WHERE task_id = ?", (task_id,))[0]["output"])
    assert stored["side_effects"][0]["decision"] == "allow"
    assert stored["side_effects"][0]["approved"] is True


def test_a_run_without_a_token_parks_the_task_with_the_fingerprint_recorded(
    queue_db, monkeypatch
):
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    task_id = _create_task(queue_db, title="T02 parked run")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval"
    assert [item["fingerprint"] for item in outcome["approval_required"]] == [fp]

    row = queue_db.row(task_id)
    assert row["status"] == "awaiting_approval", "the task parks, it does not report completion"
    recorded = json.loads(row["metadata"])
    assert recorded["approval_required"][0]["fingerprint"] == fp
    assert fp in (row["result_summary"] or ""), "the summary names what to mint for"

    run_row = queue_db.query("SELECT status, output FROM agent_runs WHERE task_id = ?", (task_id,))[0]
    assert run_row["status"] == "completed", "the RUN completed; the TASK waits for the owner"
    assert json.loads(run_row["output"])["approval_required"][0]["fingerprint"] == fp

    activities = queue_db.query(
        "SELECT action, level, detail FROM activity_log WHERE action = 'task_awaiting_approval'"
    )
    assert len(activities) == 1
    assert activities[0]["level"] == "warning"
    assert json.loads(activities[0]["detail"])["fingerprints"] == [fp]


def test_the_recorded_fingerprint_is_mintable_and_the_re_run_with_the_token_proceeds(
    queue_db, monkeypatch
):
    """The full loop, second half: mint for the recorded fingerprint, re-run with the token."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    first_task = _create_task(queue_db, title="T02 loop first")

    first = queue_db.run(_execute_agent_task(first_task))
    assert first["status"] == "awaiting_approval"
    recorded_fp = first["approval_required"][0]["fingerprint"]

    token = issue_approval(SECRET.encode(), action_fingerprint=recorded_fp).token
    second_task = _create_task(
        queue_db, title="T02 loop second", metadata={"approval_token": token}
    )

    second = queue_db.run(_execute_agent_task(second_task))

    assert second["status"] == "completed", "the owner's green light opens the gate"
    assert second["approval_required"] == []
    stored = json.loads(
        queue_db.query("SELECT output FROM agent_runs WHERE task_id = ?", (second_task,))[0]["output"]
    )
    assert stored["side_effects"][0]["decision"] == "allow"


def test_a_stale_token_parks_again_with_the_same_fingerprint(queue_db, monkeypatch):
    """An invalid supplied token is not a bypass — it parks, exactly like no token at all."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, SECRET)
    fp = _refund_fingerprint()
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    task_id = _create_task(
        queue_db, title="T02 stale token", metadata={"approval_token": "0.deadbeefdeadbeef"}
    )

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval"
    assert [item["fingerprint"] for item in outcome["approval_required"]] == [fp]


def test_no_secret_configured_means_no_channel_and_the_run_fails_honestly(
    queue_db, monkeypatch
):
    """The sandbox/no-channel fallback at the run level: the firewall's DENY stands, unchanged."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    _configure_secret(monkeypatch, "")
    seen: dict = {}
    _install_delegate(monkeypatch, queue_db, _refund_delegate(seen))
    task_id = _create_task(queue_db, title="T02 no channel")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert seen["secret_present"] is False
    assert outcome["status"] == "failed", "today's behaviour: a gated action with no channel raises"
    row = queue_db.row(task_id)
    assert row["status"] == "failed"
    assert "no approval channel configured" in (row["error_message"] or "")
