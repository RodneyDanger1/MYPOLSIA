"""WAVE 7 / logic-debugger — AUTH state machines under injected failure (the reproducible matrix).

Scope: the two credential lifecycles the wave-7 mission names — the APPROVAL token (mint -> verify
-> spend -> re-park) and the WEBSOCKET ticket (mint -> handshake -> cap -> close -> reconnect) —
plus the session/rotation edges.  Every row asserts a NAMED outcome; nothing here calls a real
provider, logs in, or sends mail.

Rows (the matrix's A/B block):

  A1  approval token: accepted at exactly `exp`, refused one second later
  A2  approval token: an EXPIRED consume writes NO ledger row (a rejected token cannot burn one)
  A3  approval token: a replay is `already_used`, the ledger holds ONE row, and the stored key is a
      DIGEST (never the raw bearer)
  A4  approval token: a token minted for another fingerprint spends nothing
  A5  approval RE-PARK: an expired token on a parked row sends nothing, keeps the parked set, drops
      the dead token and re-parks by name; a FRESH token then sends exactly once
  B1  WS ticket: accepted at exactly `exp`, refused after it
  B2  WS ticket: a correctly-signed far-future expiry is still refused (the TTL bound)
  B3  WS ticket: rotating the API key retires every outstanding ticket; a new mint works
  B4  WS ticket reconnect: the SAME ticket is accepted again after its socket closed — same
      identity, same cap slot (a reconnect must not create a new identity or leak a slot)
"""
from __future__ import annotations

import urllib.parse
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import celery_app.tasks.agent_tasks as agent_tasks
from app.agents.policy_firewall import (
    approval_token_id,
    consume_approval,
    fingerprint,
    issue_approval,
    verify_approval,
)
from app.api.v1 import ws as ws_module
from app.config import settings
from app.models.approval import ConsumedApproval

SECRET = b"w7-approval-state-secret"
SECRET_TEXT = SECRET.decode()
FP = "send|email|prospect@example.com"


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """The worker legs publish activity through `events.get_redis`; no network in a unit test."""
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


# ── A1-A4: the approval-token machine ────────────────────────────────────────
@pytest.mark.asyncio
async def test_a1_a_token_is_accepted_at_its_expiry_second_and_refused_after(async_db_session):
    minted = issue_approval(SECRET, action_fingerprint=FP, ttl_s=30)

    assert verify_approval(
        SECRET, minted.token, action_fingerprint=FP, now=float(minted.expires_at)
    ), "the expiry second itself is inside the window (the documented comparison)"
    assert not verify_approval(
        SECRET, minted.token, action_fingerprint=FP, now=float(minted.expires_at + 1)
    ), "one second past `exp` the token is dead"
    assert not verify_approval(
        SECRET, minted.token, action_fingerprint="some-other-action", now=float(minted.expires_at)
    ), "scope is the fingerprint: a valid token cannot authorise a different action"


@pytest.mark.asyncio
async def test_a2_an_expired_consume_writes_no_ledger_row(async_db_session):
    minted = issue_approval(SECRET, action_fingerprint=FP, ttl_s=5)

    spent = await consume_approval(
        async_db_session, SECRET, minted.token, action_fingerprint=FP,
        now=float(minted.expires_at + 1), consumed_by="w7-test",
    )

    assert spent.ok is False and spent.reason == "expired"
    ledger = await async_db_session.scalar(select(func.count()).select_from(ConsumedApproval))
    assert ledger == 0, "a refused token must never burn a ledger row"


@pytest.mark.asyncio
async def test_a3_a_replay_spends_once_and_the_ledger_stores_a_digest(async_db_session):
    minted = issue_approval(SECRET, action_fingerprint=FP, ttl_s=60)

    first = await consume_approval(
        async_db_session, SECRET, minted.token, action_fingerprint=FP, consumed_by="w7-test-1"
    )
    replay = await consume_approval(
        async_db_session, SECRET, minted.token, action_fingerprint=FP, consumed_by="w7-test-2"
    )

    assert first.ok is True and first.reason == ""
    assert replay.ok is False and replay.reason == "already_used", (
        "a second spend is the named replay, never a second success"
    )
    rows = (await async_db_session.execute(select(ConsumedApproval))).scalars().all()
    assert len(rows) == 1, "one approval, one ledger row"
    assert rows[0].token_id == approval_token_id(minted.token)
    assert minted.token not in rows[0].token_id, "the bearer token is never stored verbatim"
    assert rows[0].consumed_by == "w7-test-1", "the WINNING spend is the recorded one"


@pytest.mark.asyncio
async def test_a4_a_token_for_another_fingerprint_spends_nothing(async_db_session):
    minted = issue_approval(SECRET, action_fingerprint=FP, ttl_s=60)

    spent = await consume_approval(
        async_db_session, SECRET, minted.token,
        action_fingerprint="refund|stripe|pi_someone_else", consumed_by="w7-test",
    )

    assert spent.ok is False and spent.reason == "invalid"
    ledger = await async_db_session.scalar(select(func.count()).select_from(ConsumedApproval))
    assert ledger == 0


# ── A5: the worker's re-park leg (queue_db: the real claim/replay path) ──────
class _ProviderRecorder:
    """Stands in ONLY for the provider hop of the guarded sender — the app's own send path runs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"ok": True, "result": {"message_id": f"sg-w7-{len(self.calls)}"}}


def _install_provider(monkeypatch) -> _ProviderRecorder:
    rec = _ProviderRecorder()
    monkeypatch.setattr("app.agents.email_outreach.compliance_send.send_outreach_email", rec)
    return rec


def _create_parked_task(queue_db, metadata: dict) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db, title="w7 parked approval", agent_type="email_outreach",
                source="scheduler", metadata=metadata, status="awaiting_approval",
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _attach_token(queue_db, task_id: int, token: str, fingerprint_value: str) -> None:
    from app.services.task_service import attach_approval_token

    async def _attach() -> None:
        async with queue_db.factory() as db:
            await attach_approval_token(db, task_id, token, action_fingerprint=fingerprint_value)
            await db.commit()

    queue_db.run(_attach())


def _metadata(queue_db, task_id: int) -> dict:
    import json

    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


def _parked_metadata(fp: str, payload: dict, item: dict) -> dict:
    return {
        "approval_required": [
            {"action": "send", "ref": item["prospect_email"], "channel": "email", "fingerprint": fp}
        ],
        agent_tasks.APPROVED_PAYLOAD_KEY: {
            fp: {
                "agent_type": "email_outreach",
                "action": "send",
                "ref": item["prospect_email"],
                "channel": "email",
                "class": "external_reversible",
                "payload": payload,
                "item": item,
            }
        },
    }


def test_a5_an_expired_token_reparks_sends_nothing_and_a_fresh_token_sends_once(
    queue_db, monkeypatch
):
    """The owner approves a parked row with a token whose 1 h window already ran out.

    The expiry is a NAMED refusal that sends nothing and BURNS nothing (the row keeps its parked
    set and drops the dead token), so the owner can mint a fresh token — the state machine's
    half-open case: parked again, not failed, not sent.
    """
    monkeypatch.setattr(settings, "approval_secret", SECRET_TEXT)
    monkeypatch.setattr(settings, "sandbox_mode", False)
    provider = _install_provider(monkeypatch)

    payload = {
        "action": "send", "subject": "w7", "body": "hello",
        "to": "prospect@example.com",
    }
    item = {"prospect_email": "prospect@example.com", "subject": "w7", "body": "hello"}
    fp = fingerprint("external_reversible", "email", payload)
    task_id = _create_parked_task(queue_db, _parked_metadata(fp, payload, item))

    expired = issue_approval(SECRET, action_fingerprint=fp, ttl_s=-60).token
    _attach_token(queue_db, task_id, expired, fp)

    first = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert provider.calls == [], "an expired approval must never reach the provider"
    row = queue_db.row(task_id)
    assert row["status"] == "awaiting_approval", row
    after = _metadata(queue_db, task_id)
    assert after["approval_required"][0]["fingerprint"] == fp, "the parked set is preserved"
    assert agent_tasks.APPROVAL_TOKEN_KEY not in after, "the dead token is dropped, not re-served"
    assert agent_tasks.APPROVAL_TOKEN_FINGERPRINT_KEY not in after
    assert first["status"] == "awaiting_approval", first
    assert queue_db.count("agent_runs") >= 1, "the run itself is on the ledger"

    # The owner mints a FRESH token: the exact same parked action now performs exactly once.
    fresh = issue_approval(SECRET, action_fingerprint=fp, ttl_s=600).token
    _attach_token(queue_db, task_id, fresh, fp)
    second = queue_db.run(agent_tasks._execute_agent_task(task_id))

    assert len(provider.calls) == 1, provider.calls
    assert provider.calls[0]["to"] == "prospect@example.com"
    assert queue_db.row(task_id)["status"] == "completed", second


# ── B1-B4: the websocket ticket machine ─────────────────────────────────────
KEY = "w7-ws-owner-key"


@pytest.fixture(autouse=True)
def _isolate_ws(monkeypatch):
    """Every test starts with a configured key and EMPTY socket/mint counters."""
    monkeypatch.setattr(settings, "api_key", KEY)
    monkeypatch.setattr(ws_module, "_open_sockets", {})
    monkeypatch.setattr(ws_module, "_operator_sockets", {})
    monkeypatch.setattr(ws_module, "_mint_times", {})


class _FakeWS:
    """The members `activity_socket` reads/writes, with the refusal frame recorded."""

    def __init__(self, *, ticket: str | None = None, key: str | None = None) -> None:
        self.query_params = {} if ticket is None else {"ticket": ticket}
        self.headers = {} if key is None else {"x-api-key": key}
        self.scope = {"query_string": urllib.parse.urlencode(self.query_params).encode()}
        self.accepted = False
        self.closed = False
        self.close_codes: list[int | None] = []
        self.close_reasons: list[str | None] = []
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        self.close_codes.append(code)
        self.close_reasons.append(reason)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def test_b1_a_ticket_is_accepted_at_its_expiry_second_and_refused_after():
    ticket, _ttl = ws_module.mint_ws_ticket()
    expires = int(ticket.split(".")[1])

    assert ws_module.parse_ws_ticket(ticket, now=float(expires)) is not None, (
        "the expiry second is inside the window (the documented comparison)"
    )
    assert ws_module.parse_ws_ticket(ticket, now=float(expires + 1)) is None
    assert ws_module.verify_ws_ticket(ticket, now=float(expires + 1)) is False


def test_b2_a_correctly_signed_far_future_expiry_is_still_refused():
    """A hand-rolled ticket cannot buy a longer window even with a valid signature."""
    now = int(__import__("time").time())
    honest = now + ws_module._TICKET_TTL_SECONDS + 500
    subject = "w7probe"
    forged = f"v2.{honest}.{subject}.{ws_module._ticket_signature(honest, subject)}"

    assert ws_module.parse_ws_ticket(forged, now=float(now)) is None, (
        "the TTL bound must reject an expiry further out than a mint could ever produce"
    )


def test_b3_rotating_the_api_key_retires_every_outstanding_ticket(monkeypatch):
    ticket, _ttl = ws_module.mint_ws_ticket()
    assert ws_module.verify_ws_ticket(ticket) is True

    monkeypatch.setattr(settings, "api_key", "w7-ws-rotated-key")
    assert ws_module.verify_ws_ticket(ticket) is False, (
        "the signing key is derived from the API key: a rotation must retire outstanding tickets"
    )

    rotated, _ttl2 = ws_module.mint_ws_ticket()
    assert ws_module.verify_ws_ticket(rotated) is True


@pytest.mark.asyncio
async def test_b4_the_same_ticket_reconnects_after_its_socket_closed(monkeypatch):
    """A reconnect is not a new identity: the slot is released, then re-taken by the same subject."""
    ticket, _ttl = ws_module.mint_ws_ticket()

    async def _noop(_ws) -> None:  # the relay is not this row's subject: accept -> teardown
        return None

    monkeypatch.setattr(ws_module, "_serve", _noop)

    first = _FakeWS(ticket=ticket)
    await ws_module.activity_socket(first)
    assert first.accepted is True
    assert ws_module._open_sockets == {}, "the closed socket released its slot"

    second = _FakeWS(ticket=ticket)
    await ws_module.activity_socket(second)
    assert second.accepted is True, "a reconnect inside the ticket's 30 s TTL is served"
    assert ws_module._open_sockets == {}, "and it released the slot again"
    assert ws_module._operator_sockets == {}
