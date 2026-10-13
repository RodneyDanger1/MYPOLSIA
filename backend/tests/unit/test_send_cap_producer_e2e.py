"""RD14 — the send-cap producer: real writers -> real reader -> real firewall (cap-fires E2E).

The audited defect class (A2 F-04; registry D3 residual "capability-cap (`RateCaps` send caps)
remains separate/known (`channel_sends` no writer)"): the per-channel caps read
`sends_today`/`sends_this_hour` and NOTHING wrote the records they count, so `RateCaps` could never
fire. These tests pin the two halves through the REAL functions only:

  * twitter — rows written by the real producer (`social_service.record_published_posts`, the exact
    function `agent_tasks._persist_run_artifacts` calls after a completed run) drive the real count
    (`channel_service.channel_sends_snapshot`) and the next send is DENIED by the existing caps
    (`E_RATE_LIMITED`, "N/N") — the refused attempt never reaches the provider and adds no row;
  * email — the cap can only fire when every REAL send carries the provider's OWN id. RD14 fixed
    `integrations._sendgrid_send` to return SendGrid's per-message `X-Message-Id`; before that the
    adapter returned only `status_code`, every send's dedupe key was "202", distinct sends collapsed
    onto ONE `email_logs` row and the count stuck at 1 (measured: raw/probe_state_BEFORE.txt). With
    the pre-fix return restored (the RED leg) the email acceptance below cannot pass.

Doubles are test-only and named: the CLI seam (`collect_structured` — the mock CLI carries no
structured payload), the provider SDK (a fake `sendgrid` module whose response mirrors SendGrid's:
`status_code=202` + a unique `X-Message-Id` header), and the clock (pinned with a fixed anchor /
patched `utcnow` exactly the way `test_channel_sends` pins it).
"""
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

import app.config as app_config
from app.agents import integrations
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_RATE_LIMITED, PolsiaError
from app.models.outreach import EmailLog, Prospect
from app.models.social import SocialPost
from app.services import outreach_service, social_service
from app.services.channel_service import channel_sends_snapshot

# A fixed mid-day anchor, like test_channel_sends: the company-local day and the trailing hour are
# both deterministic, and producer writes are pinned to it via the patched `utcnow`.
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)     # inside the day window, outside the hour window


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Hand the agent the payload its CLI collection would have produced (mock CLI carries none)."""
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


class _FakeSendGrid:
    """Provider double: mints a UNIQUE `X-Message-Id` per send, like the real API does.

    The real contract SendGrid's SDK exposes is exactly this: `response.status_code` plus a
    per-message `X-Message-Id` response header. `sent_message_ids` records what the provider was
    asked to send, so a test can assert that a refused attempt never reached it.
    """

    def __init__(self) -> None:
        self.sent_message_ids: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = 202
                self.headers = {"X-Message-Id": message_id}

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
        sendgrid_mod.SendGridAPIClient = _Client          # type: ignore[attr-defined]
        mail_mod.Mail = lambda **kw: {"mail": kw}          # type: ignore[attr-defined]
        helpers.mail = mail_mod                            # type: ignore[attr-defined]
        sendgrid_mod.helpers = helpers                     # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
        return fake


def _email_payload(index: int) -> dict[str, Any]:
    return {"action": "send", "emails": [{
        "prospect_email": f"prospect{index}@example.com",
        "subject": f"s{index}", "body": f"b{index}",
    }]}


# ── twitter: the working half, pinned (rows -> count -> refusal) ───────────────────────────────
async def test_twitter_cap_fires_against_rows_the_real_producer_wrote(async_db_session, monkeypatch):
    """11 real sends -> allowed, persisted; 12 -> the NEXT attempt is refused "12/12"."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    db = async_db_session

    # (a) the REAL producer writes 11 sends, two hours before the anchor (day window only)
    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "twitter", "content": f"seed {i}", "tweet_id": f"tw-{i}"} for i in range(11)
    ]})
    assert report["posts_written"] == 11

    # (b) the REAL reader — the exact shape the worker injects as `context["channel_sends"]`
    snapshot = await channel_sends_snapshot(db, ["twitter", "linkedin"], now=NOW)
    assert snapshot["twitter"] == {"today": 11, "hour": 0}
    assert set(snapshot["twitter"]) == {"today", "hour"}     # exactly the fields the caps read

    # (c) one real send under the cap: the firewall ALLOWS and the provider is called
    sent: list[dict[str, Any]] = []

    def fake_post_tweet(text: str, *, platform: str = "twitter") -> dict[str, Any]:
        sent.append({"platform": platform, "text": text})
        return {"ok": True, "result": {"id": f"tw-new-{len(sent)}"}}

    monkeypatch.setattr(integrations, "post_tweet", fake_post_tweet)
    agent = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "twitter", "content": "post under the cap"},
    ]})
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]
    assert out["posted"][0]["tweet_id"] == "tw-new-1"
    assert len(sent) == 1

    # (d) the worker persists that REAL send (same producer, same flush-only discipline)
    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["twitter"], now=NOW)
    assert at_cap == {"twitter": {"today": 12, "hour": 1}}

    # (e) the NEXT send: the existing RateCaps enforcement refuses it, by name
    agent2 = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "twitter", "content": "one too many"},
    ]})
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "12/12" in excinfo.value.message and "twitter" in excinfo.value.message
    assert len(sent) == 1                                     # the refused attempt sent nothing
    assert (await db.execute(select(func.count()).select_from(SocialPost))).scalar() == 12


async def test_twitter_hour_cap_fires_from_fresh_rows(async_db_session, monkeypatch):
    """3 sends inside the trailing hour == the twitter hour cap: the next attempt is refused 3/3."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    db = async_db_session
    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "twitter", "content": f"fresh {i}", "tweet_id": f"tw-fresh-{i}"} for i in range(3)
    ]})
    assert report["posts_written"] == 3

    snapshot = await channel_sends_snapshot(db, ["twitter"], now=NOW)
    assert snapshot["twitter"] == {"today": 3, "hour": 3}

    agent = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "twitter", "content": "over the hour"},
    ]})
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "hourly cap" in excinfo.value.message and "3/3" in excinfo.value.message


# ── email: the acceptance — the cap fires only when every real send gets its own provider id ───
async def test_email_cap_fires_when_every_real_send_carries_the_providers_own_id(
    async_db_session, monkeypatch
):
    """15 real sends through the REAL adapter + producer -> 15 counted rows -> the 16th refused.

    RED-able: with the pre-RD14 `_sendgrid_send` (status_code only) every send's id is "202", the
    producer records ONE row and skips 14 duplicates, the count sticks at 1 and the 16th send
    proceeds — so this test cannot pass before the fix.
    """
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr(outreach_service, "utcnow", lambda: NOW)
    fake = _FakeSendGrid().install(monkeypatch)
    db = async_db_session

    # Fifteen REAL sends (the email hour cap is 15 in RateCaps.per_channel_hour).
    for i in range(15):
        snapshot = await channel_sends_snapshot(db, ["email"], now=NOW)
        assert snapshot["email"]["hour"] == i                 # the count climbs, send by send
        agent = _stub_collection(EmailOutreachAgent(), _email_payload(i))
        out = agent.run({"title": "send"}, {"channel_sends": snapshot})
        email = out["emails"][0]
        assert email["status"] == "sent"
        assert email["sendgrid_id"] == fake.sent_message_ids[-1]   # the provider's OWN unique id
        assert email["sendgrid_id"] not in ("", "202")             # never a status code as an id
        report = await outreach_service.record_sent_emails(db, out)
        assert report["emails_written"] == 1, "each distinct real send is one counted row"
        assert report["duplicates_skipped"] == 0

    assert len(fake.sent_message_ids) == 15
    ids = (await db.execute(select(EmailLog.sendgrid_id))).scalars().all()
    assert len(ids) == len(set(ids)) == 15

    at_cap = await channel_sends_snapshot(db, ["email"], now=NOW)
    assert at_cap == {"email": {"today": 15, "hour": 15}}

    # the 16th send: the existing RateCaps enforcement refuses it before any provider call
    agent = _stub_collection(EmailOutreachAgent(), _email_payload(99))
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "send"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "15/15" in excinfo.value.message and "email" in excinfo.value.message
    assert len(fake.sent_message_ids) == 15                    # the provider was never reached
    assert (await db.execute(select(func.count()).select_from(EmailLog))).scalar() == 15


async def test_a_failed_send_adds_no_counted_row(async_db_session, monkeypatch):
    """The integration refuses (no credentials): status "failed", no row, the count does not move."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "")
    db = async_db_session

    agent = _stub_collection(EmailOutreachAgent(), _email_payload(0))
    out = agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 0, "hour": 0}}})
    assert out["emails"][0]["status"] == "failed"
    assert [r["error"]["code"] for r in out["refusals"]] == ["E_INTEGRATION"]

    report = await outreach_service.record_sent_emails(db, out)
    assert report["emails_written"] == 0
    assert (await db.execute(select(func.count()).select_from(EmailLog))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(Prospect))).scalar() == 0
    assert await channel_sends_snapshot(db, ["email"], now=NOW) == {"email": {"today": 0, "hour": 0}}
