"""WAVE 7 / logic-debugger — EMAIL state machines under injected failure (the reproducible matrix).

Rows (the matrix's E/C block).  The wave-7 tree changed under this lane (other seats landed the
reply-to-optout route and the follow-up suppression filter while this file was written), so every
row is asserted against the CURRENT bytes — two of them are the still-open ordering defects and are
registered as `xfail(strict=True)` with the matching `REQUESTS-W7.md` entry:

  E1  the footer's reply-to-opt-out grammar: narrow, quote-aware, and never a guess
  E2  an inbound "unsubscribe" reply stores the DURABLE suppression (status) and stamps the chain,
      in one transaction, with one audit row
  E3  a RE-DELIVERED inbound message (same provider id) is one row and does not re-stamp
  E4  the one-click link: stable/case-insensitive token, idempotent replay, tamper changes nothing
  E5  opt-out precedence in the guarded sender: a suppressed prospect is refused BEFORE the
      provider, in the DB mode
  E6  [OPEN] opt-out vs a stale run preflight snapshot: the snapshot wins today
  E7  [OPEN] inbound reply BEFORE the sent log exists: the log is born un-replied
  E8  opt-out outranks a due follow-up step: no task is minted for a suppressed prospect
  E9  purpose separation: an approval token is not an unsubscribe token and vice versa
  E10 the session-free (worker) send path also enforces suppression before the provider
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.agents.policy_firewall import issue_approval
from app.config import settings
from app.core.errors import E_POLICY_DENIED, E_VALIDATION, PolsiaError
from app.models.outreach import EmailLog, Prospect
from app.models.report import ActivityLog
from app.models.support import SupportInboxItem
from app.services import email_compliance as compliance
from app.services import outreach_service
from app.services import support_inbox_service as inbox

SECRET = "w7-email-secret"
ADDRESS = "ada@example.com"

COMPANY = SimpleNamespace(
    sender_legal_name="Meridian Labs, Inc.",
    sender_postal_address="1 Example Street, Springfield, IL 62701",
    website_url="https://example.test",
)


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def _configured_channel(monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", SECRET)
    monkeypatch.setattr(settings, "sandbox_mode", False)
    # the footer's identity gate reads the company row through this one function
    monkeypatch.setattr("app.services.company_service.get_company", _company)
    return None


async def _company(_db):
    return COMPANY


class _ProviderTripwire:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True, "result": {"message_id": "sg-w7-email"}}


@pytest.fixture()
def provider(monkeypatch) -> _ProviderTripwire:
    trip = _ProviderTripwire()
    monkeypatch.setattr("app.agents.integrations.send_email", trip)
    return trip


async def _seed_prospect(db, *, status: str = "new", email: str = ADDRESS) -> Prospect:
    prospect = Prospect(email=email, status=status, source="w7-test")
    db.add(prospect)
    await db.flush()
    return prospect


async def _seed_sent_log(db, prospect: Prospect, *, sendgrid_id: str = "sg-w7-seed") -> EmailLog:
    log = EmailLog(
        prospect_id=prospect.id,
        subject="hello",
        body="body",
        sendgrid_id=sendgrid_id,
        status="sent",
        sent_at=datetime.now(UTC).replace(tzinfo=None),
        follow_up_due=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
    )
    db.add(log)
    await db.flush()
    return log


# ── E1: the reply-to-opt-out grammar ────────────────────────────────────────
@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("unsubscribe", "", True),
        ("Re: Unsubscribe", "anything", True),
        ("", "Unsubscribe\n", True),
        ("", "please opt out of these emails", True),
        ("Re: our chat", "> unsubscribe\n> sent from my phone", False),
        ("Re: our chat", "Thanks — the note was useful.", False),
    ],
)
def test_e1_the_reply_opt_out_grammar_is_narrow_and_quote_aware(subject, body, expected):
    assert compliance.is_opt_out_reply(subject, body) is expected


# R3 CLOSED by truthseeker W7: is_opt_out_reply now STOPS at an attribution line, so a reply that
# quotes our own footer is not read as the customer's ask. The stale xfail (strict) marker was
# removed by the Lead; the assertion below is the fixed grammar and passes.
def test_e1b_quoted_history_under_an_attribution_is_not_the_customers_ask():
    quoted = "On Monday someone wrote:\nunsubscribe"
    assert compliance.is_opt_out_reply("Re: our chat", quoted) is False


# ── E2: the inbound opt-out becomes a DURABLE suppression ──────────────────
@pytest.mark.asyncio
async def test_e2_an_unsubscribe_reply_stores_the_opt_out_and_stamps_the_chain(async_db_session):
    prospect = await _seed_prospect(async_db_session)
    log = await _seed_sent_log(async_db_session, prospect)

    item = await inbox.submit_item(
        async_db_session,
        provider_message_id="pm-w7-optout-1",
        from_address="Ada@Example.COM",           # one mailbox, any casing
        subject="Re: unsubscribe",
        body="please take me off the list",
    )
    await async_db_session.commit()

    assert item is not None
    await async_db_session.refresh(prospect)
    await async_db_session.refresh(log)
    assert prospect.status == "unsubscribed", "the reply route must store suppression, not only a stamp"
    assert log.replied_at is not None, "the answered chain stops"

    audits = (
        await async_db_session.execute(
            select(ActivityLog).where(ActivityLog.action == inbox.OPT_OUT_AUDIT_ACTION)
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].detail["prospect_id"] == prospect.id
    assert audits[0].detail["item_id"] == item.id
    assert "ada@example.com" not in (audits[0].summary or "").lower(), "no address in the audit line"


# ── E3: inbound replay is one row, one stamp ───────────────────────────────
@pytest.mark.asyncio
async def test_e3_a_redelivered_inbound_message_is_one_row_and_does_not_restamp(async_db_session):
    prospect = await _seed_prospect(async_db_session)
    log = await _seed_sent_log(async_db_session, prospect)

    first = await inbox.submit_item(
        async_db_session, provider_message_id="pm-w7-dup-1",
        from_address=ADDRESS, subject="Re: hi", body="thanks",
    )
    await async_db_session.commit()
    await async_db_session.refresh(log)
    stamped_at = log.replied_at
    assert stamped_at is not None

    second = await inbox.submit_item(
        async_db_session, provider_message_id="pm-w7-dup-1",
        from_address=ADDRESS, subject="Re: hi (again)", body="thanks again",
    )
    await async_db_session.commit()

    assert second.id == first.id
    count = await async_db_session.scalar(select(func.count()).select_from(SupportInboxItem))
    assert count == 1
    await async_db_session.refresh(log)
    assert log.replied_at == stamped_at, "a duplicate delivery is not a new reply"
    assert second.body == "thanks", "a re-delivery never overwrites the stored message"


# ── E4: the one-click link ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_e4_the_unsubscribe_link_is_stable_case_insensitive_and_idempotent(async_db_session):
    token = compliance.unsubscribe_token("Ada@Example.COM")
    assert compliance.verify_unsubscribe_token(token) == ADDRESS, "one mailbox, one consent"

    first = await outreach_service.unsubscribe_prospect(async_db_session, "Ada@Example.COM")
    second = await outreach_service.unsubscribe_prospect(async_db_session, ADDRESS)
    await async_db_session.commit()

    assert first["created"] is True and first["already"] is False
    assert second["already"] is True, "a replayed link is the same state, not a second effect"
    count = await async_db_session.scalar(select(func.count()).select_from(Prospect))
    assert count == 1, "the mixed-case spelling must not create a second row"


@pytest.mark.asyncio
async def test_e4b_a_tampered_link_is_refused_and_changes_no_state(async_db_session):
    token = compliance.unsubscribe_token(ADDRESS)
    payload, signature = token.split(".")
    forged = f"{payload}.{'A' * len(signature)}"

    with pytest.raises(PolsiaError) as excinfo:
        compliance.verify_unsubscribe_token(forged)
    assert excinfo.value.code == E_VALIDATION
    count = await async_db_session.scalar(select(func.count()).select_from(Prospect))
    assert count == 0, "a refused token writes nothing"


# ── E5: opt-out precedence in the DB send mode ─────────────────────────────
@pytest.mark.asyncio
async def test_e5_a_suppressed_prospect_is_refused_before_the_provider(async_db_session, provider):
    await _seed_prospect(async_db_session, status="unsubscribed")

    with pytest.raises(PolsiaError) as excinfo:
        await compliance.send_compliant_email(
            async_db_session, to=ADDRESS, subject="hello", body="body"
        )

    assert excinfo.value.code == E_POLICY_DENIED
    assert provider.calls == [], "the provider must never be reachable for a suppressed address"


# ── E6: [OPEN] opt-out vs a stale run preflight snapshot ───────────────────
# R1/R2/R4 CLOSED by the Lead (wave 7): the send-time verification, reply-before-log and
# legacy-duplicate suppression fixes are in; the stale strict xfail was removed.
@pytest.mark.asyncio
async def test_e6_an_opt_out_during_a_run_beats_the_stale_preflight(async_db_session, provider):
    await _seed_prospect(async_db_session, status="new")
    stale = await outreach_service.compliance_preflight(async_db_session)
    assert ADDRESS not in stale["suppressed"]

    # the recipient opts out while the run is in flight
    await outreach_service.unsubscribe_prospect(async_db_session, ADDRESS)
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await compliance.send_compliant_email(
            None, to=ADDRESS, subject="hello", body="body", preflight=stale
        )
    assert excinfo.value.code == E_POLICY_DENIED
    assert provider.calls == []


# ── E7: [OPEN] the inbound reply that arrives BEFORE the log exists ────────
# R1/R2/R4 CLOSED by the Lead (wave 7): the send-time verification, reply-before-log and
# legacy-duplicate suppression fixes are in; the stale strict xfail was removed.
@pytest.mark.asyncio
async def test_e7_a_reply_that_precedes_the_sent_log_is_not_forgotten(async_db_session):
    await _seed_prospect(async_db_session)
    # the reply lands FIRST (a fast auto-responder), so there is no log to stamp yet
    await inbox.submit_item(
        async_db_session, provider_message_id="pm-w7-order-1",
        from_address=ADDRESS, subject="Re: hello", body="got it — happy to talk",
    )
    await async_db_session.commit()

    report = await outreach_service.record_sent_emails(
        async_db_session,
        {"emails": [{
            "prospect_email": ADDRESS, "subject": "hello", "body": "body",
            "status": "sent", "sendgrid_id": "sg-w7-order-1", "provider_status": 202,
        }]},
    )
    await async_db_session.commit()
    assert report["ok"] is True, report
    log = (await async_db_session.execute(select(EmailLog))).scalars().one()
    assert log.replied_at is not None, "the already-arrived reply must stamp the row it answers"
    assert log.follow_up_due is None, "no follow-up may be due for a prospect who replied"


# ── E8: the follow-up sweep never mints work for a suppressed prospect ─────
def test_e8_a_suppressed_prospect_gets_no_follow_up_task(queue_db):
    from celery_app.tasks.followups import _run_follow_up_sweep

    async def _seed() -> None:
        async with queue_db.factory() as db:
            prospect = Prospect(email=ADDRESS, status="unsubscribed", source="w7-test")
            db.add(prospect)
            await db.flush()
            db.add(EmailLog(
                prospect_id=prospect.id, subject="hello", body="body",
                sendgrid_id="sg-w7-suppressed-1", status="sent",
                sent_at=datetime.now(UTC).replace(tzinfo=None),
                follow_up_due=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
            ))
            await db.commit()

    queue_db.run(_seed())
    result = queue_db.run(_run_follow_up_sweep())

    assert result["due"] == 0
    assert result.get("suppressed") == 1, "the withheld step is COUNTED, never silently dropped"
    assert result["created"] == 0
    assert queue_db.count("tasks") == 0, "no paid run may be minted for a suppressed address"


# ── E11: [OPEN] a legacy mixed-case duplicate must not hide the opt-out ────
# R1/R2/R4 CLOSED by the Lead (wave 7): the send-time verification, reply-before-log and
# legacy-duplicate suppression fixes are in; the stale strict xfail was removed.
@pytest.mark.asyncio
async def test_e11_a_legacy_mixed_case_duplicate_does_not_hide_the_opt_out(async_db_session, provider):
    subscribed = Prospect(email="Ada@Example.COM", status="new", source="legacy")
    opted_out = Prospect(email="ada@example.com", status="unsubscribed", source="unsubscribe-link")
    async_db_session.add_all([subscribed, opted_out])
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as excinfo:
        await compliance.send_compliant_email(
            async_db_session, to="ada@example.com", subject="hello", body="body"
        )
    assert excinfo.value.code == E_POLICY_DENIED
    assert provider.calls == []


# ── E9: purpose separation between the two token families ──────────────────
@pytest.mark.asyncio
async def test_e9_an_approval_token_is_not_an_unsubscribe_token(async_db_session):
    approval = issue_approval(SECRET.encode(), action_fingerprint="send|email|" + ADDRESS).token
    with pytest.raises(PolsiaError) as excinfo:
        compliance.verify_unsubscribe_token(approval)
    assert excinfo.value.code == E_VALIDATION, "the opt-out route must never accept a bearable approval"

    link = compliance.unsubscribe_token(ADDRESS)
    from app.agents.policy_firewall import verify_approval

    assert verify_approval(SECRET.encode(), link, action_fingerprint="send|email|" + ADDRESS) is False, (
        "and the approval verifier must never accept an opt-out link"
    )


# ── E10: the session-free (worker) send path enforces suppression too ──────
@pytest.mark.asyncio
async def test_e10_the_worker_path_refuses_a_suppressed_address_before_the_provider(provider):
    preflight = {
        "company": {
            "sender_legal_name": COMPANY.sender_legal_name,
            "sender_postal_address": COMPANY.sender_postal_address,
            "website_url": COMPANY.website_url,
        },
        "suppressed": [ADDRESS],
    }

    with pytest.raises(PolsiaError) as excinfo:
        await compliance.send_compliant_email(
            None, to=ADDRESS, subject="hello", body="body", preflight=preflight
        )

    assert excinfo.value.code == E_POLICY_DENIED
    assert provider.calls == []
