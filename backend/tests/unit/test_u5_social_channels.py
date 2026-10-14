"""U5/P1 — social platform coverage: the dispatch table + the LinkedIn/Meta adapters, pinned.

What this pins (and why it can only pass after U5/P1 — the RED legs):
  * `post_tweet(platform="linkedin")` routes to a REAL LinkedIn adapter. Pre-U5 it refused with
    "post_tweet adapter does not handle platform 'linkedin'" (the old guard at :42-47), so the
    named-refusal test below fails on the old bytes (RED leg 1);
  * `post_tweet(platform="facebook"/"instagram")` route to the Meta adapters (RED leg 2);
  * the adapters refuse BY NAME when unconfigured (E_INTEGRATION + `how_to_fix` + `missing` naming
    every variable), when instagram lacks its REQUIRED `image_url` (E_VALIDATION, provider never
    called), and when the provider itself errors (provider text kept);
  * `RateCaps.per_channel_day` carries the documented per-platform numbers, the social_media
    capability covers every shipped platform, and the schema Literal admits every wired platform
    (facebook/instagram here; tiktok/youtube added by V8 — `test_v8_tiktok_youtube.py`) while
    still rejecting an unwired platform.

Doubles are test-only and named. `sys.modules["httpx"]` is a fake whose call surface mirrors the real
client (`post(url, headers=/json=/data=, timeout=)` -> response with `.status_code`, `.headers`,
`.text`, `.json()`); the SETTINGS surface is a `SimpleNamespace` so each drill controls the exact
credential values (the adapters read the `*_access_token` / `*_urn` settings through `_setting`,
which falls back to "" for anything absent and then refuses by name).
No test can touch the network: the provider legs are only ever reached with the fake installed.
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from pydantic import ValidationError

import app.config as app_config
from app.agents import integrations
from app.agents.policy_firewall import AGENT_CAPABILITIES, RateCaps
from app.core.errors import E_INTEGRATION, E_VALIDATION
from app.schemas.agent_outputs import PostedItem, SocialPostDraft

LINKEDIN_ENV = ("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN")
META_ENV = ("META_PAGE_ACCESS_TOKEN", "META_FACEBOOK_PAGE_ID", "META_INSTAGRAM_USER_ID")


class _FakeHTTPX:
    """Provider double for `httpx.post`: records every call, replays scripted responses.

    Response shape mirrors what the real adapters consume: `status_code`, case-sensitive-insensitive
    `headers` (a plain dict — the adapters read headers case-insensitively on purpose), `.text` for
    the error snippet, `.json()` for the Graph id. Scripted responses are consumed in order; when the
    script runs out a permissive default is served so routing tests do not have to script the happy
    shape.
    """

    def __init__(self, *responses: tuple[int, dict[str, str], str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeHTTPX:
        fake = self

        class _Response:
            def __init__(self, status_code: int, headers: dict[str, str], text: str) -> None:
                self.status_code = status_code
                self.headers = headers
                self.text = text

            def json(self) -> Any:
                return json.loads(self.text)

        def post(url: str, **kwargs: Any) -> _Response:
            fake.calls.append({"url": url, **kwargs})
            if fake.responses:
                status, headers, text = fake.responses.pop(0)
                return _Response(status, headers, text)
            if "linkedin" in url:
                return _Response(201, {"x-restli-id": "urn:li:share:7001"}, "")
            return _Response(200, {}, '{"id": "graph-1"}')

        module = types.ModuleType("httpx")
        module.post = post                              # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "httpx", module)
        return fake


def _linkedin_settings(**overrides: str) -> types.SimpleNamespace:
    values = {"linkedin_access_token": "li-token", "linkedin_author_urn": "urn:li:person:42"}
    values.update(overrides)
    return types.SimpleNamespace(**{k: v for k, v in values.items() if v is not None})


def _meta_settings(**overrides: str) -> types.SimpleNamespace:
    values = {
        "meta_page_access_token": "meta-token",
        "meta_facebook_page_id": "page-123",
        "meta_instagram_user_id": "ig-456",
    }
    values.update(overrides)
    return types.SimpleNamespace(**{k: v for k, v in values.items() if v is not None})


# ── LinkedIn: the named-refusal drill (RED leg 1) ────────────────────────────────────────────────

def test_linkedin_unconfigured_refuses_by_name_naming_every_variable(monkeypatch):
    """No LINKEDIN_* settings -> E_INTEGRATION, every missing var named, a real how_to_fix.

    Pre-U5 this call answered `E_VALIDATION ... does not handle platform 'linkedin'` — the RED state.
    """
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace())
    out = integrations.post_tweet("hello", platform="linkedin")
    assert out["ok"] is False
    err = out["error"]
    assert err["code"] == E_INTEGRATION
    assert err["context"]["missing"] == list(LINKEDIN_ENV)
    for name in LINKEDIN_ENV:
        assert name in err["message"]
    assert "LINKEDIN_ACCESS_TOKEN" in err["how_to_fix"]


def test_linkedin_configured_routes_through_the_dispatch_table_to_the_real_adapter(monkeypatch):
    """Configured: the REAL adapter runs (fake httpx) — real URL, real headers, real entity body."""
    monkeypatch.setattr(integrations, "settings", _linkedin_settings())
    fake = _FakeHTTPX().install(monkeypatch)

    out = integrations.post_tweet("hello linkedin", platform="linkedin")

    assert out == {"ok": True, "result": {"id": "urn:li:share:7001"}}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://api.linkedin.com/rest/posts"
    assert call["headers"]["Authorization"] == "Bearer li-token"
    assert call["headers"]["LinkedIn-Version"]
    assert call["json"]["author"] == "urn:li:person:42"
    assert call["json"]["commentary"] == "hello linkedin"
    assert call["json"]["lifecycleState"] == "PUBLISHED"
    assert call["json"]["visibility"] == "PUBLIC"


def test_linkedin_provider_error_keeps_the_provider_text(monkeypatch):
    """A non-2xx from LinkedIn is a named E_INTEGRATION refusal carrying the provider's own text."""
    monkeypatch.setattr(integrations, "settings", _linkedin_settings())
    _FakeHTTPX((401, {}, '{"message":"Invalid access token"}')).install(monkeypatch)

    out = integrations.post_tweet("hello", platform="linkedin")

    assert out["ok"] is False
    assert out["error"]["code"] == E_INTEGRATION
    assert "401" in out["error"]["message"]
    assert "Invalid access token" in out["error"]["message"]


# ── Meta Facebook + Instagram (one unit, one app/token) ─────────────────────────────────────────

def test_facebook_and_instagram_unconfigured_refuse_by_name(monkeypatch):
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace())

    fb = integrations.post_tweet("hello", platform="facebook")
    assert fb["error"]["code"] == E_INTEGRATION
    assert fb["error"]["context"]["missing"] == ["META_PAGE_ACCESS_TOKEN", "META_FACEBOOK_PAGE_ID"]
    for name in META_ENV:
        assert name in fb["error"]["how_to_fix"]

    ig = integrations.post_tweet("hello", platform="instagram", image_url="https://cdn.example.com/i.jpg")
    assert ig["error"]["code"] == E_INTEGRATION
    assert ig["error"]["context"]["missing"] == ["META_PAGE_ACCESS_TOKEN", "META_INSTAGRAM_USER_ID"]
    assert "META_INSTAGRAM_USER_ID" in ig["error"]["how_to_fix"]


def test_facebook_configured_posts_to_the_page_feed(monkeypatch):
    monkeypatch.setattr(integrations, "settings", _meta_settings())
    fake = _FakeHTTPX().install(monkeypatch)

    out = integrations.post_tweet("hello page", platform="facebook")

    assert out == {"ok": True, "result": {"id": "graph-1"}}
    call = fake.calls[0]
    assert call["url"] == "https://graph.facebook.com/v21.0/page-123/feed"
    assert call["data"]["message"] == "hello page"
    assert call["data"]["access_token"] == "meta-token"


def test_instagram_requires_an_image_url_and_never_calls_the_provider(monkeypatch):
    """IG's Content Publishing API has NO text-only post: refuse by name, never a silent drop."""
    monkeypatch.setattr(integrations, "settings", _meta_settings())
    fake = _FakeHTTPX().install(monkeypatch)

    out = integrations.post_tweet("hello", platform="instagram")

    assert out["ok"] is False
    assert out["error"]["code"] == E_VALIDATION
    assert "image_url" in out["error"]["message"]
    assert out["error"]["how_to_fix"]
    assert fake.calls == [], "an unpostable item must not reach the provider"


def test_instagram_configured_runs_the_two_step_container_then_publish(monkeypatch):
    monkeypatch.setattr(integrations, "settings", _meta_settings())
    fake = _FakeHTTPX(
        (201, {}, '{"id": "creation-77"}'),
        (200, {}, '{"id": "media-99"}'),
    ).install(monkeypatch)

    out = integrations.post_tweet(
        "ig caption", platform="instagram", image_url="https://cdn.example.com/pic.jpg"
    )

    assert out == {"ok": True, "result": {"id": "media-99"}}
    assert len(fake.calls) == 2
    container, publish = fake.calls
    assert container["url"] == "https://graph.facebook.com/v21.0/ig-456/media"
    assert container["data"]["image_url"] == "https://cdn.example.com/pic.jpg"
    assert container["data"]["caption"] == "ig caption"
    assert publish["url"] == "https://graph.facebook.com/v21.0/ig-456/media_publish"
    assert publish["data"]["creation_id"] == "creation-77"


# ── dispatch: media refusals, unknown platforms — named refusals, never a crash ─────────────────

def test_media_posts_refuse_by_name_on_text_only_adapters():
    """A post carrying an image on a text-only adapter is refused by name — never silently dropped."""
    twitter = integrations.post_tweet("x", platform="twitter", image_url="https://cdn.example.com/i.jpg")
    assert twitter["ok"] is False
    assert twitter["error"]["code"] == E_VALIDATION
    assert "twitter" in twitter["error"]["message"]

    facebook = integrations.post_tweet("x", platform="facebook", image_url="https://cdn.example.com/i.jpg")
    assert facebook["ok"] is False
    assert facebook["error"]["code"] == E_VALIDATION
    assert "facebook" in facebook["error"]["message"]


def test_unknown_platform_is_a_named_refusal_not_a_crash(capsys):
    """An unadapted platform stays the PINNED refusal text (E_VALIDATION, platform in context) and
    the how_to_fix now names the supported set from the live dispatch table."""
    out = integrations.post_tweet("hello", platform="mastodon")
    assert out["ok"] is False
    err = out["error"]
    assert err["code"] == E_VALIDATION
    assert "does not handle platform 'mastodon'" in err["message"]
    assert err["context"] == {"platform": "mastodon"}
    assert "linkedin" in err["how_to_fix"] and "facebook" in err["how_to_fix"]
    assert capsys.readouterr().err == "", "a refusal path never writes to stderr"


# ── policy surface: caps rows, capability channels ───────────────────────────────────────────────

def test_rate_caps_carry_the_documented_platform_rows():
    """The per-day numbers, platform by platform — now including the U5 channels."""
    caps = RateCaps()
    assert caps.per_channel_day["facebook"] == 3
    assert caps.per_channel_day["instagram"] == 5
    assert caps.per_channel_day["tiktok"] == 10
    assert caps.per_channel_day["youtube"] == 3
    assert caps.per_channel_day["linkedin"] == 4          # unchanged (already shipped)
    assert caps.per_channel_hour == {"email": 15, "twitter": 3, "github": 2}, (
        "no hour cap was invented for a platform without a documented hourly number"
    )


def test_social_capability_channels_cover_every_platform_the_plan_names():
    channels = AGENT_CAPABILITIES["social_media"].channels
    assert channels == ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube")


# ── schema: additive only — wired platforms admitted, unwired platforms still rejected ───────────

def test_schema_admits_the_new_platforms_and_keeps_unwired_ones_out():
    facebook = SocialPostDraft(platform="facebook", content="hello")
    assert facebook.platform == "facebook" and facebook.image_url is None
    instagram = SocialPostDraft(platform="instagram", content="hello",
                                image_url="https://cdn.example.com/i.jpg")
    assert instagram.image_url == "https://cdn.example.com/i.jpg"
    assert PostedItem(platform="facebook", content="gone out", tweet_id="1_2").tweet_id == "1_2"
    # V8/T4 pin move (old -> new): tiktok was the pinned "not yet wired" platform until its
    # adapter landed — that acceptance now lives in `test_v8_tiktok_youtube.py`; the promise
    # stays honest with a platform that has no adapter at all.
    with pytest.raises(ValidationError):
        SocialPostDraft(platform="mastodon", content="not wired anywhere")


# ── agent level: sandbox turns no-adapter attempts into labelled drafts (never a raise) ──────────

def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def test_sandboxed_facebook_and_tiktok_attempts_become_labelled_drafts(monkeypatch):
    """Sandbox is the firewall's answer for every channel, adapter or not: a labelled draft, no
    raise, and the integration is never reached. (tiktok gained its adapter in V8 — the sandbox
    semantics are platform-independent.)"""
    from app.agents.social_media.agent import SocialMediaAgent

    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)

    def _explode(*args: Any, **kwargs: Any) -> dict:
        raise AssertionError("sandboxed run must never reach the integration")

    monkeypatch.setattr(integrations, "post_tweet", _explode)
    agent = _stub_collection(SocialMediaAgent(), {"action": "post", "posts": [
        {"platform": "facebook", "content": "fb draft"},
        {"platform": "tiktok", "content": "sandboxed tiktok draft"},
    ]})
    out = agent.run({"title": "post"}, {})

    assert out["sandbox"] is True
    assert out["drafted_not_sent"] is True
    assert out["posted"] == []
    assert [r["sandboxed"] for r in out["side_effects"]] == [True, True]
