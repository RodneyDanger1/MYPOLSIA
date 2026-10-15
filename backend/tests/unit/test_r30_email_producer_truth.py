"""R30 — the email cap's producers: provider-id truth (M3) and batch continuation (M5).

The two R27 residuals this lane closes, pinned through the REAL functions only:

  * M3 — `email_outreach/agent.py` fell back to the HTTP status code when the adapter reported no
    `X-Message-Id`, so every such send's `sendgrid_id` was `"202"` and
    `outreach_service.record_sent_emails` collapsed DISTINCT real sends onto ONE counted row
    (measured: 2 sends -> 1 -> the email cap undercounted permanently). Fixed agent-side: a send
    with no REAL provider id is not recorded, and the performed-but-uncountable gap is NAMED
    (`uncounted_sends`) instead of silently absorbed.
  * M5 — `record_sent_emails` aborted the WHOLE batch at the first malformed `prospect_email`;
    performed sends after it were never counted. Fixed: the malformed item is skipped by name
    (`skipped_malformed`) and the batch CONTINUES on a provably healthy session (the address
    refusal fires before any flush).
  * parity — email_outreach and customer_support are the two producers of the SAME dedupe key;
    one test pins both captures against both provider shapes so they cannot drift apart again
    (M3 existed because the CS capture was right and the email capture was not).

Doubles live only here: the CLI seam (`collect_structured` — the mock CLI carries no structured
payload), the provider SDK (a fake `sendgrid` module mirroring the real 202 + X-Message-Id
headers, WITH and WITHOUT the header), and the clock (fixed anchor + patched `utcnow`, the
repo's own pattern). Every DB row is written by the REAL producer on the real (sqlite) schema.
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

import app.config as app_config
from app.agents.customer_support.agent import CustomerSupportAgent
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.core.errors import E_VALIDATION
from app.models.outreach import EmailLog, Prospect
from app.services import outreach_service
from app.services.channel_service import channel_sends_snapshot

#: V11/W1: the worker-resolved compliance preflight a real `email_outreach` run carries — the
#: same `channel_sends` pattern (the worker owns the DB, the agent does not). A send without it
#: (and with no database for the guarded sender to read) is REFUSED BY NAME, so every leg that
#: expects a real provider send states the sender identity here. No assertion changed.
_COMPLIANCE = {"company": {"sender_legal_name": "Acme Robotics LLC",
                          "sender_postal_address": "123 Main St, Minneapolis, MN 55401, USA",
                          "website_url": "https://acme.example"},
               "suppressed": []}

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)



# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Hand the agent the payload its CLI collection would have produced (mock CLI carries none)."""
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


class _FakeSendGrid:
    """Provider double: 202 + a UNIQUE `X-Message-Id` per send — or NO header when told to.

    The two shapes are the whole point: `send_ids=False` is the real API behaviour the M3 defect
    keyed on (a 202 with no per-message id must never become an id).
    """

    def __init__(self, *, send_ids: bool = True) -> None:
        self.sent_message_ids: list[str] = []
        self._send_ids = send_ids

    def install(self, monkeypatch: Any) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = 202
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


def _email_payload(index: int) -> dict[str, Any]:
    return {"action": "send", "emails": [{
        "prospect_email": f"prospect{index}@example.com",
        "subject": f"s{index}", "body": f"b{index}",
    }]}


def _two_email_payload() -> dict[str, Any]:
    return {"action": "send", "emails": [
        {"prospect_email": "first@example.com", "subject": "s0", "body": "b0"},
        {"prospect_email": "second@example.com", "subject": "s1", "body": "b1"},
    ]}


def _support_payload() -> dict[str, Any]:
    return {"replies": [{
        "message_ref": "msg-1",
        "from_address": "cust@example.com",
        "subject": "Billing question",
        "reply_body": "You can update it under Settings -> Billing.",
    }]}


def _as_the_worker_maps_cs_replies(out: dict[str, Any]) -> dict[str, Any]:
    """The exact mapping `agent_tasks._persist_run_artifacts` applies to CS runs (read: 239-257)."""
    return {"emails": [
        {
            "prospect_email": str(r.get("from_address") or r.get("from") or "").strip(),
            "subject": f"Re: {r.get('subject') or 'your message'}",
            "body": str(r.get("reply_body") or ""),
            "status": r.get("status"),
            "sendgrid_id": r.get("sendgrid_id"),
        }
        for r in out.get("replies") or []
        if str(r.get("from_address") or r.get("from") or "").strip()
    ]}


def _arm(monkeypatch: Any) -> None:
    """Real send path: no sandbox, SendGrid configured, deterministic clock."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)


async def _count(db: Any, model: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar())


# ── M3: no real provider id -> not counted, named — never a status code as a key ───────────────


async def test_m3_two_sends_without_a_provider_id_are_zero_rows_and_both_gaps_named(
    async_db_session, monkeypatch,
):
    """The M3 measurement re-run: 2 distinct performed sends, no `X-Message-Id` anywhere.

    Before R30: both `sendgrid_id`s were `"202"`, `record_sent_emails` wrote ONE row and the
    email cap counted 1 for 2 real sends. Now: both captures are `""` (a status code is not an
    id), the producer records NOTHING (0 is the honest count for "performed but uncountable" —
    never a fabricated 1), and each gap is NAMED on the run output.
    """
    _arm(monkeypatch)
    fake = _FakeSendGrid(send_ids=False).install(monkeypatch)
    db = async_db_session

    agent = _stub_collection(EmailOutreachAgent(), _two_email_payload())
    out = agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 0, "hour": 0}}, "compliance": _COMPLIANCE})

    assert len(fake.sent_message_ids) == 2                        # two real provider sends happened
    assert [e["status"] for e in out["emails"]] == ["sent", "sent"]
    assert [e["sendgrid_id"] for e in out["emails"]] == ["", ""]  # WAS ["202", "202"]
    assert out["uncounted_sends"] == [
        {"reason": "no_provider_id", "prospect_email": "first@example.com",
         "provider_status": 202},
        {"reason": "no_provider_id", "prospect_email": "second@example.com",
         "provider_status": 202},
    ]

    report = await outreach_service.record_sent_emails(db, out)
    assert report["ok"] is True and report["emails_written"] == 0
    assert await _count(db, EmailLog) == 0                        # NOT collapsed onto 1 row
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 0, "hour": 0}}


async def test_m3_two_sends_with_real_distinct_ids_are_two_rows(async_db_session, monkeypatch):
    """The other half of the measurement: a real `X-Message-Id` per send -> one counted row each."""
    _arm(monkeypatch)
    fake = _FakeSendGrid(send_ids=True).install(monkeypatch)
    db = async_db_session

    agent = _stub_collection(EmailOutreachAgent(), _two_email_payload())
    out = agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 0, "hour": 0}}, "compliance": _COMPLIANCE})

    assert [e["sendgrid_id"] for e in out["emails"]] == fake.sent_message_ids == [
        "sg-msg-1", "sg-msg-2"]
    assert "uncounted_sends" not in out                           # nothing uncountable to name

    report = await outreach_service.record_sent_emails(db, out)
    assert (report["emails_written"], report["duplicates_skipped"], report["ok"]) == (2, 0, True)
    rows = (await db.execute(select(EmailLog))).scalars().all()
    assert sorted(r.sendgrid_id for r in rows) == ["sg-msg-1", "sg-msg-2"]
    assert len({r.sendgrid_id for r in rows}) == 2                # two DISTINCT keys
    assert await _count(db, Prospect) == 2                        # one prospect per address
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {
        "email": {"today": 2, "hour": 2}}


# ── M5: item-scoped continuation at the first malformed address ─────────────────────────────────


async def test_m5_a_malformed_head_item_skips_by_name_and_the_batch_continues(
    async_db_session, monkeypatch,
):
    """The caller's case, verbatim: batch [malformed, valid, valid] -> 2 counted + 1 named skip.

    The malformed performed send cannot become an `email_logs` row (`prospect_id` is a required
    FK), but the refusal fires before any flush — so the session is healthy and the two sends
    AFTER it must still be counted. Before R30 this batch wrote ONE row and permanently
    undercounted the other two.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session

    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-bad", "prospect_email": "no-at-sign"},
        {"status": "sent", "sendgrid_id": "sg-1", "prospect_email": "a@example.com",
         "subject": "s1", "body": "b1"},
        {"status": "sent", "sendgrid_id": "sg-2", "prospect_email": "b@example.com",
         "subject": "s2", "body": "b2"},
    ]})

    assert report["emails_written"] == 2                          # WAS 0 (batch aborted at item 0)
    assert report["ok"] is False                                  # ...the skip is never silent
    assert report["error"] == {"code": E_VALIDATION,
                               "message": "prospect email 'no-at-sign' is not a usable address"}
    assert report["skipped_malformed"] == [{
        "index": 0,
        "prospect_email": "no-at-sign",
        "sendgrid_id": "sg-bad",
        "code": E_VALIDATION,
        "message": "prospect email 'no-at-sign' is not a usable address",
    }]
    assert sorted(
        log.sendgrid_id for log in (await db.execute(select(EmailLog))).scalars()
    ) == ["sg-1", "sg-2"]
    assert await _count(db, Prospect) == 2

    # the session is usable after the skip: a follow-up write on the SAME session succeeds
    after = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-3", "prospect_email": "c@example.com",
         "subject": "s3", "body": "b3"},
    ]})
    assert after["emails_written"] == 1 and after["ok"] is True
    assert await _count(db, EmailLog) == 3


async def test_m5_a_batch_without_skips_keeps_the_exact_report_shape(async_db_session,
                                                                     monkeypatch):
    """No skip -> NO `skipped_malformed` key at all (the frozen exact-shape pins depend on it).

    `test_channel_agent_caps.py` and the R27 sweep file both assert the no-skip report
    VERBATIM; the skip list is a detail that appears only when there is something to list.
    """
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    db = async_db_session

    report = await outreach_service.record_sent_emails(db, {"emails": [
        {"status": "sent", "sendgrid_id": "sg-only", "prospect_email": "only@example.com",
         "subject": "s", "body": "b"},
    ]})
    assert report == {"ok": True, "emails_written": 1, "prospects_created": 1,
                      "duplicates_skipped": 0, "error": None}
    assert "skipped_malformed" not in report


# ── parity: the two producers of the one dedupe key cannot drift apart again ────────────────────


async def test_producer_parity_email_outreach_and_customer_support_agree(
    async_db_session, monkeypatch,
):
    """ONE test, BOTH producers, BOTH provider shapes — the reconcile pin for R27's M3 drift.

    email_outreach and customer_support both send through the same adapter and are counted by
    the same producer. M3 existed because the CS capture was right (provider id or empty) and the
    email capture was not (`or status_code`). After R30 both captures are byte-identical in
    behaviour:
      * a real `X-Message-Id` -> both keep the provider's OWN id and both are counted once;
      * no header -> NEITHER fabricates a key (both `""`, never `"202"`), NEITHER is counted,
        and the email side names its gap.
    """
    _arm(monkeypatch)
    db = async_db_session
    caps = {"channel_sends": {"email": {"today": 0, "hour": 0}}, "compliance": _COMPLIANCE}

    # (a) both providers return a real per-message id
    fake = _FakeSendGrid(send_ids=True).install(monkeypatch)
    email_out = _stub_collection(EmailOutreachAgent(), _email_payload(0)).run(
        {"title": "send"}, caps)
    support_out = _stub_collection(CustomerSupportAgent(), _support_payload()).run(
        {"title": "reply"}, caps)

    assert email_out["emails"][0]["sendgrid_id"] == "sg-msg-1"
    assert support_out["replies"][0]["sendgrid_id"] == "sg-msg-2"
    assert fake.sent_message_ids == ["sg-msg-1", "sg-msg-2"]
    email_report = await outreach_service.record_sent_emails(db, email_out)
    support_report = await outreach_service.record_sent_emails(
        db, _as_the_worker_maps_cs_replies(support_out))
    assert (email_report["emails_written"], support_report["emails_written"]) == (1, 1)
    assert "uncounted_sends" not in email_out and "uncounted_sends" not in support_out

    # (b) NEITHER provider returns an id: identical capture semantics, nothing counted
    _FakeSendGrid(send_ids=False).install(monkeypatch)
    email_out = _stub_collection(EmailOutreachAgent(), _email_payload(1)).run(
        {"title": "send"}, caps)
    support_out = _stub_collection(CustomerSupportAgent(), _support_payload()).run(
        {"title": "reply"}, caps)

    assert email_out["emails"][0]["sendgrid_id"] == support_out["replies"][0]["sendgrid_id"] == ""
    assert "202" not in (email_out["emails"][0]["sendgrid_id"],
                         support_out["replies"][0]["sendgrid_id"])
    assert email_out["uncounted_sends"] == [{
        "reason": "no_provider_id",
        "prospect_email": "prospect1@example.com",
        "provider_status": 202,
    }]
    assert (await outreach_service.record_sent_emails(db, email_out))["emails_written"] == 0
    assert (await outreach_service.record_sent_emails(
        db, _as_the_worker_maps_cs_replies(support_out)))["emails_written"] == 0
    assert await _count(db, EmailLog) == 2                        # exactly (a)'s two rows
