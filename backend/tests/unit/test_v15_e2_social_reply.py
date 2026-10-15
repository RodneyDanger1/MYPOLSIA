"""4-04 (S5-04) — social replies: REAL threading + ZERO fabricated refs — RED-first legs.

BUG-DIGEST §C10 measured `social_media/agent.py:211`'s `f"mention:{index}"`: a reply ref no
provider ever issued, written into the run payload whenever the model omitted `mention_id` — the
same fabrication class T7 killed for CS/ads refs. Pinned here:

  * the twitter adapter is really REPLY-CAPABLE (`post_tweet(..., reply_to=<tweet id>)` ->
    `create_tweet(in_reply_to_tweet_id=...)`), so a reply with a real mention id really threads;
    the other platform adapters refuse `reply_to` BY NAME (no silent drop, no fake threading);
  * the agent never fabricates a mention ref: a reply without a provider `mention_id` is refused
    (`E_VALIDATION`) BEFORE the firewall — it never reaches the provider and its `reply_id`
    stays None; the `mention:{index}` string is pinned ABSENT at source level;
  * a reply with a real mention id carries it into the provider call (`in_reply_to_tweet_id`) and
    records the provider's OWN reply id.

The mentions SOURCE (a provider timeline read) is NOT built in this wave — the roster copy
handoff (H6 -> W1) removes the "monitors engagement" claim; this file pins the capability the
runtime really ships.
"""
from __future__ import annotations

import inspect
import sys
import types
from typing import Any

import app.config as app_config
import app.models  # noqa: F401
from app.agents import integrations
from app.agents.side_effects import SideEffectOutcome
from app.agents.social_media import agent as social_agent_mod
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_VALIDATION


def _install_fake_tweepy(monkeypatch: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    class _Response:
        def __init__(self, tweet_id: str) -> None:
            self.data = {"id": tweet_id}

    class _Client:
        def __init__(self, **creds: Any) -> None:
            self.creds = creds

        def create_tweet(self, *, text: str, **kwargs: Any) -> _Response:
            sent.append({"text": text, **kwargs})
            return _Response(f"tweet-{len(sent)}")

    module = types.ModuleType("tweepy")
    module.Client = _Client                                  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tweepy", module)
    return sent


def _arm_twitter(monkeypatch: Any) -> None:
    monkeypatch.setattr(app_config.settings, "twitter_api_key", "ck")
    monkeypatch.setattr(app_config.settings, "twitter_api_secret", "cs")
    monkeypatch.setattr(app_config.settings, "twitter_access_token", "at")
    monkeypatch.setattr(app_config.settings, "twitter_access_secret", "as")


def _allowed_outcome(*args: Any, **kwargs: Any) -> SideEffectOutcome:
    return SideEffectOutcome(allowed=True, sandboxed=False, requires_approval=False)


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


# ── 1. the adapter: real threading on twitter, named refusal elsewhere ─────────────────────────


def test_the_twitter_adapter_threads_a_reply_with_the_real_in_reply_to_id(monkeypatch) -> None:
    sent = _install_fake_tweepy(monkeypatch)
    _arm_twitter(monkeypatch)

    out = integrations.post_tweet("thanks for the kind words", platform="twitter", reply_to="55")

    assert out["ok"] is True
    assert sent[0]["in_reply_to_tweet_id"] == "55"
    assert sent[0]["text"] == "thanks for the kind words"
    assert out["result"]["id"] == "tweet-1"


def test_a_plain_text_post_keeps_its_exact_call_shape(monkeypatch) -> None:
    sent = _install_fake_tweepy(monkeypatch)
    _arm_twitter(monkeypatch)

    out = integrations.post_tweet("hello world", platform="twitter")

    assert out["ok"] is True
    assert "in_reply_to_tweet_id" not in sent[0]


def test_platforms_without_threading_refuse_reply_to_by_name() -> None:
    out = integrations.post_tweet("hi", platform="linkedin", reply_to="9")
    assert out["ok"] is False
    assert out["error"]["code"] == E_VALIDATION
    assert "does not support replies" in out["error"]["message"]
    assert "twitter" in out["error"]["how_to_fix"]


# ── 2. the agent: no fabricated refs; real threading end to end ────────────────────────────────


def test_a_reply_without_a_real_mention_id_is_refused_and_never_reaches_the_provider(
    monkeypatch,
) -> None:
    calls: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> SideEffectOutcome:
        calls.append((args, kwargs))
        return _allowed_outcome()

    monkeypatch.setattr(social_agent_mod, "request_side_effect", spy)
    payload = {"summary": "reply to a mention", "action": "reply",
               "replies": [{"reply": "thanks!"}]}

    out = _stub_collection(SocialMediaAgent(), payload).run({"title": "mentions"}, {})

    assert calls == [], "a reply without a real mention id must never reach the firewall/provider"
    refusals = [r for r in out["refusals"] if r["error"]["code"] == E_VALIDATION]
    assert len(refusals) == 1
    assert "mention_id" in refusals[0]["error"]["message"]
    assert out["replies"][0]["reply_id"] is None


def test_a_reply_with_a_real_mention_id_threads_and_records_the_provider_reply_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(social_agent_mod, "request_side_effect", _allowed_outcome)
    captured: dict[str, Any] = {}

    def fake_post(content: str, *, platform: str = "twitter", image_url: str = "",
                  reply_to: str = "") -> dict[str, Any]:
        captured.update(content=content, platform=platform, reply_to=reply_to)
        return {"ok": True, "result": {"id": "900"}}

    monkeypatch.setattr(social_agent_mod.integrations, "post_tweet", fake_post)
    payload = {"summary": "reply to a real mention", "action": "reply",
               "replies": [{"mention_id": "55", "reply": "thanks!"}]}

    out = _stub_collection(SocialMediaAgent(), payload).run({"title": "mentions"}, {})

    assert captured["reply_to"] == "55", "the reply must thread on the real mention id"
    assert captured["platform"] == "twitter"
    assert captured["content"] == "thanks!"
    assert out["replies"][0]["reply_id"] == "900", "the PROVIDER's reply id, never an invention"


def test_the_mention_index_fabrication_is_gone_from_source() -> None:
    """The §C10 string is pinned ABSENT — a fabricated ref cannot come back as a literal."""
    source = inspect.getsource(social_agent_mod)
    assert 'f"mention:{index}"' not in source
    assert "mention:{index}" not in source
    assert "no provider mention_id" in source
