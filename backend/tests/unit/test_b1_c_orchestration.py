"""Wave B1 / seat C3 — what survives a restart or a new day.

Two P1 findings, both about state that only exists in prose or only exists until the next blip.

**F-14 leg B — the approved payload is never persisted, so a resume re-sends a DIFFERENT body.**
D1 (phase A) bound the approval fingerprint to the FULL body the owner is shown
(`policy_firewall.fingerprint` over `action_class|channel|canonical(payload)`), which means a
regenerated body can never verify an old token.  Leg B is the other half: the exact approved
payload must be STORED with the approval and REPLAYED on resume.  This file pins:

  * parking a run writes `tasks.metadata["approved_payload"][fp]` = the exact payload that was
    fingerprinted (reconstructed from the run's own items and SELF-CHECKED against the receipt's
    fingerprint — a projection that does not reproduce the fingerprint stores no payload);
  * a resume sends the STORED body (A) even when the model would emit a different body (B), and
    never calls the model to get it;
  * a resume whose parked fingerprint has no stored payload REFUSES by name
    (`E_ACTION_REPLAY_UNAVAILABLE`) instead of re-sending a regenerated body;
  * a replay that already began is never begun twice (a second send could double the message).

**F-21 — one broker blip burns the company-local day.**  The morning cycle commits its
once-per-(cycle, local-day) claim BEFORE any spend, then fans the created rows out.  When the
broker refuses that fan-out the exception aborted the cycle BEFORE its report, the claim stayed
committed, and the window closed — the day's cycle was unrun and unrecoverable.  This file pins:

  * a refused fan-out RE-ARMS the day (a durable `cycle_rearmed` record + `metadata.rearmed_at`
    on the created rows through the shipped `stamp_rearmed`), and the cycle still records itself;
  * the next tick of the SAME company-local day finishes the hand-off ONCE — from the stored
    plan, with no second model call — and a third tick skips;
  * the catch-up is bounded to its own company-local day: a re-arm recorded for another day is
    never a catch-up.

Every test below was written BEFORE its fix and observed red on the defect; the seat's REPORT.md
carries the planted/restored SHA-256 pairs (raw/C3/red-leg*.log).
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.agents.crew_factory as crew_factory
import app.config as app_config
from app.agents.policy_firewall import (
    RunTaint,
    fingerprint,
    issue_approval,
    reset_run_approval,
    reset_run_taint,
    set_run_taint,
)
from app.config import settings
from app.core.errors import E_ACTION_REPLAY_UNAVAILABLE
from celery_app.tasks import agent_tasks, daily_cycle

SECRET = "b1-c3-approval-secret"
#: Two bodies that differ ONLY past char 200 — the same pair D1 uses, so a "regenerated" body is
#: indistinguishable from A by any truncated preimage: the replay must compare/replay REAL bytes.
BODY_A = "z" * 239 + "A" + "z" * 20
BODY_B = "z" * 239 + "B" + "z" * 20


# ── fixtures: no network, no run context leaking between cases ─────────────────


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """The worker legs publish activity through `events.get_redis` (the celery conftest's autouse
    guard is scoped to `tests/unit/celery/`, and this file lives in `tests/unit/`)."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def _clean_run_context():
    reset_run_approval()
    reset_run_taint()
    yield
    reset_run_approval()
    reset_run_taint()


@pytest.fixture(autouse=True)
def _outside_sandbox(monkeypatch):
    """The approval channel is only reachable outside SANDBOX_MODE (a sandboxed run drafts)."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)


def _configure_secret(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "approval_secret", value)


# ── the worker's own task-row verbs (file truth, never the ORM) ────────────────


def _create_task(queue_db, *, title: str, agent_type: str = "email_outreach",
                 metadata: dict | None = None, status: str = "pending") -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db, title=title, agent_type=agent_type, source="scheduler",
                metadata=metadata, status=status,
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _attach_token(queue_db, task_id: int, token: str) -> None:
    """Deliver the minted token exactly as `POST /api/v1/approvals` does (the real service verb)."""
    from app.services.task_service import attach_approval_token

    async def _attach() -> None:
        async with queue_db.factory() as db:
            await attach_approval_token(db, task_id, token)
            await db.commit()

    queue_db.run(_attach())


def _metadata(queue_db, task_id: int) -> dict:
    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


# ── the email site: the REAL agent, the REAL firewall, a fixed model output ─────


def _email_delegate(body: str, *, calls: list[dict], send: Any, subject: str = "a subject"):
    """A run of the real `EmailOutreachAgent` whose model output is fixed to `body`.

    The agent, its send loop, the firewall and the approval channel are the shipped ones; only the
    MODEL OUTPUT is fixed (the documented `collect_structured` seam every agent test uses).  The
    run is TAINTED, which is the shipped way a reversible egress is escalated to a human.
    """
    from app.agents.email_outreach.agent import EmailOutreachAgent

    def delegate(agent_type: str, task_dict: dict, context: dict) -> dict:
        calls.append({
            "agent_type": agent_type,
            "task": dict(task_dict),
            "metadata": dict(task_dict.get("metadata") or {}),
        })
        agent = EmailOutreachAgent()
        payload = {
            "action": "send",
            "emails": [{
                "prospect_email": "prospect@example.com",
                "subject": subject,
                "body": body,
            }],
            "summary": f"draft for {body[:6]}",
        }
        agent.collect_structured = lambda task, context, **kw: (copy.deepcopy(payload), None)
        set_run_taint(RunTaint(tainted=True, flagged_blocks=[1], sources=["task"]))
        try:
            out = agent.run(task_dict, context)
        finally:
            reset_run_taint()
        out["agent_type"] = agent_type
        return out

    return delegate


class ProviderRecorder:
    """The provider side of a REAL delivered email (never the code under test)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reply: dict = {"ok": True, "result": {"message_id": "sg-b1c3-1"}}

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return self.reply


def _install_email_provider(monkeypatch) -> ProviderRecorder:
    """Stub ONLY the guarded sender's last hop for BOTH callers (the agent and the replay leg)."""
    rec = ProviderRecorder()
    monkeypatch.setattr("app.agents.email_outreach.agent.send_outreach_email", rec)
    monkeypatch.setattr("app.agents.email_outreach.compliance_send.send_outreach_email", rec)
    return rec


def _install_run(monkeypatch, delegate) -> list[dict]:
    calls: list[dict] = []
    spy = delegate(calls)
    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return calls


# ── F-14 leg B: the payload the owner saw is the payload that is sent ──────────


def test_parking_stores_the_exact_payload_the_owner_was_shown(queue_db, monkeypatch):
    """A parked run records the words, not just the fingerprint (the defect: only prose)."""
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_A, calls=calls, send=provider),
    )
    task_id = _create_task(queue_db, title="F-14 park")

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval", outcome
    requirement = outcome["approval_required"][0]
    fp = requirement["fingerprint"]
    stored = _metadata(queue_db, task_id)

    assert stored["approval_required"][0]["fingerprint"] == fp, "the parked record is unchanged"
    approved = stored.get(agent_tasks.APPROVED_PAYLOAD_KEY)
    assert isinstance(approved, dict) and approved, (
        "the approved payload was NOT persisted — a resume can only re-generate the prose "
        f"(metadata={stored!r})"
    )
    record = approved[fp]
    assert record["payload"] == {
        "action": "send",
        "subject": "a subject",
        "body": BODY_A,
        "to": "prospect@example.com",
    }, record
    assert record["agent_type"] == "email_outreach"
    assert record["item"]["prospect_email"] == "prospect@example.com"
    # The stored payload IS the approved action: its own fingerprint is the fingerprint the owner
    # mints a token for.  A reconstruction that does not reproduce it would be a different action.
    assert fingerprint(record["class"], record["channel"], record["payload"]) == fp
    assert provider.calls == [], "a parked run sends nothing"
    assert len(calls) == 1


@pytest.mark.parametrize("site", ["email_outreach", "customer_support", "social_media",
                                  "code_generation", "finance"])
def test_every_gated_site_projects_the_exact_approved_payload(site):
    """The projection is per-agent and SELF-CHECKED: each result's items reproduce the receipt.

    No DB, no worker: the receipts are built with the SHIPPED `side_effect_receipt` shape and the
    SHIPPED `fingerprint`, so a projection that drifts from the agent's own payload construction
    cannot pass this test.
    """
    from app.agents.policy_firewall import ActionClass

    cases: dict[str, tuple[dict, dict, dict]] = {
        # site -> (result, receipt extras, expected payload)
        "email_outreach": (
            {"action": "send", "emails": [{"prospect_email": "a@b.com", "subject": "s",
                                           "body": BODY_A}]},
            {"action": "send", "ref": "a@b.com", "class": ActionClass.EXTERNAL_REVERSIBLE.value,
             "channel": "email"},
            {"action": "send", "subject": "s", "body": BODY_A, "to": "a@b.com"},
        ),
        "customer_support": (
            {"replies": [{"message_ref": "inbox-7", "proposed_action": "send_reply",
                          "from_address": "c@d.com", "subject": "hi", "reply_body": BODY_A}]},
            {"action": "send_reply", "ref": "inbox-7",
             "class": ActionClass.EXTERNAL_REVERSIBLE.value, "channel": "email"},
            {"action": "send_reply", "ref": "inbox-7", "body": BODY_A, "to": "c@d.com"},
        ),
        "social_media": (
            {"action": "post",
             "posts": [{"platform": "twitter", "content": BODY_A}],
             "replies": []},
            {"action": "post", "ref": "post:0",
             "class": ActionClass.EXTERNAL_REVERSIBLE.value, "channel": "twitter"},
            {"action": "post", "content": BODY_A, "ref": "post:0", "platform": "twitter"},
        ),
        "code_generation": (
            {"action": "open_pr", "changes": [{"repo": "acme/site", "branch": "feat/x",
                                               "pr_title": "a title", "pr_body": BODY_A}]},
            {"action": "open_pr", "ref": "acme/site#feat/x",
             "class": ActionClass.EXTERNAL_IRREVERSIBLE.value, "channel": "github"},
            {"action": "open_pr", "repo": "acme/site", "branch": "feat/x",
             "pr_title": "a title", "pr_body": BODY_A},
        ),
        "finance": (
            {"failed_payments": [{"payment_intent_id": "pi_3TEST", "event_id": "evt_1",
                                  "amount_cents": 3000, "refund_requested": True}]},
            {"action": "refund", "ref": "pi_3TEST",
             "class": ActionClass.EXTERNAL_IRREVERSIBLE.value, "channel": "stripe"},
            {"action": "refund", "ref": "pi_3TEST", "event_id": "evt_1"},
        ),
    }
    result, receipt_fields, expected = cases[site]
    fp = fingerprint(receipt_fields["class"], receipt_fields["channel"], expected)
    from app.agents.policy_firewall import Decision, PolicyDecision
    from app.agents.side_effects import SideEffectOutcome, side_effect_receipt

    decision = PolicyDecision(
        Decision.REQUIRE_APPROVAL,
        "context tainted by untrusted content",
        "POLSIA-E115",
        require_token=True,
        audit={"fingerprint": fp},
    )
    outcome = SideEffectOutcome(
        allowed=False, sandboxed=False, requires_approval=True,
        policy_code="POLSIA-E115", reason="needs a human",
        audit={"fingerprint": fp}, decision=decision,
    )
    receipt = side_effect_receipt(
        outcome, action=receipt_fields["action"], ref=receipt_fields["ref"]
    )
    receipt.update({"class": receipt_fields["class"], "channel": receipt_fields["channel"]})
    result["side_effects"] = [receipt]

    approved = agent_tasks.approved_payloads_for(site, result, context={})

    assert set(approved) == {fp}, (site, approved)
    record = approved[fp]
    assert record["payload"] == expected, (site, record)
    assert record["class"] == receipt_fields["class"]
    assert record["channel"] == receipt_fields["channel"]
    assert fingerprint(record["class"], record["channel"], record["payload"]) == fp


def test_a_projection_that_cannot_reproduce_the_fingerprint_stores_no_payload():
    """Fail closed: an unrecognised gated action is recorded as UNREPLAYABLE, never guessed."""
    outcome = agent_tasks.approved_payloads_for(
        "email_outreach",
        {"action": "send",
         "emails": [{"prospect_email": "a@b.com", "subject": "s", "body": BODY_A}],
         "side_effects": [{"action": "send", "ref": "a@b.com", "channel": "email",
                           "class": "external_reversible",
                           "fingerprint": "000000000000000000000000", "decision":
                           "require_approval"}]},
        context={},
    )
    assert len(outcome) == 1
    record = next(iter(outcome.values()))
    assert record["payload"] is None, "a payload that does not reproduce the fingerprint is a guess"
    assert record["reason"] == "projection_mismatch"


def test_a_resume_replays_the_stored_body_and_never_a_regenerated_one(queue_db, monkeypatch):
    """THE PROOF: approve body A, resume against a model that would emit body B -> the send is A.

    The model is not asked at all: the approved bytes are the whole action.  Both the email log
    (the artifact the email cap counts) and the provider call are checked against A.
    """
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    first_calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_A, calls=calls, send=provider),
    )
    task_id = _create_task(queue_db, title="F-14 replay")

    parked = queue_db.run(agent_tasks._execute_agent_task(task_id))
    assert parked["status"] == "awaiting_approval", parked
    fp = parked["approval_required"][0]["fingerprint"]
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach_token(queue_db, task_id, token)
    assert provider.calls == [], "nothing was sent before the approval"

    # The resume: the model would now emit B (D1 measured 4 agents regenerate their prose).
    resume_calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_B, calls=calls, send=provider),
    )

    replayed = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert len(resume_calls) == 0, (
        "the resume re-ran the model, so the body it sent was a REGENERATED one "
        f"({[call['agent_type'] for call in resume_calls]} agent call(s) after the approval)"
    )
    assert replayed["status"] == "completed", replayed
    assert len(provider.calls) == 1, (
        f"the approved send was not performed exactly once: {provider.calls!r}"
    )
    sent = provider.calls[0]
    assert sent["to"] == "prospect@example.com", sent
    assert sent["body"] == BODY_A, (
        f"the replay sent a different body than the owner approved: {sent['body'][:40]!r} != A"
    )
    assert sent["subject"] == "a subject", sent
    assert queue_db.row(task_id)["status"] == "completed"
    assert queue_db.count("email_logs") == 1, "the performed send's artifact was not written"
    assert len(first_calls) == 1
    stored = json.loads(queue_db.query(
        "SELECT output FROM agent_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    )[0]["output"])
    assert stored["side_effects"][0]["decision"] == "allow"
    assert stored[agent_tasks.APPROVED_PAYLOAD_KEY][fp]["payload"]["body"] == BODY_A


def test_a_resume_with_no_stored_payload_refuses_instead_of_re_sending(queue_db, monkeypatch):
    """A row parked before the payload was stored cannot be replayed -> a NAMED refusal, no send."""
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_B, calls=calls, send=provider),
    )
    fp = fingerprint("external_reversible", "email",
                     {"action": "send", "subject": "a subject", "body": BODY_A,
                      "to": "prospect@example.com"})
    task_id = _create_task(
        queue_db,
        title="F-14 legacy parked row",
        status="awaiting_approval",
        metadata={"approval_required": [{"action": "send", "ref": "prospect@example.com",
                                         "channel": "email", "fingerprint": fp,
                                         "reason": "context tainted"}]},
    )
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach_token(queue_db, task_id, token)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == E_ACTION_REPLAY_UNAVAILABLE, outcome
    assert outcome["task_status"] == "rejected", outcome
    assert "metadata" in (outcome.get("how_to_fix") or ""), outcome
    assert provider.calls == [], "a regenerated body must never be sent under the old approval"
    assert calls == [], "the model must not be asked to re-generate the body either"
    row = queue_db.row(task_id)
    assert row["status"] == "rejected", row
    assert E_ACTION_REPLAY_UNAVAILABLE in (row["error_message"] or "")
    refused = queue_db.query(
        "SELECT action, level, summary FROM activity_log WHERE action = 'approval_replay_refused'"
    )
    assert len(refused) == 1 and refused[0]["level"] == "warning", refused
    assert queue_db.count("agent_runs") == 0, "a refusal before execution writes no ledger row"


def test_a_replay_that_already_began_is_never_sent_twice(queue_db, monkeypatch):
    """`unknown is terminal`: a started replay is not resumed — a second send doubles the mail."""
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_B, calls=calls, send=provider),
    )
    fp = fingerprint("external_reversible", "email",
                     {"action": "send", "subject": "a subject", "body": BODY_A,
                      "to": "prospect@example.com"})
    task_id = _create_task(
        queue_db,
        title="F-14 interrupted replay",
        status="awaiting_approval",
        metadata={
            "approval_required": [{"action": "send", "ref": "prospect@example.com",
                                   "channel": "email", "fingerprint": fp}],
            agent_tasks.APPROVED_PAYLOAD_KEY: {
                fp: {"agent_type": "email_outreach", "action": "send",
                     "ref": "prospect@example.com", "channel": "email",
                     "class": "external_reversible",
                     "payload": {"action": "send", "subject": "a subject", "body": BODY_A,
                                 "to": "prospect@example.com"},
                     "item": {"prospect_email": "prospect@example.com",
                              "subject": "a subject", "body": BODY_A}},
            },
            agent_tasks.APPROVAL_REPLAY_KEY: {
                fp: {"started_at": "2026-10-12T06:30:00+00:00", "worker": "dead-worker:1"},
            },
        },
    )
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach_token(queue_db, task_id, token)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == E_ACTION_REPLAY_UNAVAILABLE, outcome
    assert provider.calls == [], "an interrupted replay must not be sent a second time"
    assert calls == []
    assert queue_db.row(task_id)["status"] == "rejected"


def test_a_partially_approved_park_sends_the_approved_item_and_keeps_the_rest_parked(
    queue_db, monkeypatch
):
    """Two gated items, ONE token: the approved one is sent, the other stays parked (no bypass)."""
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    approved_payload = {"action": "send", "subject": "first", "body": BODY_A, "to": "a@b.com"}
    other_payload = {"action": "send", "subject": "second", "body": BODY_B, "to": "c@d.com"}
    fp_a = fingerprint("external_reversible", "email", approved_payload)
    fp_b = fingerprint("external_reversible", "email", other_payload)
    task_id = _create_task(
        queue_db,
        title="F-14 partial approval",
        status="awaiting_approval",
        metadata={
            "approval_required": [
                {"action": "send", "ref": "a@b.com", "channel": "email", "fingerprint": fp_a},
                {"action": "send", "ref": "c@d.com", "channel": "email", "fingerprint": fp_b},
            ],
            agent_tasks.APPROVED_PAYLOAD_KEY: {
                fp_a: {"agent_type": "email_outreach", "action": "send", "ref": "a@b.com",
                       "channel": "email", "class": "external_reversible",
                       "payload": approved_payload,
                       "item": {"prospect_email": "a@b.com", "subject": "first",
                                "body": BODY_A}},
                fp_b: {"agent_type": "email_outreach", "action": "send", "ref": "c@d.com",
                       "channel": "email", "class": "external_reversible",
                       "payload": other_payload,
                       "item": {"prospect_email": "c@d.com", "subject": "second",
                                "body": BODY_B}},
            },
        },
    )
    token = issue_approval(SECRET.encode(), action_fingerprint=fp_a).token
    _attach_token(queue_db, task_id, token)
    calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_B, calls=calls, send=provider),
    )

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert [call["to"] for call in provider.calls] == ["a@b.com"], provider.calls
    assert provider.calls[0]["body"] == BODY_A
    assert outcome["status"] == "awaiting_approval", outcome
    assert [item["fingerprint"] for item in outcome["approval_required"]] == [fp_b]
    assert calls == [], "the unapproved item is not re-generated by the model"


def test_a_task_that_was_never_parked_is_unaffected_by_the_replay_leg(queue_db, monkeypatch):
    """A token supplied out of band on a fresh row keeps the pre-F-14 contract (no parked set)."""
    _configure_secret(monkeypatch, SECRET)
    provider = _install_email_provider(monkeypatch)
    calls = _install_run(
        monkeypatch,
        lambda calls: _email_delegate(BODY_A, calls=calls, send=provider),
    )
    task_id = _create_task(queue_db, title="fresh run", metadata={"approval_token": "0.abc"})

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval", outcome
    assert len(calls) == 1, "a row with no parked set still runs its agent"
    assert provider.calls == []


# ── F-21: a broker blip must not burn the company-local day ────────────────────


class FanOut:
    """The broker edge of `enqueue_created`: records the hand-off, can refuse it."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []
        self.refuse = False

    def delay(self, task_id: int) -> None:
        if self.refuse:
            raise ConnectionError("broker refused the publish")
        self.enqueued.append(int(task_id))

    def s(self, task_id: int):
        return ("sig", int(task_id))


def _install_fanout(monkeypatch) -> FanOut:
    rec = FanOut()
    import celery

    monkeypatch.setattr("celery_app.tasks.agent_tasks.run_agent_task", rec)

    def _group(sigs):
        def _apply() -> None:
            if rec.refuse:
                raise ConnectionError("broker refused the publish")
            rec.enqueued.extend(sig[1] for sig in sigs)

        return SimpleNamespace(apply_async=_apply)

    monkeypatch.setattr(celery, "group", _group)
    return rec


def _install_model(monkeypatch, *, result=None) -> list[dict]:
    calls: list[dict] = []

    def _spy(agent_type, task_dict, context):
        calls.append({"agent_type": agent_type})
        return result if result is not None else {"summary": f"plan for {agent_type}", "tasks": []}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return calls


def _pin_clock(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(settings, "company_timezone", "UTC")
    monkeypatch.setattr(settings, "morning_cycle_hour", 6)
    monkeypatch.setattr(daily_cycle, "now_utc", lambda: when)


def _seed_hour(queue_db, hour: int) -> None:
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with queue_db.factory() as db:
            db.add(CompanyConfig(name="Cycle Co", timezone="UTC", daily_cycle_hour=hour))
            await db.commit()

    queue_db.run(_go())


def _cycle_rows(queue_db, action: str) -> list[dict]:
    return queue_db.query(
        "SELECT id, action, summary, detail FROM activity_log WHERE action = ? ORDER BY id",
        (action,),
    )


def test_a_refused_fanout_rearms_the_day_and_the_cycle_still_records_itself(
    queue_db, monkeypatch
):
    """A broker blip at the day boundary must not burn the day (F-21).

    On the defect the exception aborts the cycle before its report, the claim stays committed and
    the window closes: the day is unrun and unrecoverable.
    """
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 6, 30, tzinfo=UTC))
    _seed_hour(queue_db, 6)
    model_calls = _install_model(monkeypatch)
    fanout = _install_fanout(monkeypatch)
    fanout.refuse = True

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "deferred", result
    assert result["reason"] == "broker_refused", result
    assert len(model_calls) == 1, "the cycle really ran its plan"
    assert queue_db.count("tasks") == 3, "the created rows survive the refusal"

    reports = queue_db.query("SELECT morning_plan FROM daily_reports ORDER BY id")
    assert reports and reports[0]["morning_plan"], (
        "the cycle's OWN record was skipped because the publish failed"
    )

    rearmed = _cycle_rows(queue_db, "cycle_rearmed")
    assert len(rearmed) == 1, f"the day was not re-armed: {rearmed!r}"
    detail = json.loads(rearmed[0]["detail"])
    assert detail["cycle"] == "morning"
    assert detail["day"] == "2026-10-12"
    assert detail["reason"] == "broker_refused"
    assert sorted(detail["task_ids"]) == sorted(range(1, 4))

    rows = queue_db.query("SELECT id, metadata FROM tasks ORDER BY id")
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        assert meta.get("rearmed_at"), f"task {row['id']} carries no re-arm marker: {meta!r}"
        assert meta.get("rearm_reason"), meta


def test_the_next_tick_of_the_same_day_finishes_the_handoff_exactly_once(queue_db, monkeypatch):
    """The catch-up: one re-dispatch of the stored ids, no second plan, and a third tick skips."""
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 6, 30, tzinfo=UTC))
    _seed_hour(queue_db, 6)
    model_calls = _install_model(monkeypatch)
    fanout = _install_fanout(monkeypatch)
    fanout.refuse = True
    first = daily_cycle.run_morning_cycle()

    assert first["status"] == "deferred", first
    assert fanout.enqueued == []

    # The next tick lands AFTER the cycle's hour window — the "day boundary" case F-21 names.
    fanout.refuse = False
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 8, 30, tzinfo=UTC))

    catch_up = daily_cycle.run_morning_cycle()

    assert catch_up["status"] == "recovered", catch_up
    assert sorted(fanout.enqueued) == [1, 2, 3], fanout.enqueued
    assert len(model_calls) == 1, (
        f"the catch-up re-planned the day ({len(model_calls)} model calls): {model_calls!r}"
    )
    assert queue_db.count("tasks") == 3, "the catch-up created a second set of rows"
    recovered = _cycle_rows(queue_db, "cycle_recovered")
    assert len(recovered) == 1, recovered
    detail = json.loads(recovered[0]["detail"])
    assert detail["cycle"] == "morning" and detail["day"] == "2026-10-12"
    assert sorted(detail["task_ids"]) == [1, 2, 3]
    assert json.loads(queue_db.row(1)["metadata"])["rearm_reason"] == "cycle_publish_refused", (
        "the catch-up keeps the re-arm marker the reconciler reads (it is not cleared by a "
        "successful hand-off: the claim is the proof of life)"
    )

    # A third tick of the same day: the day is finished, nothing is re-published or re-planned.
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 9, 30, tzinfo=UTC))
    third = daily_cycle.run_morning_cycle()

    assert third["status"] == "skipped", third
    assert third["reason"] == "outside_morning_window", third
    assert sorted(fanout.enqueued) == [1, 2, 3], "the day re-ran twice"
    assert len(model_calls) == 1
    assert len(_cycle_rows(queue_db, "cycle_recovered")) == 1


def test_a_catchup_is_bounded_to_its_own_company_local_day(queue_db, monkeypatch):
    """Yesterday's re-arm is not today's errand: the catch-up may not cross the local day."""
    from app.services.activity_service import log_activity

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 8, 30, tzinfo=UTC))
    _seed_hour(queue_db, 6)
    model_calls = _install_model(monkeypatch)
    fanout = _install_fanout(monkeypatch)

    async def _seed_rearm() -> None:
        async with queue_db.factory() as db:
            await log_activity(
                db,
                agent_type="orchestrator",
                action="cycle_rearmed",
                summary="morning cycle fan-out refused for 2026-10-11",
                level="warning",
                detail={"cycle": "morning", "day": "2026-10-11", "reason": "broker_refused",
                        "task_ids": [1, 2, 3], "claim_id": 1},
            )
            await db.commit()

    queue_db.run(_seed_rearm())

    result = daily_cycle.run_morning_cycle()

    assert result["status"] == "skipped", result
    assert result["reason"] == "outside_morning_window", result
    assert fanout.enqueued == [], "a yesterday re-arm must not be dispatched today"
    assert model_calls == []
    assert _cycle_rows(queue_db, "cycle_recovered") == []


def test_the_catchup_stops_at_the_second_refusal_and_stays_recoverable(queue_db, monkeypatch):
    """A second blip re-arms again (bounded, idempotent) instead of answering with a crash."""
    _pin_clock(monkeypatch, datetime(2026, 10, 12, 6, 30, tzinfo=UTC))
    _seed_hour(queue_db, 6)
    model_calls = _install_model(monkeypatch)
    fanout = _install_fanout(monkeypatch)
    fanout.refuse = True
    assert daily_cycle.run_morning_cycle()["status"] == "deferred"

    _pin_clock(monkeypatch, datetime(2026, 10, 12, 7, 30, tzinfo=UTC))
    second = daily_cycle.run_morning_cycle()

    assert second["status"] == "deferred", second
    assert second["reason"] == "broker_refused", second
    assert len(_cycle_rows(queue_db, "cycle_rearmed")) == 2
    assert len(model_calls) == 1, "a refused hand-off must never re-plan the day"
    assert queue_db.count("tasks") == 3
