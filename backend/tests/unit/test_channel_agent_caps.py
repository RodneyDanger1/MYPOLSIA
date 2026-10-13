"""W3-refusals · S2-channels: send-count pass-through, the two T7 id refusals, send producers.

The honesty class these tests pin (wave briefs COMMON/S2):
  * `int(counts.get("today") or 0)` collapsed "no local record of sends" into "measured zero" —
    so the firewall's caps could never fire (A2 F-04). Each channel agent must forward the raw
    `int | None`; the firewall refuses a CAPPED channel it cannot verify: unknown is neither
    zero nor allowed.
  * `f"campaign:{index}"` / `f"msg:{index}"` fabricated a provider identifier that then reached
    the firewall fingerprint — and, outside sandbox, the provider — wearing a real-looking id.
    T7: refuse by name (`E_VALIDATION`) and skip; the item never reaches the firewall.
  * the producers persist ONLY performed sends (status sent + real provider id), deduped,
    `flush`-only, never raising — they are the source the counts (and therefore the caps) come
    from.

Seat-local vs S1-dependent: the spy tests assert what the AGENTS forward and hold before S1's
firewall lands. The tests asserting the firewall's unknown refusal (`"no local record"`) and the
sandbox-before-caps order exercise the S1-landed `sends_today: int | None` contract from the same
wave, exactly as the brief specifies.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

import app.config as app_config
from app.agents.ads_management.agent import AdsManagementAgent
from app.agents.code_generation.agent import CodeGenerationAgent
from app.agents.customer_support.agent import CustomerSupportAgent
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.agents.policy_firewall import Decision, PolicyDecision
from app.agents.side_effects import SideEffectOutcome, named_refusal
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import (
    E_INTEGRATION,
    E_POLICY_DENIED,
    E_RATE_LIMITED,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    PolsiaError,
)
from app.models.outreach import EmailLog, Prospect
from app.models.social import SocialPost
from app.services import outreach_service, social_service

# --------------------------------------------------------------------------- helpers


class _FirewallSpy:
    """Records every `request_side_effect` call; answers with a sandbox refusal so the run
    records a labelled draft and never reaches a provider integration."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> SideEffectOutcome:
        self.calls.append((args, kwargs))
        return SideEffectOutcome(
            allowed=False,
            sandboxed=True,
            requires_approval=False,
            policy_code="POLSIA-E114",
            e_code=E_SANDBOX_BLOCKED,
            reason="test stub: sandbox refusal",
            audit={"sandbox": True},
        )

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1][1]


class _DecisionSpy:
    """Records every `evaluate_side_effect` call; answers with a contract-correct
    `PolicyDecision` (the shape the refund branch reads)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> PolicyDecision:
        self.calls.append((args, kwargs))
        return PolicyDecision(
            Decision.REQUIRE_APPROVAL, "test stub: refund needs a human", "POLSIA-E115"
        )


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Give the agent the payload its CLI collection would have produced (mock mode never
    spawns a CLI; this only stubs `collect_structured`)."""
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent


def _social_payload() -> dict[str, Any]:
    return {"action": "post", "posts": [{"platform": "twitter", "content": "hello world"}]}


def _email_payload() -> dict[str, Any]:
    return {"action": "send", "emails": [{
        "prospect_email": "prospect@example.com", "subject": "hi", "body": "hello",
    }]}


def _support_payload() -> dict[str, Any]:
    return {"replies": [{
        "message_ref": "msg-1", "proposed_action": "send_reply",
        "from": "customer@example.com", "subject": "help", "reply_body": "on it",
    }]}


def _ads_payload() -> dict[str, Any]:
    return {"recommendations": [{
        "platform": "google_ads", "action": "pause", "campaign_ref": "camp-1",
        "budget_change_usd": 0.0,
    }]}


def _code_payload() -> dict[str, Any]:
    return {"action": "open_pr", "changes": [{
        "repo": "acme/site", "branch": "feat/x", "pr_title": "t",
    }]}


# ------------------------------------------------------------------- A. counts pass-through
# The cap fires from MEASURED counts (the counts in the context reach the firewall untouched).


def test_social_over_daily_cap_is_refused_by_name(monkeypatch):
    """today=12 == twitter's RateCaps daily cap -> the firewall raises E_RATE_LIMITED and the
    message carries the observed counts. With the old `or 0` coercion this could never fire."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    agent = _stub_collection(SocialMediaAgent(), _social_payload())
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 12, "hour": 3}}})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "12/12" in excinfo.value.message
    assert "twitter" in excinfo.value.message


def test_social_one_below_cap_reaches_the_integration_not_the_cap(monkeypatch):
    """today=11, hour=2 (both below cap): the firewall ALLOWS, and the run refuses at the
    (stubbed) integration instead — the cap did not fire on below-cap counts."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(
        "app.agents.integrations.post_tweet",
        lambda text, *, platform="twitter": named_refusal(
            E_INTEGRATION, "test stub: twitter not configured"
        ),
    )
    agent = _stub_collection(SocialMediaAgent(), _social_payload())
    out = agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 11, "hour": 2}}})
    assert out["side_effects"][0]["performed"] is True        # the firewall allowed
    assert out["posted"] == []
    assert any(r.get("error", {}).get("code") == E_INTEGRATION for r in out["refusals"])


def test_social_unknown_counts_refused_no_local_record(monkeypatch):
    """Context WITHOUT channel_sends -> `None` (no local record) -> the capped channel cannot
    be verified and the firewall refuses by name. This is the end-to-end form of the
    pass-through: with `or 0` the None becomes a measured zero and nothing refuses."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    agent = _stub_collection(SocialMediaAgent(), _social_payload())
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "post"}, {})
    assert excinfo.value.code == E_POLICY_DENIED
    assert "no local record" in excinfo.value.message


def test_social_sandbox_over_cap_records_a_draft_not_a_raise(monkeypatch):
    """Sandboxed over-cap attempt: the sandbox refusal is the answer (a labelled draft), not a
    cap raise. Pins the new check order — caps bound REAL sends, and a sandboxed run performs
    none, so checking an unverifiable counter first would turn a draft into a run-killing denial."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    agent = _stub_collection(SocialMediaAgent(), _social_payload())
    out = agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 12, "hour": 3}}})
    assert out["sandbox"] is True
    assert out["drafted_not_sent"] is True
    assert out["side_effects"][0]["sandboxed"] is True
    assert out["posted"] == []


# ------------------------------------------- A. pass-through proof, one spy test per file
# Seat-local: what the agent FORWARDS to the firewall. Absent counts -> None (never 0);
# measured counts -> the exact ints. A planted `or 0` fails the `is None` leg of every test.


def test_social_counts_pass_through_raw(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.social_media.agent.request_side_effect", spy)
    agent = _stub_collection(SocialMediaAgent(), _social_payload())
    agent.run({"title": "post"}, {})
    assert spy.last["sends_today"] is None, "absent counts must stay None, not become 0"
    assert spy.last["sends_this_hour"] is None
    agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 11, "hour": 2}}})
    assert spy.last["sends_today"] == 11
    assert spy.last["sends_this_hour"] == 2


def test_email_outreach_counts_pass_through_raw(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.email_outreach.agent.request_side_effect", spy)
    agent = _stub_collection(EmailOutreachAgent(), _email_payload())
    agent.run({"title": "send"}, {})
    assert spy.last["sends_today"] is None, "absent counts must stay None, not become 0"
    assert spy.last["sends_this_hour"] is None
    agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 5, "hour": 1}}})
    assert spy.last["sends_today"] == 5
    assert spy.last["sends_this_hour"] == 1


def test_customer_support_counts_pass_through_raw(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    agent = _stub_collection(CustomerSupportAgent(), _support_payload())
    agent.run({"title": "reply"}, {})
    assert spy.last["sends_today"] is None, "absent counts must stay None, not become 0"
    assert spy.last["sends_this_hour"] is None
    agent.run({"title": "reply"}, {"channel_sends": {"email": {"today": 5, "hour": 1}}})
    assert spy.last["sends_today"] == 5
    assert spy.last["sends_this_hour"] == 1


def test_ads_counts_pass_through_raw(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.ads_management.agent.request_side_effect", spy)
    agent = _stub_collection(AdsManagementAgent(), _ads_payload())
    agent.run({"title": "ads"}, {})
    assert spy.last["sends_today"] is None, "absent counts must stay None, not become 0"
    assert spy.last["sends_this_hour"] is None
    agent.run({"title": "ads"}, {"channel_sends": {"google_ads": {"today": 3, "hour": 1}}})
    assert spy.last["sends_today"] == 3
    assert spy.last["sends_this_hour"] == 1


def test_code_generation_counts_pass_through_raw(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.code_generation.agent.request_side_effect", spy)
    agent = _stub_collection(CodeGenerationAgent(), _code_payload())
    agent.run({"title": "pr"}, {})
    assert spy.last["sends_today"] is None, "absent counts must stay None, not become 0"
    assert spy.last["sends_this_hour"] is None
    agent.run({"title": "pr"}, {"channel_sends": {"github": {"today": 1, "hour": 0}}})
    assert spy.last["sends_today"] == 1
    assert spy.last["sends_this_hour"] == 0


@pytest.mark.parametrize("agent_cls,payload_factory", [
    (EmailOutreachAgent, _email_payload),
    (CustomerSupportAgent, _support_payload),
    (AdsManagementAgent, _ads_payload),
    (CodeGenerationAgent, _code_payload),
])
def test_unknown_counts_refused_no_local_record_for_every_channel_agent(
    monkeypatch, agent_cls, payload_factory
):
    """Same end-to-end unknown-count refusal as the social test, for the other four files."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    agent = _stub_collection(agent_cls(), payload_factory())
    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "t"}, {})
    assert excinfo.value.code == E_POLICY_DENIED
    assert "no local record" in excinfo.value.message


# ------------------------------------------------------------------ B. T7: skip, never fabricate


def test_ads_recommendation_without_campaign_ref_is_refused_and_never_reaches_the_firewall(
    monkeypatch,
):
    """No provider campaign_ref: a named E_VALIDATION refusal, applied=False, and — the point —
    zero firewall calls. The old `f"campaign:{index}"` fabricates an id and calls the firewall
    with it (see raw/redleg-1-ads-fabricated-ref.sh)."""
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.ads_management.agent.request_side_effect", spy)
    agent = _stub_collection(AdsManagementAgent(), {"recommendations": [{
        "platform": "google_ads", "action": "pause",
    }]})
    out = agent.run({"title": "ads"}, {})
    assert spy.calls == [], "a fabricated campaign ref must never reach the firewall"
    refusals = [r for r in out["refusals"] if r["error"]["code"] == E_VALIDATION]
    assert len(refusals) == 1
    assert "recommendation #0 has no provider campaign_ref" in refusals[0]["error"]["message"]
    assert refusals[0]["error"]["context"] == {"index": 0, "platform": "google_ads"}
    assert refusals[0]["error"]["how_to_fix"]                       # named, actionable
    assert out["recommendations"][0]["applied"] is False
    assert out["side_effects"] == []


def test_ads_recommendation_with_campaign_ref_still_acts(monkeypatch):
    """The other half of T7: with a real campaign_ref the guard does not fire and the ref is
    forwarded untouched."""
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.ads_management.agent.request_side_effect", spy)
    agent = _stub_collection(AdsManagementAgent(), _ads_payload())
    out = agent.run({"title": "ads"}, {})
    assert len(spy.calls) == 1
    assert spy.last["payload"]["campaign_ref"] == "camp-1"
    assert not [r for r in out.get("refusals", []) if r["error"]["code"] == E_VALIDATION]


def test_support_reply_without_provider_ref_is_refused_before_any_firewall_call(monkeypatch):
    """No message_ref AND no message_id: named E_VALIDATION refusal, status stays draft, and
    neither evaluate_side_effect nor request_side_effect is ever called (a fabricated
    `msg:{index}` used to reach both)."""
    spy = _FirewallSpy()
    eval_spy = _DecisionSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    monkeypatch.setattr("app.agents.customer_support.agent.evaluate_side_effect", eval_spy)
    agent = _stub_collection(CustomerSupportAgent(), {"replies": [{
        "proposed_action": "refund", "reply_body": "refunding",
    }]})
    out = agent.run({"title": "reply"}, {})
    assert spy.calls == []
    assert eval_spy.calls == [], "the guard must run before any firewall evaluation"
    refusals = [r for r in out["refusals"] if r["error"]["code"] == E_VALIDATION]
    assert len(refusals) == 1
    assert "reply #0 has no provider message_ref" in refusals[0]["error"]["message"]
    assert out["replies"][0]["status"] == "draft"
    assert out["side_effects"] == []


def test_support_reply_with_message_id_only_uses_it_as_the_ref(monkeypatch):
    """`message_id` alone is a real provider id: the guard passes and that id is the ref."""
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    agent = _stub_collection(CustomerSupportAgent(), {"replies": [{
        "message_id": "mid-9", "proposed_action": "send_reply", "reply_body": "on it",
        "from": "customer@example.com", "subject": "help",
    }]})
    out = agent.run({"title": "reply"}, {})
    assert len(spy.calls) == 1
    assert spy.last["payload"]["ref"] == "mid-9"
    assert not [r for r in out.get("refusals", []) if r["error"]["code"] == E_VALIDATION]


# ------------------------------------------------------------------------- C. send producers


@pytest.mark.asyncio
async def test_record_published_posts_writes_real_sends_then_dedupes(async_db_session):
    result = {"posted": [
        {"platform": "twitter", "content": "first", "tweet_id": "tw-1"},
        {"platform": "twitter", "content": "second", "tweet_id": "tw-2"},
    ]}
    first = await social_service.record_published_posts(async_db_session, result)
    assert first == {"ok": True, "posts_written": 2, "duplicates_skipped": 0,
                     "skipped_invalid": 0, "error": None}
    rows = (await async_db_session.execute(
        select(SocialPost).order_by(SocialPost.id)
    )).scalars().all()
    assert [r.tweet_id for r in rows] == ["tw-1", "tw-2"]
    assert all(r.status == "published" and r.published_at is not None for r in rows)

    second = await social_service.record_published_posts(async_db_session, result)
    assert second["ok"] is True
    assert second["posts_written"] == 0
    assert second["duplicates_skipped"] == 2
    total = (await async_db_session.execute(
        select(func.count()).select_from(SocialPost)
    )).scalar()
    assert total == 2


@pytest.mark.asyncio
async def test_record_published_posts_never_counts_unsent_items(async_db_session):
    """Items without a provider id were not sent: skipped_invalid, no row — 'not sent' must
    stay distinguishable from 'sent'."""
    result = {"posted": [
        {"content": "no provider id"},
        {"platform": "twitter", "content": "empty id", "tweet_id": ""},
        "not-a-dict",
    ]}
    report = await social_service.record_published_posts(async_db_session, result)
    assert report["ok"] is True
    assert report["posts_written"] == 0
    assert report["skipped_invalid"] == 3
    total = (await async_db_session.execute(
        select(func.count()).select_from(SocialPost)
    )).scalar()
    assert total == 0


@pytest.mark.asyncio
async def test_record_sent_emails_creates_prospect_once_and_dedupes(async_db_session):
    result = {"emails": [
        {"prospect_email": "ada@example.com", "subject": "s1", "body": "b1",
         "status": "sent", "sendgrid_id": "sg-1"},
        {"prospect_email": "bob@example.com", "subject": "s2", "body": "b2",
         "status": "sent", "sendgrid_id": "sg-2"},
    ]}
    first = await outreach_service.record_sent_emails(async_db_session, result)
    assert first == {"ok": True, "emails_written": 2, "prospects_created": 2,
                     "duplicates_skipped": 0, "error": None}
    logs = (await async_db_session.execute(
        select(EmailLog).order_by(EmailLog.id)
    )).scalars().all()
    assert [log.sendgrid_id for log in logs] == ["sg-1", "sg-2"]
    assert all(log.status == "sent" and log.sent_at is not None for log in logs)
    addresses = (await async_db_session.execute(select(Prospect.email))).scalars().all()
    assert sorted(addresses) == ["ada@example.com", "bob@example.com"]

    second = await outreach_service.record_sent_emails(async_db_session, result)
    assert second["ok"] is True
    assert second["emails_written"] == 0
    assert second["duplicates_skipped"] == 2
    assert second["prospects_created"] == 0
    assert (await async_db_session.execute(
        select(func.count()).select_from(EmailLog)
    )).scalar() == 2


@pytest.mark.asyncio
async def test_record_sent_emails_reuses_an_existing_prospect(async_db_session):
    result = {"emails": [
        {"prospect_email": "same@example.com", "subject": "1", "body": "1",
         "status": "sent", "sendgrid_id": "sg-a"},
        {"prospect_email": "same@example.com", "subject": "2", "body": "2",
         "status": "sent", "sendgrid_id": "sg-b"},
    ]}
    report = await outreach_service.record_sent_emails(async_db_session, result)
    assert report["emails_written"] == 2
    assert report["prospects_created"] == 1
    prospect_ids = (await async_db_session.execute(
        select(EmailLog.prospect_id)
    )).scalars().all()
    assert len(set(prospect_ids)) == 1
    assert (await async_db_session.execute(
        select(func.count()).select_from(Prospect)
    )).scalar() == 1


@pytest.mark.asyncio
async def test_record_sent_emails_recovers_when_the_pre_select_loses_a_race(
    async_db_session, monkeypatch
):
    """The duplicate-refusal re-select path: a create attempt that loses a race (the row appears
    between the SELECT and the INSERT) still records the email against the EXISTING prospect.
    The savepoint in `_find_or_create_prospect` keeps the failed flush from poisoning the
    session (evidence: raw/probe_dup_prospect.py vs raw/probe_dup_savepoint.py)."""
    kept = await outreach_service.create_prospect(async_db_session, "raced@example.com")

    real_fetch_scalar = outreach_service.fetch_scalar
    calls = {"n": 0}

    async def losing_pre_select(db, stmt, *, what):
        calls["n"] += 1
        if calls["n"] <= 2:      # call 1: log dedupe, call 2: prospect pre-select
            return None          # both "find nothing", as if the race was won elsewhere
        return await real_fetch_scalar(db, stmt, what=what)

    monkeypatch.setattr(outreach_service, "fetch_scalar", losing_pre_select)
    report = await outreach_service.record_sent_emails(async_db_session, {"emails": [{
        "prospect_email": "raced@example.com", "subject": "s", "body": "b",
        "status": "sent", "sendgrid_id": "sg-race",
    }]})
    assert report["ok"] is True
    assert report["emails_written"] == 1
    assert report["prospects_created"] == 0    # found on the re-select after the refusal
    assert calls["n"] >= 3                     # ... and only after the duplicate was refused
    prospect_id = (await async_db_session.execute(
        select(EmailLog.prospect_id)
    )).scalar_one()
    assert prospect_id == kept.id


@pytest.mark.asyncio
async def test_record_sent_emails_ignores_unperformed_sends(async_db_session):
    """Draft / failed / no-provider-id items are not sends: nothing recorded, report still ok."""
    result = {"emails": [
        {"prospect_email": "a@example.com", "status": "draft", "sendgrid_id": None},
        {"prospect_email": "b@example.com", "status": "failed", "sendgrid_id": "sg-f"},
        {"prospect_email": "c@example.com", "status": "sent", "sendgrid_id": ""},
    ]}
    report = await outreach_service.record_sent_emails(async_db_session, result)
    assert report["ok"] is True
    assert report["emails_written"] == 0
    assert (await async_db_session.execute(
        select(func.count()).select_from(EmailLog)
    )).scalar() == 0
    assert (await async_db_session.execute(
        select(func.count()).select_from(Prospect)
    )).scalar() == 0


@pytest.mark.asyncio
async def test_record_sent_emails_broken_input_is_a_named_error_not_a_crash(async_db_session):
    """A 'sent' email with no usable prospect_email cannot be linked (prospect_id is a required
    FK): the failure is counted in the report's error field, and nothing raises."""
    result = {"emails": [{"status": "sent", "sendgrid_id": "sg-9", "prospect_email": ""}]}
    report = await outreach_service.record_sent_emails(async_db_session, result)
    assert report["ok"] is False
    assert report["emails_written"] == 0
    assert report["error"]["code"] == E_VALIDATION
    assert "prospect" in report["error"]["message"]
    assert (await async_db_session.execute(
        select(func.count()).select_from(EmailLog)
    )).scalar() == 0
