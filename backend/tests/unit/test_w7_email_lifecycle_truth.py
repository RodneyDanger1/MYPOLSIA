"""WAVE 7 — the email lifecycle's DATA TRUTH: no fake "sent", one mailbox one row, no lost find.

The state model this file pins (`app/services/email_lifecycle.py` is its one owner):

    draft   -> `agent_runs.output.emails[].status` (never an `email_logs` row)
    queued  -> a `tasks` row under `followup:<log id>` (the follow-up the chain will fire)
    sent    -> `email_logs.status='sent'` + the provider's own message id
    refused -> `agent_runs.output.refusals[]` (+ the writer's named skip lists)
    inbound -> `support_inbox_items` (+ one audit row per delivery)
    reply   -> `email_logs.replied_at` (+ the opt-out audit row when the reply says so)
    follow-up -> `email_logs.follow_up_due` (stated cadence only)

WHAT IS PROVEN HERE, one defect per group (every leg is a real service call on the real schema;
doubles are the provider SDK and the settings seam only — AGENTS.md §7):

1. **A row that states no status is a DRAFT, never a send.**  `email_logs.status` shipped with
   `server_default="sent"`, so any writer that forgot a status fabricated a performed send that
   the email cap counts.  The default is `draft` now (migration 0019), and the cap sees 0.
2. **"sent" requires provider acceptance.**  `services.email_lifecycle.provider_accepted` is the
   one rule (a stated 2xx, else the provider's message id); the agent records `failed` with a
   named refusal on a 4xx, and the writer REFUSES a `sent` claim whose stated provider status is
   not 2xx — so neither an agent nor a replayed output can put a rejected call in the ledger.
3. **The same message twice is one row** (the provider id is the natural key), and the same
   message persisted twice through the found-prospects leg is one prospect.
4. **One mailbox is one row**: two casings of one address are one prospect, so the suppression
   list cannot be sidestepped by casing.
5. **A "find" run writes its prospects** — the agent's first duty used to be a lost write.
6. **The footer's reply-to-opt-out promise is executable**: an inbound reply whose first
   substantive line says "unsubscribe" records the durable opt-out; a quoted footer does not.

RED legs for 1/2/3/5 are in the lane's `raw-w7/RED-*.xml` (each one restores the pre-fix line and
shows the named failure).
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

import app.config as app_config
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.core.errors import E_INTEGRATION, E_POLICY_DENIED, E_VALIDATION, MeridianError
from app.models.company import CompanyConfig
from app.models.outreach import EmailLog, Prospect
from app.schemas.agent_outputs import EmailDraft
from app.services import outreach_service
from app.services.channel_service import channel_sends_snapshot
from app.services.email_compliance import is_opt_out_reply, send_compliant_email
from app.services.email_lifecycle import (
    EMAIL_ATTEMPT_STATUSES,
    EMAIL_ROW_DEFAULT_STATUS,
    EMAIL_ROW_STATUSES,
    provider_accepted,
    provider_message_id,
    provider_status_code,
)
from app.services.support_inbox_service import OPT_OUT_AUDIT_ACTION, submit_item

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)

LEGAL_NAME = "Acme Robotics LLC"
POSTAL_ADDRESS = "123 Main St, Minneapolis, MN 55401, USA"
WEBSITE = "https://acme.example"
SECRET = "unit-test-approval-secret"

_COMPLIANCE = {
    "company": {
        "sender_legal_name": LEGAL_NAME,
        "sender_postal_address": POSTAL_ADDRESS,
        "website_url": WEBSITE,
    },
    "suppressed": [],
    # W7/R1 contract: this fixture stands in for a producer that resolved suppression for
    # THIS send (the session-free path refuses an unverified snapshot by name).
    "send_time_verified": True,
}


# ── doubles (provider SDK + settings + clock only) ──────────────────────────────────────────────


class _FakeSendGrid:
    """Provider double with a CONFIGURABLE status code — the acceptance question's whole point.

    `status=202` + an id is the real happy shape.  `status=400`, returned WITHOUT raising, is the
    hostile shape: the adapter call "succeeded" (integration_call returns ok=True) while the
    provider in fact refused the message.  That is where a fake "sent" came from.
    """

    def __init__(self, *, status: int = 202, send_ids: bool = True) -> None:
        self.sent_message_ids: list[str] = []
        self.status = status
        self._send_ids = send_ids

    def install(self, monkeypatch: Any) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = fake.status
                self.headers = {"X-Message-Id": message_id} if fake._send_ids else {}

        class _Client:
            def __init__(self, api_key: str) -> None:
                self.api_key = api_key

            def send(self, message: Any) -> _Response:
                message_id = f"sg-msg-{len(fake.sent_message_ids) + 1}"
                fake.sent_message_ids.append(message_id)
                return _Response(message_id)

        sendgrid_mod = types.ModuleType("sendgrid")
        helpers = types.ModuleType("sendgrid.helpers")
        mail_mod = types.ModuleType("sendgrid.helpers.mail")
        sendgrid_mod.SendGridAPIClient = _Client            # type: ignore[attr-defined]
        mail_mod.Mail = lambda **kw: {"mail": kw}            # type: ignore[attr-defined]
        helpers.mail = mail_mod                              # type: ignore[attr-defined]
        sendgrid_mod.helpers = helpers                       # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
        return fake


def _arm(monkeypatch: Any) -> None:
    """Real send path: no sandbox, SendGrid configured, deterministic clock, opt-out secret."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(app_config.settings, "approval_secret", SECRET)
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Hand the agent the payload its CLI collection would have produced (the mock CLI has none)."""
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def _send_payload(*emails: dict[str, Any]) -> dict[str, Any]:
    return {"action": "send", "emails": list(emails), "prospects": []}


def _email(address: str, n: int = 0) -> dict[str, Any]:
    return {"prospect_email": address, "subject": f"s{n}", "body": f"b{n}"}


async def _count(db: Any, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar())


async def _company(db: Any) -> None:
    db.add(
        CompanyConfig(
            name="Acme Robotics",
            sender_legal_name=LEGAL_NAME,
            sender_postal_address=POSTAL_ADDRESS,
            website_url=WEBSITE,
        )
    )
    await db.flush()


# ── 1. the state vocabulary and the acceptance rule (pure) ─────────────────────────────────────


def test_the_attempt_vocabulary_is_the_agent_output_contract() -> None:
    """`EMAIL_ATTEMPT_STATUSES` is the schema's literal set — one vocabulary, pinned in two places.

    The ORDER differs on purpose (this module lists the lifecycle order draft -> queued -> sent ->
    failed; the schema lists draft, sent, queued, failed), so the pin is the SET.
    """
    annotation = EmailDraft.model_fields["status"].annotation
    assert set(annotation.__args__) == set(EMAIL_ATTEMPT_STATUSES) == {
        "draft", "queued", "sent", "failed",
    }
    assert EMAIL_ROW_DEFAULT_STATUS == "draft"          # OLD: "sent" (migration 0019)


def test_the_row_vocabulary_is_the_database_check_and_the_attempts_are_a_subset() -> None:
    """The DB's `ck_email_logs_status` allows 8 states; an ATTEMPT may only be one of the four.

    Migration 0002's CHECK (PostgreSQL-only, verified live 2026-10-29) is the row vocabulary; the
    four delivery-event states have NO producer in this tree (a `delivered`/`bounced` row would be
    a fabricated event without a provider callback), which is why the attempt vocabulary is the
    subset below.  A future provider-event writer must add its state to `EmailDraft.status` too,
    and this pin is where that decision becomes visible.
    """
    assert set(EMAIL_ATTEMPT_STATUSES) < set(EMAIL_ROW_STATUSES)
    assert set(EMAIL_ROW_STATUSES) == {
        "draft", "queued", "sent", "delivered", "opened", "replied", "bounced", "failed",
    }


@pytest.mark.parametrize(
    ("result", "accepted", "why"),
    [
        ({"ok": True, "result": {"status_code": 202, "message_id": "sg-1"}}, True, "2xx + id"),
        ({"ok": True, "result": {"status_code": 202}}, True, "2xx is the provider's own answer"),
        ({"ok": True, "result": {"status_code": 200, "message_id": "x"}}, True, "200"),
        ({"ok": True, "result": {"status_code": 400, "message_id": "sg-x"}}, False, "4xx = no"),
        ({"ok": True, "result": {"status_code": 503, "message_id": "sg-x"}}, False, "5xx = no"),
        ({"ok": True, "result": {"message_id": "sg-1"}}, True, "no code, provider id = evidenced"),
        ({"ok": True, "result": {}}, False, "no code and no id: unevidenced"),
        ({"ok": True, "result": {"status_code": 0}}, False, "0 is not a status"),
        ({"ok": True, "result": {"status_code": True}}, False, "a bool is not a status"),
        ({"ok": False, "error": {"code": E_INTEGRATION}}, False, "the adapter refused"),
        ("not-a-dict", False, "not a result at all"),
    ],
)
def test_provider_acceptance_is_evidenced_only(
    result: Any, accepted: bool, why: str
) -> None:
    assert provider_accepted(result) is accepted, why


def test_the_provider_helpers_never_invent_an_id_or_a_status() -> None:
    assert provider_message_id({"ok": True, "result": {"status_code": 202}}) == ""
    assert provider_message_id({"ok": True, "result": {"message_id": None}}) == ""
    assert provider_message_id({"ok": True, "result": {"message_id": " sg-9 "}}) == "sg-9"
    assert provider_status_code({"ok": True, "result": {}}) is None
    assert provider_status_code({"ok": True, "result": {"status_code": -1}}) is None


# ── 2. a row that states no status is a draft, and the cap does not count it ───────────────────


async def test_a_row_that_states_no_status_is_a_draft_and_is_not_a_counted_send(
    async_db_session,
) -> None:
    """RED LEG (raw-w7/RED-A-sent-default.xml): restore `server_default="sent"` -> this fails.

    The defect: `email_logs.status` defaulted to `sent`, so an INSERT that never mentioned a
    status became a performed send (no provider id, no `sent_at`) and `channel_sends_today`
    counted it.  After 0019 the same INSERT is a `draft`, and the cap measures 0.
    """
    db = async_db_session
    prospect = Prospect(email="ada@example.com")
    db.add(prospect)
    await db.flush()
    db.add(EmailLog(prospect_id=prospect.id, subject="never stated"))   # no status= argument
    await db.commit()

    row = (await db.execute(select(EmailLog))).scalars().one()
    assert row.status == "draft"                       # WAS "sent" (the fabricated send)
    assert row.sendgrid_id is None and row.sent_at is None
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 0, "hour": 0}
    }


# ── 3. the writer: a sent claim needs a provider id, and the provider's own answer ──────────────


async def test_the_writer_counts_only_a_provider_accepted_send(async_db_session, monkeypatch) -> None:
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "draft", "sendgrid_id": "sg-draft", "prospect_email": "a@example.com"},
        {"status": "sent", "sendgrid_id": "", "prospect_email": "b@example.com"},
        {"status": "sent", "sendgrid_id": None, "prospect_email": "c@example.com"},
        {"status": "failed", "sendgrid_id": "sg-failed", "prospect_email": "d@example.com"},
        {"status": "sent", "sendgrid_id": "sg-real", "prospect_email": "e@example.com",
         "provider_status": 202},
    ]})
    assert report["emails_written"] == 1 and report["ok"] is True
    rows = (await db.execute(select(EmailLog))).scalars().all()
    assert [(row.status, row.sendgrid_id) for row in rows] == [("sent", "sg-real")]
    assert rows[0].sent_at is not None


async def test_a_sent_claim_that_contradicts_the_provider_answer_is_skipped_by_name(
    async_db_session, monkeypatch,
) -> None:
    """RED LEG (raw-w7/RED-B-rejected-claim.xml): drop the gate -> a 400 'send' is counted.

    A replayed (or pre-fix) output can say `status="sent"` while its recorded provider answer is
    a 4xx.  The ledger's writer must not turn that into a counted send: the row is skipped by
    name, the report is honest (`ok=False`, `rejected_provider_status`), and no email row exists.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-rejected", "prospect_email": "rej@example.com",
         "provider_status": 400},
        {"status": "sent", "sendgrid_id": "sg-ok", "prospect_email": "ok@example.com",
         "provider_status": 202},
    ]})
    assert report["emails_written"] == 1
    assert report["ok"] is False
    assert report["rejected_provider_status"] == [{
        "index": 0, "prospect_email": "rej@example.com", "sendgrid_id": "sg-rejected",
        "provider_status": 400,
    }]
    rows = (await db.execute(select(EmailLog))).scalars().all()
    assert [row.sendgrid_id for row in rows] == ["sg-ok"]
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 1, "hour": 1}
    }


# ── 4. the agent: a 4xx answer is failed + named, never "sent" ─────────────────────────────────


async def test_the_agent_records_failed_when_the_provider_answers_4xx(
    async_db_session, monkeypatch,
) -> None:
    """RED LEG (raw-w7/RED-C-ok-is-sent.xml): `if result.get("ok")` -> status "sent", a fake send.

    The double is the PROVIDER-CALL seam (permitted, AGENTS.md §7): it returns the hostile shape
    `{"ok": True, "result": {"status_code": 400, "message_id": ...}}` — an adapter answer that
    "succeeded" at the call layer while the provider in fact refused the message.  The live
    SendGrid adapter refuses a non-2xx answer itself (defence in depth); this leg proves the AGENT
    does not depend on that, because `ok is True` is not acceptance.  The run records `failed`,
    the provider's status rides the named refusal, and NOTHING is persisted or counted.
    """
    _arm(monkeypatch)

    def _hostile_send(*, to: str, subject: str, body: str) -> dict[str, Any]:
        return {"ok": True, "result": {"status_code": 400, "message_id": "sg-refused"}}

    monkeypatch.setattr("app.agents.integrations.send_email", _hostile_send)
    db = async_db_session
    agent = _stub_collection(EmailOutreachAgent(), _send_payload(_email("rej@example.com")))

    out = agent.run(
        {"title": "send"},
        {"channel_sends": {"email": {"today": 0, "hour": 0}}, "compliance": _COMPLIANCE},
    )

    assert out["emails"][0]["status"] == "failed"          # WAS "sent" (the fake)
    assert out["emails"][0]["sendgrid_id"] is None
    assert out["emails"][0]["provider_status"] == 400
    refusal = out["refusals"][0]["error"]
    assert refusal["code"] == E_INTEGRATION
    assert refusal["context"]["provider_status"] == 400

    report = await outreach_service.record_sent_emails(db, out)
    assert report["emails_written"] == 0
    assert await _count(db, EmailLog) == 0
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 0, "hour": 0}
    }


async def test_the_agent_records_sent_on_202_with_the_provider_id(
    async_db_session, monkeypatch,
) -> None:
    """The other half: a real 202 + `X-Message-Id` is exactly one counted row."""
    _arm(monkeypatch)
    fake = _FakeSendGrid(status=202).install(monkeypatch)
    db = async_db_session
    agent = _stub_collection(EmailOutreachAgent(), _send_payload(_email("ok@example.com")))

    out = agent.run(
        {"title": "send"},
        {"channel_sends": {"email": {"today": 0, "hour": 0}}, "compliance": _COMPLIANCE},
    )

    assert out["emails"][0]["status"] == "sent"
    assert out["emails"][0]["sendgrid_id"] == fake.sent_message_ids[0]
    assert out["emails"][0]["provider_status"] == 202
    assert "refusals" not in out or out["refusals"] == []
    report = await outreach_service.record_sent_emails(db, out)
    assert report["emails_written"] == 1
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 1, "hour": 1}
    }


# ── 5. dedupe / idempotency: the same message twice is one row ─────────────────────────────────


async def test_the_same_provider_message_twice_is_one_row_and_one_count(
    async_db_session, monkeypatch,
) -> None:
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    result = {"emails": [{
        "status": "sent", "sendgrid_id": "sg-once", "prospect_email": "once@example.com",
        "subject": "s", "body": "b", "provider_status": 202,
    }]}
    first = await outreach_service.record_sent_emails(db, result)
    second = await outreach_service.record_sent_emails(db, result)

    assert (first["emails_written"], second["duplicates_skipped"]) == (1, 1)
    assert await _count(db, EmailLog) == 1
    assert await _count(db, Prospect) == 1
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 1, "hour": 1}
    }


# ── 6. one mailbox is one row (the case-variant dedupe and its suppression consequence) ────────


async def test_two_casings_of_one_mailbox_are_one_prospect(async_db_session, monkeypatch) -> None:
    """RED LEG (raw-w7/RED-D-case-split.xml): exact-match lookup -> TWO prospect rows.

    OLD: `_find_or_create_prospect` selected `Prospect.email == address` (case-sensitive), so
    `Ada@Example.com` and `ada@example.com` became two rows for one person — and the suppression
    check reads with `lower(email) = …`, i.e. it could answer from the wrong one.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session
    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-case-1", "prospect_email": "Ada@Example.COM",
         "subject": "s1", "body": "b1", "provider_status": 202},
        {"status": "sent", "sendgrid_id": "sg-case-2", "prospect_email": "ada@example.com",
         "subject": "s2", "body": "b2", "provider_status": 202},
    ]})
    assert report["emails_written"] == 2
    assert report["prospects_created"] == 1                      # WAS 2 (two rows, one mailbox)
    addresses = (await db.execute(select(Prospect.email))).scalars().all()
    assert addresses == ["ada@example.com"]
    assert await _count(db, EmailLog) == 2


async def test_a_case_variant_send_cannot_bypass_the_suppression_list(
    async_db_session, monkeypatch,
) -> None:
    """The consequence the split caused: the SAME mailbox opted out, and a send must be refused.

    The subscriber is answered for every casing, because there is one row and the send path
    compares case-insensitively — the guarded sender refuses with the NAMED `E_POLICY_DENIED`
    before any provider contact.
    """
    _arm(monkeypatch)
    db = async_db_session
    await _company(db)
    await outreach_service.unsubscribe_prospect(db, "Ada@Example.COM")
    await db.commit()

    with pytest.raises(MeridianError) as excinfo:
        await send_compliant_email(
            db, to="ADA@example.com", subject="hi", body="hello",
        )
    assert excinfo.value.code == E_POLICY_DENIED
    assert "suppressed" in excinfo.value.message


# ── 7. the lost write: a "find" run persists its prospects ─────────────────────────────────────


async def test_a_find_run_persists_its_prospects_once(async_db_session) -> None:
    """RED LEG (raw-w7/RED-E-lost-find.xml): without the found-prospects leg -> 0 rows.

    `record_sent_emails` only ever created a prospect as a side effect of a SENT email, so
    `action="find"` — the outreach agent's first duty — wrote NOTHING and the outreach page's
    "the outreach agent finds prospects when it runs" copy was false.
    """
    db = async_db_session
    result = {"action": "find", "emails": [], "prospects": [
        {"email": "Found@Example.COM", "first_name": "Ada", "company": "Analytical",
         "source": "directory"},
        {"email": "second@example.com", "last_name": "Lovelace"},
        {"email": "not-an-address"},
    ]}
    report = await outreach_service.record_sent_emails(db, result)

    assert report["found_prospects_written"] == 2
    assert report["found_prospects_existing"] == 0
    assert report["skipped_found_prospects"] == [{
        "index": 2, "email": "not-an-address", "code": E_VALIDATION,
        "message": "prospect email 'not-an-address' is not a usable address",
    }]
    assert report["ok"] is False                                  # the skip is never silent
    rows = (await db.execute(select(Prospect).order_by(Prospect.id))).scalars().all()
    assert [(row.email, row.first_name, row.company, row.source) for row in rows] == [
        ("found@example.com", "Ada", "Analytical", "directory"),
        ("second@example.com", None, None, None),
    ]

    again = await outreach_service.record_sent_emails(db, result)   # a replayed run
    assert (again["found_prospects_written"], again["found_prospects_existing"]) == (0, 2)
    assert await _count(db, Prospect) == 2


# ── 8. the footer's reply-to-opt-out promise is executable ────────────────────────────────────


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("unsubscribe", "hello", True),
        ("Re: unsubscribe", "hello", True),
        ("Re: hello", "unsubscribe", True),
        ("Re: hello", "Please unsubscribe me.", True),
        ("Re: hello", "opt out", True),
        ("Re: hello", "I would like to know more", False),
        ("Re: hello", '> To opt out: reply to this email with "unsubscribe"', False),
        # R3 (logic-debugger's W7 matrix, REQUESTS-W7 R3): an ATTRIBUTION line ends the scan —
        # everything under "On … wrote:" is the quoted original, which carries our own footer.
        ("Re: hello", "On Monday someone wrote:\nunsubscribe", False),
        ("Re: hello", "unsubscribe\n\nOn Monday someone wrote:\n> unsubscribe", True),
        ("Re: hello", "-----Original Message-----\nunsubscribe", False),
    ],
)
def test_is_opt_out_reply_shapes(subject: str, body: str, expected: bool) -> None:
    assert is_opt_out_reply(subject, body) is expected


async def test_a_reply_saying_unsubscribe_records_the_durable_opt_out(
    async_db_session, monkeypatch,
) -> None:
    """RED LEG (raw-w7/RED-F-reply-optout.xml): before the fix -> chain stopped, NO suppression.

    The footer line says "reply to this email with 'unsubscribe'".  The reply used to stamp
    `replied_at` (stopping THIS chain) and store nothing, so the next campaign mailed the person
    again.  Now the ONE producer (`unsubscribe_prospect`) runs in the same transaction, and the
    audit row carries IDs/flags only — never the customer's text.
    """
    from app.models.support import SupportInboxItem
    from app.services import support_inbox_service
    from app.services.activity_service import ActivityLog  # noqa: F401  (import check only)

    monkeypatch.setattr(support_inbox_service, "now_utc", lambda: NOW)
    db = async_db_session
    prospect = Prospect(email="chain@example.com", status="contacted")
    db.add(prospect)
    await db.flush()
    db.add(EmailLog(prospect_id=prospect.id, subject="touch 1", status="sent",
                    sendgrid_id="sg-chain-1", sent_at=NOW, follow_up_due=NOW))
    await db.commit()

    item = await submit_item(
        db, provider_message_id="reply-opt-out-1", from_address="CHAIN@Example.COM",
        subject="Re: your email", body="unsubscribe", received_at=None,
    )
    await db.commit()

    refreshed = await db.get(Prospect, prospect.id)
    assert refreshed is not None and refreshed.status == "unsubscribed"
    log = (await db.execute(select(EmailLog))).scalars().one()
    assert log.replied_at is not None                     # the chain stopped, as before
    assert await _count(db, SupportInboxItem) == 1

    from sqlalchemy import text as sql_text

    rows = (await db.execute(
        sql_text("SELECT action, detail FROM activity_log WHERE action = :a"),
        {"a": OPT_OUT_AUDIT_ACTION},
    )).all()
    assert len(rows) == 1
    assert "unsubscribe" not in str(rows[0][1]).lower()   # no customer text in the audit row
    assert item.provider_message_id == "reply-opt-out-1"


async def test_a_quoted_footer_alone_is_not_an_opt_out(async_db_session, monkeypatch) -> None:
    """The false-positive guard: quoting OUR footer back is not the customer's ask."""
    from app.services import support_inbox_service

    monkeypatch.setattr(support_inbox_service, "now_utc", lambda: NOW)
    db = async_db_session
    prospect = Prospect(email="quoter@example.com", status="contacted")
    db.add(prospect)
    await db.flush()
    db.add(EmailLog(prospect_id=prospect.id, subject="touch 1", status="sent",
                    sendgrid_id="sg-quoter-1", sent_at=NOW))
    await db.commit()

    await submit_item(
        db, provider_message_id="reply-quote-1", from_address="quoter@example.com",
        subject="Re: your email",
        body='> To opt out: reply to this email with "unsubscribe" and we will stop.',
        received_at=None,
    )
    await db.commit()

    refreshed = await db.get(Prospect, prospect.id)
    assert refreshed is not None and refreshed.status == "contacted"   # NOT suppressed
    log = (await db.execute(select(EmailLog))).scalars().one()
    assert log.replied_at is not None                                    # the chain still stops
