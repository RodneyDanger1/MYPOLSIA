"""W3 / OR-02 — a partially failed approved replay never loses the work it really did.

The defect (orchestration-hunter `BUGS.json` OR-02, P1): `_replay_approved_actions` performed
item 1, then RAISED on item 2's provider refusal — after the irreversible send.  Every outcome
(and the performed-item list) died with the raising frame; `_persist_run_artifacts` was gated on
`run_status == "completed"`, so `email_logs` was never written; the send-once markers were never
merged; and `update_task_status("failed")` ran R3's rule and emptied
`metadata.approval_required` while leaving `metadata.approval_token` and `approved_payload`
behind.  The retry then saw an EMPTY parked set (`_approval_replay_plan` returns `{}`), ran the
AGENT with the old token attached, and could re-send item 1.

What this file pins (PLAN.md seat W3 acceptance):

  * a failed second item leaves the row failed (then requeued by the shipped retry ladder) with
    item 1's `email_logs` row, its `approval_replay[fp1] = sent` marker and its receipt persisted
    BEFORE the failure is observable (one transaction, one commit);
  * the owed item stays PARKED (the failed write's clear is undone in the same transaction) so a
    second delivery REPLAYS it — and performs item 2 only, never item 1 again;
  * an UNPROVEN delivery (an unexpected sender exception) is recorded as `provider_refused`: a
    later delivery REFUSES by name instead of re-sending, and the agent is never re-run;
  * a TERMINAL failure ends the approval explicitly (`metadata.approval_replay_ended`, token
    dropped, parked set cleared) — never a failed row that still advertises a live approval;
  * a parked row delivered WITHOUT a token never falls through to the model (it re-parks).

The two-item batch is driven through a `verify_approval` double: one minted token names exactly
one fingerprint (`policy_firewall.issue_approval`), so a batch whose BOTH items reach the sender
cannot be produced from a single real token.  The real single-token semantics — the second item
stays parked and the row re-parks — stay pinned by
`tests/unit/test_b1_c_orchestration.py::test_a_partially_approved_park_sends_the_approved_item_and_keeps_the_rest_parked`
(unmodified by this seat).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import app.agents.crew_factory as crew_factory
import app.config as app_config
from app.agents.policy_firewall import (
    fingerprint,
    issue_approval,
    reset_run_approval,
)
from app.config import settings
from app.core.errors import E_INTEGRATION, E_POLICY_DENIED, MeridianError
from celery_app.tasks import agent_tasks

SECRET = "w3-or02-approval-secret"
BODY_A = "first approved body"
BODY_B = "second approved body"


# ── fixtures: no network, no run context leaking between cases ─────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_redis_events(monkeypatch):
    """The worker legs publish activity through `events.get_redis` (belt and braces: the celery
    conftest's autouse guard already does this for tests in this directory)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def _clean_run_context():
    reset_run_approval()
    yield
    reset_run_approval()


@pytest.fixture(autouse=True)
def _outside_sandbox(monkeypatch):
    """An approval replay is only reachable outside SANDBOX_MODE (a sandboxed run drafts)."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)


def _configure_secret(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "approval_secret", value)


# ── the approved payload rows the parking branch writes (F-14 leg B) ──────────────────────────


def _payload(subject: str, body: str, to: str) -> dict:
    return {"action": "send", "subject": subject, "body": body, "to": to}


def _record(subject: str, body: str, to: str) -> dict:
    return {
        "agent_type": "email_outreach",
        "action": "send",
        "ref": to,
        "channel": "email",
        "class": "external_reversible",
        "value_usd": 0.0,
        "payload": _payload(subject, body, to),
        "item": {"prospect_email": to, "subject": subject, "body": body},
    }


def _two_item_park() -> tuple[dict, str, str]:
    """(metadata, fp_first, fp_second) — two parked items with their stored approved payloads."""
    first = _payload("first", BODY_A, "first@example.com")
    second = _payload("second", BODY_B, "second@example.com")
    fp_first = fingerprint("external_reversible", "email", first)
    fp_second = fingerprint("external_reversible", "email", second)
    metadata = {
        "approval_required": [
            {"action": "send", "ref": "first@example.com", "channel": "email",
             "fingerprint": fp_first, "reason": "context tainted"},
            {"action": "send", "ref": "second@example.com", "channel": "email",
             "fingerprint": fp_second, "reason": "context tainted"},
        ],
        agent_tasks.APPROVED_PAYLOAD_KEY: {
            fp_first: _record("first", BODY_A, "first@example.com"),
            fp_second: _record("second", BODY_B, "second@example.com"),
        },
    }
    return metadata, fp_first, fp_second


# ── the task row + the token attach (the shipped verbs) ───────────────────────────────────────


def _create_parked_task(queue_db, metadata: dict) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db,
                title="W3 partial replay",
                agent_type="email_outreach",
                source="scheduler",
                status="awaiting_approval",
                metadata=metadata,
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _attach_token(queue_db, task_id: int, token: str) -> None:
    from app.services.task_service import attach_approval_token

    async def _attach() -> None:
        async with queue_db.factory() as db:
            await attach_approval_token(db, task_id, token)
            await db.commit()

    queue_db.run(_attach())


def _metadata(queue_db, task_id: int) -> dict:
    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


# ── the provider / model doubles ──────────────────────────────────────────────────────────────


class BatchProvider:
    """The provider side of a REAL delivered email; fails the SECOND item once, then succeeds."""

    def __init__(self, *, second_failure: BaseException | None) -> None:
        self.calls: list[dict] = []
        self.second_failure = second_failure
        self.failed_once = False

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        if (
            kwargs.get("to") == "second@example.com"
            and self.second_failure is not None
            and not self.failed_once
        ):
            self.failed_once = True
            raise self.second_failure
        return {"ok": True, "result": {"message_id": f"sg-w3-{len(self.calls)}"}}

    def sends_to(self, address: str) -> int:
        return sum(1 for call in self.calls if call.get("to") == address)


def _install_provider(monkeypatch, provider: BatchProvider) -> BatchProvider:
    """Stub ONLY the guarded sender's last hop for the replay leg."""
    monkeypatch.setattr("app.agents.email_outreach.compliance_send.send_outreach_email", provider)
    return provider


def _install_model(monkeypatch) -> list[dict]:
    """Record every `run_agent_for_task` call: the resume must never ask the model again."""
    calls: list[dict] = []

    def _spy(agent_type, task_dict, context):  # noqa: ANN001
        calls.append({"agent_type": agent_type, "task_id": task_dict.get("id")})
        return {"summary": "the model was asked to re-generate the approved body"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return calls


def _accept_every_token(monkeypatch) -> None:
    """Drive a BATCH approval (see the module docstring): one token, two covered fingerprints."""
    monkeypatch.setattr(
        "app.agents.policy_firewall.verify_approval", lambda *args, **kwargs: True
    )


# ── 1. the partial failure keeps the work and the owed approval ───────────────────────────────


def test_a_failed_second_item_keeps_item_ones_artifact_marker_and_the_owed_park(
    queue_db, monkeypatch
):
    """RED (pre-fix): the replay raised, `email_logs` stayed empty, the parked set was destroyed."""
    _configure_secret(monkeypatch, SECRET)
    _accept_every_token(monkeypatch)
    provider = _install_provider(
        monkeypatch,
        BatchProvider(
            second_failure=MeridianError(E_INTEGRATION, "the provider answered 503 mid-batch")
        ),
    )
    model_calls = _install_model(monkeypatch)
    metadata, fp_first, fp_second = _two_item_park()
    task_id = _create_parked_task(queue_db, metadata)
    # The owner's token names the item that still has to go out.
    _attach_token(queue_db, task_id, issue_approval(
        SECRET.encode(), action_fingerprint=fp_second).token)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["retryable"] is True, outcome
    assert outcome["requeued"] is True, outcome
    assert model_calls == [], "the resume re-ran the model instead of replaying the stored payload"
    # item 1 was really sent, exactly once; item 2 attempted once and failed.
    assert provider.sends_to("first@example.com") == 1, provider.calls
    assert provider.sends_to("second@example.com") == 1, provider.calls

    # (a) item 1's artifact was persisted BEFORE the failure became observable.
    logs = queue_db.query(
        "SELECT subject, sendgrid_id, status FROM email_logs ORDER BY id"
    )
    assert len(logs) == 1, f"the performed send's artifact was lost: {logs!r}"
    # (`email_logs` carries `prospect_id`, not the address — the SUBJECT is the item identity here)
    assert logs[0]["subject"] == "first", logs[0]
    assert logs[0]["sendgrid_id"], logs[0]
    # (b) the send-once marker for item 1 records the SEND (it can never be replayed).
    meta = _metadata(queue_db, task_id)
    assert meta["approval_replay"][fp_first]["outcome"] == "sent", meta.get("approval_replay")
    # (c) the owed item is still parked — the failed write's clear is undone in the same commit —
    #     and the false "cleared" record R3 wrote is gone.
    assert [record["fingerprint"] for record in meta["approval_required"]] == [fp_second], meta
    assert "approval_cleared" not in meta, meta
    # (d) the token survives because it still names the owed fingerprint.
    assert meta.get("approval_token"), "the token that covers the owed item was dropped"
    # (e) the stored approved payload is kept for both (the sent one for audit, the owed one to
    #     replay); nothing is re-generated.
    assert set(meta[agent_tasks.APPROVED_PAYLOAD_KEY]) == {fp_first, fp_second}
    # (f) the row is requeued by the shipped retry ladder (claimable again), failed otherwise.
    row = queue_db.row(task_id)
    assert row["status"] == "pending", row
    assert E_INTEGRATION in (row["error_message"] or ""), row["error_message"]
    # (g) the partial outcome is in the activity feed, not only in the row.
    actions = [
        record["action"]
        for record in queue_db.query("SELECT action FROM activity_log ORDER BY id")
    ]
    assert "approval_replay_incomplete" in actions, actions


def test_a_second_delivery_replays_the_owed_item_only_and_never_re_sends_item_one(
    queue_db, monkeypatch
):
    """The whole point of OR-02: a retry performs item 2 and NOT item 1 again."""
    _configure_secret(monkeypatch, SECRET)
    _accept_every_token(monkeypatch)
    provider = _install_provider(
        monkeypatch,
        BatchProvider(
            second_failure=MeridianError(E_INTEGRATION, "the provider answered 503 mid-batch")
        ),
    )
    model_calls = _install_model(monkeypatch)
    metadata, fp_first, fp_second = _two_item_park()
    task_id = _create_parked_task(queue_db, metadata)
    _attach_token(queue_db, task_id, issue_approval(
        SECRET.encode(), action_fingerprint=fp_second).token)

    first = queue_db.run(agent_tasks._execute_agent_task(task_id))
    assert first["status"] == "failed", first
    assert provider.sends_to("first@example.com") == 1

    # The retry delivery: the row is claimable again, the owed item is still parked.
    second = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert second["status"] == "completed", (second, queue_db.row(task_id)["error_message"])
    assert provider.sends_to("first@example.com") == 1, (
        f"item 1 was re-sent on the retry: {provider.calls!r}"
    )
    assert provider.sends_to("second@example.com") == 2, provider.calls
    assert model_calls == [], "the retry asked the model again instead of replaying"
    logs = queue_db.query("SELECT subject FROM email_logs ORDER BY id")
    assert [row["subject"] for row in logs] == ["first", "second"], logs
    meta = _metadata(queue_db, task_id)
    assert meta["approval_replay"][fp_second]["outcome"] == "sent", meta["approval_replay"]
    assert meta["approval_required"] == [], "a completed row must have no live parked approval"
    assert queue_db.row(task_id)["status"] == "completed"


# ── 2. an UNPROVEN delivery is never re-sent ──────────────────────────────────────────────────


def test_an_unexpected_sender_exception_records_the_unknown_outcome_and_blocks_a_resend(
    queue_db, monkeypatch
):
    """A raised sender may have reached the provider: the retry must REFUSE, not re-send."""
    _configure_secret(monkeypatch, SECRET)
    _accept_every_token(monkeypatch)
    provider = _install_provider(
        monkeypatch,
        BatchProvider(second_failure=RuntimeError("connection reset after the request body")),
    )
    model_calls = _install_model(monkeypatch)
    metadata, fp_first, fp_second = _two_item_park()
    task_id = _create_parked_task(queue_db, metadata)
    _attach_token(queue_db, task_id, issue_approval(
        SECRET.encode(), action_fingerprint=fp_second).token)

    first = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert first["status"] == "failed", first
    assert first["requeued"] is True, first
    meta = _metadata(queue_db, task_id)
    assert meta["approval_replay"][fp_second]["outcome"] == "provider_refused", (
        meta["approval_replay"]
    )
    assert [record["fingerprint"] for record in meta["approval_required"]] == [fp_second]
    assert len(queue_db.query("SELECT id FROM email_logs")) == 1

    # The retry: the unknown-outcome marker makes the replay REFUSE by name — no second send,
    # no model call.
    second = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert second["status"] == "skipped", second
    assert second["reason"] == "E_ACTION_REPLAY_UNAVAILABLE", second
    assert second["task_status"] == "rejected", second
    assert provider.sends_to("first@example.com") == 1
    assert provider.sends_to("second@example.com") == 1, (
        f"the unproven send was attempted again: {provider.calls!r}"
    )
    assert model_calls == []


# ── 3. a TERMINAL failure ends the approval explicitly ────────────────────────────────────────


def test_a_terminal_replay_failure_ends_the_parked_approval_and_drops_the_token(
    queue_db, monkeypatch
):
    _configure_secret(monkeypatch, SECRET)
    _accept_every_token(monkeypatch)
    _install_provider(
        monkeypatch,
        BatchProvider(second_failure=MeridianError(E_POLICY_DENIED, "the opt-out gate refused")),
    )
    _install_model(monkeypatch)
    metadata, fp_first, fp_second = _two_item_park()
    task_id = _create_parked_task(queue_db, metadata)
    _attach_token(queue_db, task_id, issue_approval(
        SECRET.encode(), action_fingerprint=fp_second).token)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["requeued"] is False, "E_POLICY_DENIED is terminal"
    row = queue_db.row(task_id)
    assert row["status"] == "failed", row
    meta = _metadata(queue_db, task_id)
    assert meta["approval_required"] == [], (
        "a terminal failed row must not still advertise a live parked approval"
    )
    assert "approval_token" not in meta, "a spent bearer token must not ride a later re-arm"
    assert meta["approval_replay_ended"]["fingerprints"] == [fp_second], meta
    assert meta["approval_replay_ended"]["token_cleared"] is True
    assert len(queue_db.query("SELECT id FROM email_logs")) == 1, "item 1's artifact still lands"


# ── 4. the resume seam: a parked row is never handed back to the model ────────────────────────


def test_a_parked_row_delivered_without_a_token_never_runs_the_model(queue_db, monkeypatch):
    """A parked set is replayed or refused — never re-generated (OR-02's token-loss edge)."""
    _configure_secret(monkeypatch, SECRET)
    _install_provider(monkeypatch, BatchProvider(second_failure=None))
    model_calls = _install_model(monkeypatch)
    metadata, _fp_first, _fp_second = _two_item_park()

    async def _make() -> int:
        from app.services.task_service import create_task

        async with queue_db.factory() as db:
            task = await create_task(
                db,
                title="parked, no token",
                agent_type="email_outreach",
                source="scheduler",
                metadata=metadata,   # parked set + stored payloads, NO `approval_token`
            )
            await db.commit()
            return int(task.id)

    task_id = queue_db.run(_make())

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert model_calls == [], (
        "a parked row with no token fell through to the agent — a regenerated body could be "
        "sent under an approval the owner gave for different words"
    )
    assert outcome["status"] == "awaiting_approval", outcome
    assert [item["fingerprint"] for item in outcome["approval_required"]] == [
        metadata["approval_required"][0]["fingerprint"],
        metadata["approval_required"][1]["fingerprint"],
    ], outcome
