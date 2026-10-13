"""L3-01 / L3-02 / L3-05 / L3-07 — AGENT RUNTIME TRUTH: run-action gates, check-and-consume
channel caps, and the provider-issued reply id.

What this file pins (findings lane-3 + research-C, phase-3 fixer-3):

* L3-01 — a run whose own `action` is "draft"/"noop" attempts NOTHING: no firewall call, no
  provider call, `posted == []`, and the payload says so (`drafted_not_sent`). `action="post"`
  still traverses the firewall/caps/provider exactly as before (the gate removes disclaimed
  intent, not the machinery).
* L3-02 — `email_outreach` sends only on `action="send"`; "find"/"draft"/"noop" record the
  labelled draft and never touch the firewall or SendGrid.
* L3-01 (weaker variant) — `code_generation` attempts a PR only on `action="open_pr"`.
* L3-05 — the channel caps are check-and-consume INSIDE a run: what this run has already
  performed is added to the pre-run snapshot before every firewall call, and incremented only
  after a performed AND successful provider call. A cap denial with nothing performed stays
  fatal (the shipped contract, `tests/unit/test_channel_agent_caps.py`); a cap denial AFTER a
  real send is recorded as a named refusal (`E_RATE_LIMITED`, POLSIA-E117) and the item stays a
  draft — so the performed work still persists and a retryable refusal cannot re-run the batch.
* L3-07 — the reply's `reply_id` is the PROVIDER's id (or None), never the model's string, and
  the sandbox guard covers replies too.

No sockets, no servers: adapters are replaced in memory, the firewall runs for real.
"""
from __future__ import annotations

from typing import Any

import pytest

import app.config as app_config
from app.agents import integrations
from app.agents.code_generation.agent import CodeGenerationAgent
from app.agents.email_outreach.agent import EmailOutreachAgent
from app.agents.side_effects import SideEffectOutcome
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_POLICY_DENIED, E_RATE_LIMITED, PolsiaError

# --------------------------------------------------------------------------- helpers


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Give the agent the payload its CLI collection would have produced (no CLI is spawned)."""
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent


def _fake_post_tweet(calls: list[str]) -> Any:
    def _post(text: str, *, platform: str = "twitter") -> dict[str, Any]:
        calls.append(text)
        return {"ok": True, "result": {"id": f"provider-{len(calls)}"}}

    return _post


# --------------------------------------------------------------------------- L3-01


def test_social_draft_run_performs_no_external_action(monkeypatch):
    """A `draft` run writes its posts and replies but publishes nothing (probe1's RED case)."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "draft",
        "posts": [{"platform": "twitter", "content": "drafted post"}],
        "replies": [{"mention_id": "m1", "reply": "drafted reply", "reply_id": "model-invented"}],
    })

    out = agent.run({"title": "draft today's posts"},
                    {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert calls == [], "a run that decided 'publish nothing yet' must not publish"
    assert out["posted"] == []
    assert out["side_effects"] == []
    assert out["drafted_not_sent"] is True
    # the written drafts still exist as data, exactly as today
    assert out["tweets"] == ["drafted post"]
    assert out["replies"][0]["reply_id"] is None, "never the model's invented id"


def test_social_post_action_publishes_through_the_firewall_and_reports_provider_ids(monkeypatch):
    """The claimed action still performs: firewall -> provider -> provider ids in the payload."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post",
        "posts": [{"platform": "twitter", "content": "real post"}],
        "replies": [{"mention_id": "m1", "reply": "real reply", "reply_id": "model-invented"}],
    })

    out = agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert calls == ["real post", "real reply"]
    assert [p["tweet_id"] for p in out["posted"]] == ["provider-1"]
    assert [r["performed"] for r in out["side_effects"]] == [True, True]


def test_social_noop_run_performs_no_external_action(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {"action": "noop", "posts": [], "replies": []})

    out = agent.run({"title": "noop"}, {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert calls == []
    assert out["drafted_not_sent"] is True


# --------------------------------------------------------------------------- L3-02


def test_email_find_run_sends_nothing(monkeypatch):
    """`action="find"` is prospects-only: the email is a labelled draft, SendGrid never called."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    sent: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to})
        return {"ok": True, "result": {"message_id": "sg-1", "status_code": 202}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    agent = _stub_collection(EmailOutreachAgent(), {
        "action": "find",
        "prospects": [{"email": "ceo@example.com", "source": "probe"}],
        "emails": [{"prospect_email": "ceo@example.com", "subject": "hi", "body": "b"}],
    })

    out = agent.run({"title": "find prospects"},
                    {"channel_sends": {"email": {"today": 0, "hour": 0}}})

    assert sent == []
    assert out["emails"][0]["status"] == "draft"
    assert out["emails"][0]["sendgrid_id"] is None
    assert out["drafted_not_sent"] is True
    assert out["prospects"] == [{"email": "ceo@example.com", "source": "probe"}], (
        "a find run still returns its prospects"
    )


def test_email_send_action_still_sends(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    sent: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to})
        return {"ok": True, "result": {"message_id": "sg-real"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    agent = _stub_collection(EmailOutreachAgent(), {
        "action": "send",
        "emails": [{"prospect_email": "ceo@example.com", "subject": "hi", "body": "b"}],
    })

    out = agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 0, "hour": 0}}})

    assert sent == [{"to": "ceo@example.com"}]
    assert out["emails"][0]["status"] == "sent"
    assert out["emails"][0]["sendgrid_id"] == "sg-real"


# ------------------------------------------------------------ L3-01 code_generation variant


def test_code_generation_plan_run_does_not_attempt_a_pull_request(monkeypatch):
    """A `plan` run with a filled `changes[]` must not attempt the irreversible PR action."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    from app.agents.code_generation import agent as codegen_agent

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a plan run must not reach the firewall for a PR")

    monkeypatch.setattr(codegen_agent, "request_side_effect", forbidden)
    agent = _stub_collection(CodeGenerationAgent(), {
        "action": "plan",
        "changes": [{"repo": "acme/site", "branch": "feat/x", "pr_title": "t"}],
    })

    out = agent.run({"title": "plan the change"}, {})

    assert out["side_effects"] == []
    assert out.get("pr_url") is None


def test_code_generation_open_pr_action_still_attempts_through_the_firewall(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    from app.agents.code_generation import agent as codegen_agent

    calls: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> SideEffectOutcome:
        calls.append(kwargs)
        return SideEffectOutcome(
            allowed=True, sandboxed=False, requires_approval=False,
            policy_code="POLSIA-E000", reason="test stub: approved",
        )

    monkeypatch.setattr(codegen_agent, "request_side_effect", spy)
    monkeypatch.setattr(
        integrations, "open_pull_request",
        lambda **kw: {"ok": True, "result": {"url": "https://example.test/pr/7", "number": 7}},
    )
    agent = _stub_collection(CodeGenerationAgent(), {
        "action": "open_pr",
        "changes": [{"repo": "acme/site", "branch": "feat/x", "pr_title": "t"}],
    })

    out = agent.run({"title": "open the pr"},
                    {"channel_sends": {"github": {"today": 0, "hour": 0}}})

    assert len(calls) == 1
    assert calls[0]["sends_today"] == 0, "a measured snapshot is forwarded"
    assert out["pr_url"] == "https://example.test/pr/7"


# --------------------------------------------------------------------------- L3-05


def test_social_batch_sends_exactly_one_then_records_the_cap_refusals(monkeypatch):
    """The probe6-A case: caps 12/day + 3/hour, snapshot 11/2, batch of 5 -> 1 send + 4 refusals."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post",
        "posts": [{"platform": "twitter", "content": f"post {i}"} for i in range(3)],
        "replies": [{"mention_id": f"m{i}", "reply": f"reply {i}"} for i in range(2)],
    })

    out = agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 11, "hour": 2}}})

    assert len(calls) == 1, "one slot was left; the batch must consume exactly that one"
    assert len(out["posted"]) == 1
    refusals = [r for r in out["refusals"] if r["error"]["code"] == E_RATE_LIMITED]
    assert len(refusals) == 4, "every remaining item is refused BY NAME, not silently dropped"
    assert all("cap for twitter" in r["error"]["message"] for r in refusals)
    receipts = out["side_effects"]
    assert [r["performed"] for r in receipts] == [True, False, False, False, False]
    assert all(r["policy_code"] == "POLSIA-E117" for r in receipts[1:])
    assert out["drafted_not_sent"] is True


def test_social_cap_refusal_with_nothing_performed_stays_fatal(monkeypatch):
    """The shipped contract is untouched: a run dispatched into a full channel raises, loudly."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post", "posts": [{"platform": "twitter", "content": "one too many"}],
    })

    with pytest.raises(PolsiaError) as excinfo:
        agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 12, "hour": 3}}})

    assert excinfo.value.code == E_RATE_LIMITED
    assert "12/12" in excinfo.value.message
    assert calls == []


def test_a_failed_integration_and_a_sandbox_refusal_consume_no_cap_slot(monkeypatch):
    """Only a performed AND successful provider call consumes a slot (spy on what the firewall sees)."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)  # sandbox: nothing performed
    from app.agents.email_outreach import agent as outreach_agent

    real = outreach_agent.request_side_effect
    seen: list[int | None] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("sends_today"))
        return real(*args, **kwargs)

    monkeypatch.setattr(outreach_agent, "request_side_effect", spy)
    agent = _stub_collection(EmailOutreachAgent(), {
        "action": "send",
        "emails": [{"prospect_email": f"p{i}@example.com", "subject": "s", "body": "b"}
                   for i in range(3)],
    })

    agent.run({"title": "send"}, {"channel_sends": {"email": {"today": 40, "hour": 5}}})

    assert seen == [40, 40, 40], "sandboxed attempts performed nothing; the count must not move"


def test_none_snapshot_plus_performed_stays_none(monkeypatch):
    """`None` (no local record) must never collapse into a number, even with a performed counter."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    from app.agents.social_media import agent as social_agent

    real = social_agent.request_side_effect
    seen: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((kwargs.get("sends_today"), kwargs.get("sends_this_hour")))
        return real(*args, **kwargs)

    monkeypatch.setattr(social_agent, "request_side_effect", spy)
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post", "posts": [{"platform": "twitter", "content": "x"}],
    })

    with pytest.raises(PolsiaError) as excinfo:  # unknown count -> fail closed (E114)
        agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": None, "hour": None}}})

    assert excinfo.value.code == E_POLICY_DENIED
    assert seen == [(None, None)]


def test_performed_sends_shift_the_counts_the_firewall_sees(monkeypatch):
    """Check-and-consume, measured: with headroom, each performed send advances the forwarded count."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    from app.agents.social_media import agent as social_agent

    real = social_agent.request_side_effect
    seen: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("sends_today"))
        return real(*args, **kwargs)

    monkeypatch.setattr(social_agent, "request_side_effect", spy)
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post",
        "posts": [{"platform": "twitter", "content": f"p{i}"} for i in range(3)],
    })

    out = agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 9, "hour": 0}}})

    assert seen == [9, 10, 11], "each performed send advances what the next firewall call sees"
    assert len(out["posted"]) == 3


# --------------------------------------------------------------------------- L3-07


def test_reply_id_carries_the_providers_id_not_the_models(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "reply",
        "replies": [{"mention_id": "m1", "reply": "thanks!", "reply_id": "model-invented-999"}],
    })

    out = agent.run({"title": "reply"}, {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert calls == ["thanks!"]
    assert out["replies"][0]["reply_id"] == "provider-1", "the provider issued this id"


def test_sandbox_run_nulls_reply_id_never_the_models_string(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "reply",
        "replies": [{"mention_id": "m1", "reply": "thanks!", "reply_id": "model-invented-999"}],
    })

    out = agent.run({"title": "reply"}, {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert out["replies"][0]["reply_id"] is None


def test_the_sandbox_guard_covers_every_published_item_including_replies(monkeypatch):
    """L3-07: the guard used to inspect `posts[0]` only; replies were structurally invisible."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    calls: list[str] = []
    monkeypatch.setattr(integrations, "post_tweet", _fake_post_tweet(calls))
    from app.agents.social_media import agent as social_agent

    guarded: list[dict[str, Any]] = []

    def guard_spy(item: dict[str, Any], *, agent_type: str, keys: tuple[str, ...]) -> None:
        guarded.append(dict(item))

    monkeypatch.setattr(social_agent, "assert_not_reporting_real_side_effect", guard_spy)
    agent = _stub_collection(SocialMediaAgent(), {
        "action": "post",
        "posts": [{"platform": "twitter", "content": "p"}],
        "replies": [{"mention_id": "m1", "reply": "r"}],
    })

    agent.run({"title": "post"}, {"channel_sends": {"twitter": {"today": 0, "hour": 0}}})

    assert len(guarded) == 2, "post AND reply are guarded"
    assert {item.get("tweet_id") for item in guarded} == {"provider-1", "provider-2"}
