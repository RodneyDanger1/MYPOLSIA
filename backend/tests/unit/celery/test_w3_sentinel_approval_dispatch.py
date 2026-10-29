"""W3 / SENTINEL — R-W2-S2: the approval token is SPENT at the worker dispatch seam.

THE GAP THIS FILE PINS (taskmaster W2 §7, §13.6; sentinel W2 REQUESTS §R-W2-S2).  Every other
irreversible seam SPENDS an approval through the `consumed_approvals` ledger (deploy, data wipe,
phone), but the WORKER only VERIFIED it: `set_run_approval` published the token and
`_replay_approved_actions`/`side_effects` re-verified it per item.  So one token could authorise the
same irreversible agent action twice — on a re-delivery, or on ANOTHER row naming the same
fingerprint (a fingerprint is a hash of class|channel|payload, so two rows can name one action).

WHAT LANDS HERE
  * `attach_approval_token` PERSISTS the token→fingerprint binding (`metadata.approval_token_fingerprint`)
    and the approvals route passes the minted fingerprint;
  * `_spend_approval_before_dispatch` SPENDS the token (ledger UNIQUE `token_id`) BEFORE the run row
    exists and BEFORE any provider is reachable; `already_used` refuses the dispatch by name
    (`MERIDIAN-E121`) and the row re-parks asking for a fresh mint; a ledger write that fails for any
    other reason is the named `E_DB` (fail closed — the action is not dispatched);
  * ONE APPROVAL = ONE ATTEMPT: a spent token is never restored by the OR-02 settlement, so a retry
    needs a fresh mint (the fail-safe direction the deploy seam ratified).

WHAT IS DELIBERATELY NOT SPENT (pinned below, so a future reader does not "fix" it into a hole):
a token that verifies for NO action the row is waiting on authorises nothing (nothing is burned, the
per-item gate still refuses it); a token that verifies for SEVERAL parked fingerprints is not spent
either — that is only reachable under a test double, and burning a guess could kill an approval for
the wrong action.

RED LEG: with the `_spend_approval_before_dispatch` call removed (the pre-fix behaviour) the replay
tests below fail with the second send actually happening — see `raw/w3/RED-*.txt`.
"""
from __future__ import annotations

import json

import pytest

import app.agents.crew_factory as crew_factory
import app.config as app_config
from app.agents.policy_firewall import (
    fingerprint,
    issue_approval,
    reset_run_approval,
)
from app.config import settings
from app.core.errors import E_DB, E_INTEGRATION, E_POLICY_DENIED, MeridianError
from app.models.task import TaskStatus
from celery_app.tasks import agent_tasks

SECRET = "w3-sentinel-dispatch-secret"
BODY_A = "the approved body sent once per approval"
BODY_B = "a second approved body"
#: The named policy code a REPLAY carries (the ledger's own refusal, not "approval required").
MERIDIAN_E121 = "MERIDIAN-E121"


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_redis_events(monkeypatch):
    """The worker legs publish activity through `events.get_redis` (the celery conftest's own
    autouse guard covers this directory; this is belt and braces, as in the OR-02 file)."""
    from unittest.mock import AsyncMock

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


def _configure_secret(monkeypatch, value: str = SECRET) -> None:
    monkeypatch.setattr(settings, "approval_secret", value)


# ── the parked row: a fingerprint, its stored payload, and the record the owner sees ──────────


def _payload(subject: str, to: str, body: str = BODY_A) -> dict:
    return {"action": "send", "subject": subject, "body": body, "to": to}


def _record(subject: str, to: str, body: str = BODY_A) -> dict:
    return {
        "agent_type": "email_outreach",
        "action": "send",
        "ref": to,
        "channel": "email",
        "class": "external_reversible",
        "value_usd": 0.0,
        "payload": _payload(subject, to, body),
        "item": {"prospect_email": to, "subject": subject, "body": body},
    }


def _parked_metadata(*items: tuple[str, ...]) -> tuple[dict, list[str]]:
    """(metadata, fingerprints) for one or more parked email sends: `(subject, to[, body])`."""
    records = [_record(item[0], item[1], item[2] if len(item) > 2 else BODY_A) for item in items]
    fingerprints = [
        fingerprint("external_reversible", "email", record["payload"]) for record in records
    ]
    metadata = {
        "approval_required": [
            {"action": "send", "ref": record["ref"], "channel": "email",
             "fingerprint": fp, "reason": "context tainted"}
            for record, fp in zip(records, fingerprints, strict=True)
        ],
        agent_tasks.APPROVED_PAYLOAD_KEY: dict(zip(fingerprints, records, strict=True)),
    }
    return metadata, fingerprints


def _create_parked_task(queue_db, metadata: dict) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db,
                title="W3 dispatch spend",
                agent_type="email_outreach",
                source="scheduler",
                status="awaiting_approval",
                metadata=metadata,
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _attach(queue_db, task_id: int, token: str, fingerprint_value: str = "") -> None:
    from app.services.task_service import attach_approval_token

    async def _attach_() -> None:
        async with queue_db.factory() as db:
            await attach_approval_token(
                db, task_id, token, action_fingerprint=fingerprint_value
            )
            await db.commit()

    queue_db.run(_attach_())


def _metadata(queue_db, task_id: int) -> dict:
    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


def _ledger(queue_db) -> list[dict]:
    return queue_db.query(
        "SELECT token_id, jti, action_fingerprint, consumed_by FROM consumed_approvals ORDER BY id"
    )


def _activity_actions(queue_db) -> list[str]:
    return [
        row["action"]
        for row in queue_db.query("SELECT action FROM activity_log ORDER BY id")
    ]


# ── the provider / model doubles (the guarded sender's LAST hop, never the code under test) ────


class Provider:
    """The provider side of a REAL delivered email; can fail the N-th call pre-send."""

    def __init__(self, *, fail_first: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_first = fail_first
        self.failed = False

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        if self.fail_first is not None and not self.failed:
            self.failed = True
            raise self.fail_first
        return {"ok": True, "result": {"message_id": f"sg-w3-{len(self.calls)}"}}

    def sends_to(self, address: str) -> int:
        return sum(1 for call in self.calls if call.get("to") == address)


def _install_provider(monkeypatch, provider: Provider) -> Provider:
    monkeypatch.setattr("app.agents.email_outreach.compliance_send.send_outreach_email", provider)
    return provider


def _install_model(monkeypatch) -> list[dict]:
    """Record every `run_agent_for_task` call: the resume must never ask the model again."""
    calls: list[dict] = []

    def _spy(agent_type, task_dict, context):  # noqa: ANN001
        calls.append({"agent_type": agent_type, "task_id": task_dict.get("id")})
        return {"summary": "the model was asked to re-generate an approved body"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", _spy)
    return calls


def _accept_every_token(monkeypatch) -> None:
    """A verify double that accepts anything (the ONLY way to get an ambiguous match)."""
    monkeypatch.setattr(
        "app.agents.policy_firewall.verify_approval", lambda *args, **kwargs: True
    )


# ── 1. the spend: a bound token is spent at dispatch, before the effect ───────────────────────


def test_a_bound_token_is_spent_at_dispatch_and_records_what_it_authorised(queue_db, monkeypatch):
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    model_calls = _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token, fp)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "completed", (outcome, queue_db.row(task_id)["error_message"])
    assert model_calls == [], "the resume asked the model instead of replaying the stored payload"
    assert provider.sends_to("first@example.com") == 1
    rows = _ledger(queue_db)
    assert len(rows) == 1, f"the approval was not spent at dispatch: {rows!r}"
    assert rows[0]["action_fingerprint"] == fp
    assert rows[0]["consumed_by"] == f"task:{task_id}"
    assert rows[0]["jti"], "the ledger row must name the nonce that was spent"


def test_the_same_token_on_another_row_naming_the_same_action_is_refused(queue_db, monkeypatch):
    """THE replay the ledger closes: a fingerprint is a hash of the payload, so two rows can name
    the SAME action.  The second row must not perform it again."""
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    first_task = _create_parked_task(queue_db, metadata)
    second_task = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, first_task, token, fp)

    first = queue_db.run(agent_tasks._execute_agent_task(first_task))
    assert first["status"] == "completed", first

    # The SAME token is attached to the second row (a copy of the metadata is all a replayer needs).
    _attach(queue_db, second_task, token, fp)
    second = queue_db.run(agent_tasks._execute_agent_task(second_task))

    assert second["status"] == "skipped", second
    assert second["reason"] == E_POLICY_DENIED, second
    assert second["policy_code"] == "MERIDIAN-E121", second
    assert second["approval_replay"] is True, second
    assert second["task_status"] == "awaiting_approval", second
    assert provider.sends_to("first@example.com") == 1, (
        f"the replay performed the same approved action twice: {provider.calls!r}"
    )
    assert len(_ledger(queue_db)) == 1, "a replay must not write a second ledger row"
    assert "approval_replay_refused" in _activity_actions(queue_db)
    row = queue_db.row(second_task)
    assert MERIDIAN_E121 in (row["error_message"] or "")
    after = _metadata(queue_db, second_task)
    assert "approval_token" not in after, "a spent token must not ride a later re-arm"
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY not in after
    assert [item["fingerprint"] for item in after["approval_required"]] == [fp], after


# ── 2. a re-delivery of the same row (the worker-level replay) ────────────────────────────────


def _rearm_with_token(queue_db, task_id: int, token: str, fingerprint_value: str) -> None:
    """Put a spent token back on a claimable row, with the parked set restored and the send-once
    marker cleared the way the operator's documented recovery clears it."""

    async def _rearm() -> None:
        from app.services.task_service import get_task

        async with queue_db.factory() as db:
            row = await get_task(db, task_id)
            merged = dict(row.metadata_json or {})
            merged["approval_token"] = token
            merged[agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY] = fingerprint_value
            merged[agent_tasks.APPROVED_PAYLOAD_KEY] = {
                fingerprint_value: _record("first", "first@example.com")
            }
            merged[agent_tasks.APPROVAL_REQUIRED_KEY] = [{
                "action": "send", "ref": "first@example.com", "channel": "email",
                "fingerprint": fingerprint_value, "reason": "context tainted",
            }]
            merged.pop(agent_tasks.APPROVAL_REPLAY_KEY, None)
            row.metadata_json = merged
            row.status = TaskStatus.PENDING
            row.claimed_by = None
            row.claimed_at = None
            row.heartbeat_at = None
            await db.commit()

    queue_db.run(_rearm())


def test_a_redelivery_carrying_a_spent_token_parks_the_row_for_a_fresh_mint(
    queue_db, monkeypatch
):
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token, fp)
    assert queue_db.run(agent_tasks._execute_agent_task(task_id))["status"] == "completed"

    _rearm_with_token(queue_db, task_id, token, fp)
    replay = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert replay["status"] == "skipped", replay
    assert replay["policy_code"] == "MERIDIAN-E121", replay
    assert replay["task_status"] == "awaiting_approval", replay
    assert provider.sends_to("first@example.com") == 1, provider.calls
    assert queue_db.row(task_id)["status"] == "awaiting_approval"
    assert "approval_token" not in _metadata(queue_db, task_id)

    # A FRESH mint resumes the owed action — one approval, one attempt, the owner decides again.
    fresh = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, fresh, fp)
    resumed = queue_db.run(agent_tasks._execute_agent_task(task_id))
    assert resumed["status"] == "completed", (resumed, queue_db.row(task_id)["error_message"])
    assert provider.sends_to("first@example.com") == 2, provider.calls
    assert len(_ledger(queue_db)) == 2, "each mint that performed an action is its own spend"


# ── 3. what is deliberately NOT spent ─────────────────────────────────────────────────────────


def test_a_token_that_does_not_verify_is_not_spent_and_the_row_reparks(queue_db, monkeypatch):
    """An expired token authorises nothing: nothing is burned, and the shipped re-park stands."""
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    expired = issue_approval(SECRET.encode(), action_fingerprint=fp, ttl_s=-60).token
    _attach(queue_db, task_id, expired, fp)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "awaiting_approval", outcome
    assert _ledger(queue_db) == [], "an unverifiable token must never write a ledger row"
    assert provider.calls == [], provider.calls
    after = _metadata(queue_db, task_id)
    assert [item["fingerprint"] for item in after["approval_required"]] == [fp]


def test_an_unbound_token_is_spent_against_the_one_parked_action_it_verifies_for(
    queue_db, monkeypatch
):
    """Legacy/out-of-band attach (no binding): the worker derives the ONE fingerprint and spends."""
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token)          # no fingerprint handed over

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "completed", (outcome, queue_db.row(task_id)["error_message"])
    assert provider.sends_to("first@example.com") == 1, provider.calls
    rows = _ledger(queue_db)
    assert len(rows) == 1 and rows[0]["action_fingerprint"] == fp, rows


def test_an_ambiguous_token_is_never_burned_on_a_guess(queue_db, monkeypatch):
    """A token that verifies for SEVERAL parked fingerprints is not spent (a real token cannot)."""
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    _install_model(monkeypatch)
    _accept_every_token(monkeypatch)
    metadata, fps = _parked_metadata(
        ("first", "first@example.com"), ("second", "second@example.com", BODY_B)
    )
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fps[0]).token
    _attach(queue_db, task_id, token)          # no binding -> the worker must not guess

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert _ledger(queue_db) == [], "an ambiguous action binding must not burn an approval"
    assert outcome["status"] in ("completed", "awaiting_approval"), outcome
    # The verify double let both items through: the effects happen, and NO approval was burned for
    # them — which is the point (a real token can never match two fingerprints).
    assert len(provider.calls) == 2, provider.calls


# ── 4. fail closed: the ledger cannot record the spend ───────────────────────────────────────


def test_a_ledger_write_failure_refuses_the_dispatch_and_requeues_the_row(queue_db, monkeypatch):
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    model_calls = _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token, fp)

    def _refuse_ledger(*args, **kwargs):  # noqa: ANN002, ANN003
        raise MeridianError(E_DB, "the consumed_approvals table is unwritable (probe)")

    monkeypatch.setattr("app.agents.policy_firewall.consume_approval", _refuse_ledger)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == E_DB, outcome
    assert outcome["task_status"] == "pending", outcome
    assert outcome.get("requeued_for_retry") is True, outcome
    assert provider.calls == [], "the approved action must not be dispatched on an unproven spend"
    assert model_calls == []
    assert queue_db.query("SELECT COUNT(*) AS c FROM agent_runs")[0]["c"] == 0, (
        "a refused dispatch must not open a run row"
    )
    row = queue_db.row(task_id)
    assert row["status"] == "pending", row
    assert E_DB in (row["error_message"] or "")
    after = _metadata(queue_db, task_id)
    assert after.get("approval_token") == token, "an unspent token stays on the row"
    assert after.get("rearmed_at"), "the row must be marked for the stranded-row reconciler"
    assert [item["fingerprint"] for item in after["approval_required"]] == [fp]
    assert "approval_ledger_unwritable" in _activity_actions(queue_db)


def test_a_foreign_claim_is_described_truthfully_never_requeued_by_this_delivery(
    queue_db, monkeypatch
):
    """W6-S4 (wave 7): the E_DB envelope must describe the ROW, not the happy path.

    OLD behaviour (pinned by the test above for the OWNED-claim branch): the refusal payload
    hardcoded `task_status="pending"` and `requeued_for_retry=True`.  That is true only when THIS
    worker still holds the claim and the release lands.  Wave 6 measured the other branch on the
    live image (W5-S2 takeover, `approval-edb-edb_foreign.json`): the same constants came back
    while the row actually stayed `in_progress` under the other worker — a caller reading them
    would believe a row this delivery does not own had been requeued by it.

    THIS TEST drives the branch through the REAL `release_claim`: `worker_id()` is stateful, so the
    claim at the top of `_execute_agent_task` is ours and the ONE call inside the spend seam is a
    different id — exactly the shape of a takeover while a delivery is in flight.  The release then
    raises the product's own `E_TASK_CLAIMED`, and NO claim write is attempted (the W5-S2 guard).

    RED LEG (wave 7): restore `"task_status": "pending", "requeued_for_retry": True` in
    `agent_tasks._spend_approval_before_dispatch` and this test fails on the first assertion pair
    with `task_status='pending'` / `requeued_for_retry=True` for an `in_progress` row (raw output in
    `sentinel/raw/w7/RED-w6s4-task-status.txt`).
    """
    _configure_secret(monkeypatch)
    provider = _install_provider(monkeypatch, Provider())
    model_calls = _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token, fp)

    def _refuse_ledger(*args, **kwargs):  # noqa: ANN002, ANN003
        raise MeridianError(E_DB, "the consumed_approvals table is unwritable (probe)")

    monkeypatch.setattr("app.agents.policy_firewall.consume_approval", _refuse_ledger)

    # The takeover: call 1 is the claim (ours), call 2 is the spend seam's `worker_id()` (theirs).
    claim_ids = {"n": 0}

    def _takeover() -> str:
        claim_ids["n"] += 1
        return "sentinel-w7-owner" if claim_ids["n"] == 1 else "sentinel-w7-other-worker"

    monkeypatch.setattr(agent_tasks, "worker_id", _takeover)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == E_DB, outcome
    assert outcome["task_status"] == "in_progress", (
        "the envelope must report the row's ACTUAL status under a foreign claim, not 'pending': "
        f"{outcome}"
    )
    assert outcome["requeued_for_retry"] is False, (
        "this delivery did not requeue a row another worker holds: " f"{outcome}"
    )
    assert outcome["claim_released"] is False, outcome
    assert provider.calls == [], "the approved action must not be dispatched on an unproven spend"
    assert model_calls == []
    assert queue_db.query("SELECT COUNT(*) AS c FROM agent_runs")[0]["c"] == 0, (
        "a refused dispatch must not open a run row"
    )
    row = queue_db.row(task_id)
    assert row["status"] == "in_progress", row
    assert row["claimed_by"] == "sentinel-w7-owner", (
        "W5-S2: the other worker's claim must not be overwritten by the failed release"
    )
    assert row["error_message"].startswith(E_DB), row
    after = _metadata(queue_db, task_id)
    assert after.get("approval_token") == token, "an unspent token stays on the row"
    assert after.get("rearmed_at"), "the rearm stamp is written even when the release is refused"
    assert [item["fingerprint"] for item in after["approval_required"]] == [fp]
    assert "approval_ledger_unwritable" in _activity_actions(queue_db)


# ── 5. one attempt: the spent token is never restored by the OR-02 settlement ────────────────


def test_a_spent_token_is_not_restored_to_a_requeued_row(queue_db, monkeypatch):
    _configure_secret(monkeypatch)
    provider = _install_provider(
        monkeypatch, Provider(fail_first=MeridianError(E_INTEGRATION, "provider answered 503"))
    )
    _install_model(monkeypatch)
    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    token = issue_approval(SECRET.encode(), action_fingerprint=fp).token
    _attach(queue_db, task_id, token, fp)

    outcome = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["requeued"] is True, outcome
    assert len(provider.calls) == 1, "the sender was attempted once (and refused pre-send)"
    assert len(_ledger(queue_db)) == 1, "the attempt spent the approval"
    after = _metadata(queue_db, task_id)
    assert "approval_token" not in after, (
        "a SPENT token was restored to the retry — one approval must authorise one attempt"
    )
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY not in after
    assert [item["fingerprint"] for item in after["approval_required"]] == [fp], (
        "the owed action must stay parked so the owner can mint a fresh approval"
    )


# ── 6. the binding itself (attach + clear) and the two module keys ───────────────────────────


def test_the_binding_and_the_keys_agree_and_attach_writes_both(queue_db, monkeypatch):
    from app.services.task_service import (
        APPROVAL_TOKEN_FINGERPRINT_METADATA_KEY,
        APPROVAL_TOKEN_METADATA_KEY,
    )

    assert APPROVAL_TOKEN_FINGERPRINT_METADATA_KEY == agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY, (
        "the producer (task_service) and the consumer (agent_tasks) must name the same key"
    )
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY != APPROVAL_TOKEN_METADATA_KEY

    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    _attach(queue_db, task_id, "0.abc", fp)
    stored = _metadata(queue_db, task_id)
    assert stored[agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY] == fp

    # A later attach WITHOUT a fingerprint must remove the stale binding, never keep it.  (The
    # row is re-parked first: `attach_approval_token` only writes onto `awaiting_approval` rows.)
    async def _re_park() -> None:
        from app.services.task_service import get_task

        async with queue_db.factory() as db:
            row = await get_task(db, task_id)
            row.status = TaskStatus.AWAITING_APPROVAL
            await db.commit()

    queue_db.run(_re_park())
    _attach(queue_db, task_id, "0.def")
    stored = _metadata(queue_db, task_id)
    assert stored[agent_tasks.APPROVAL_TOKEN_KEY] == "0.def"
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY not in stored


def test_a_terminal_write_drops_the_token_and_its_binding_together(queue_db):
    from app.services.task_service import update_task_status

    metadata, (fp,) = _parked_metadata(("first", "first@example.com"))
    task_id = _create_parked_task(queue_db, metadata)
    _attach(queue_db, task_id, "0.abc", fp)
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY in _metadata(queue_db, task_id)

    async def _complete() -> None:
        async with queue_db.factory() as db:
            await update_task_status(db, task_id, "completed", result_summary="done")
            await db.commit()

    queue_db.run(_complete())
    stored = _metadata(queue_db, task_id)
    assert agent_tasks.APPROVAL_TOKEN_KEY not in stored
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY not in stored, (
        "a binding without its token would pair a later token with an ended approval"
    )
