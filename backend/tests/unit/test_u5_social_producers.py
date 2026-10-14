"""U5/P1 — the new channels' producer legs: real send -> real producer -> real cap fires.

Same discipline as `test_send_cap_producer_e2e.py` (RD14): every leg goes through the REAL functions —
`SocialMediaAgent` -> the REAL dispatch table + adapter (provider SDK faked, never the adapter) ->
`social_service.record_published_posts` -> `channel_service.channel_sends_snapshot` -> the next send
is DENIED by the live `RateCaps` ("3/3", "4/4", "5/5"). The document numbers (FB 3/IG 5 day caps,
LinkedIn 4) are the ones RateCaps now ships.

RED legs: pre-U5 the agent could not dispatch any of these platforms at all (the adapter refused), so
no row below could exist and every "cap fires" assertion fails on the old bytes.

The unrun platforms (tiktok/youtube) get their OWN pin at the bottom: their caps are configured
(policy readiness for the caps route) while no adapter can write a row — the count stays UNKNOWN
(`None`, never a fabricated 0) and the firewall refuses an attempted send BY NAME. That is the
honest state of an unadapted channel, and it flips to "cap fires" the day the adapter lands and
joins `channel_service._SOCIAL_CHANNELS`.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.config as app_config
from app.agents import integrations
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import request_side_effect
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_POLICY_DENIED, E_RATE_LIMITED, PolsiaError
from app.services import social_service
from app.services.channel_service import channel_sends_snapshot

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)     # inside the day window, outside the hour window


class _FakeHTTPX:
    """Provider double for the REAL adapters: records calls, serves scripted/permissive responses.

    Response shape mirrors the real client (`status_code`, `headers`, `.text`, `.json()`); the
    permissive default mints a fresh provider id per call so a persisted row can never collide by
    accident. Named, test-only: the adapters themselves run unpatched.
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
                return _Response(201, {"x-restli-id": f"urn:li:share:{len(fake.calls)}"}, "")
            if url.endswith("/feed"):
                return _Response(200, {}, json.dumps({"id": "graph-1"}))
            if url.endswith("/media"):
                return _Response(200, {}, json.dumps({"id": f"creation-{len(fake.calls)}"}))
            return _Response(200, {}, json.dumps({"id": f"media-{len(fake.calls)}"}))

        module = types.ModuleType("httpx")
        module.post = post                              # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "httpx", module)
        return fake


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    agent._orig_collect = agent.collect_structured
    agent.collect_structured = lambda task, context, **kw: (dict(payload), None)
    return agent


def _post_social(payload_platform: str, content: str, *, image_url: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"platform": payload_platform, "content": content}
    if image_url:
        item["image_url"] = image_url
    return {"action": "post", "posts": [item]}


# ── LinkedIn: real dispatcher + real adapter -> producer -> cap 4/4 ─────────────────────────────

async def test_linkedin_cap_fires_against_rows_the_real_producer_wrote(async_db_session, monkeypatch):
    """3 linkedin rows -> one send through the REAL adapter -> 4 rows -> the 5th is refused 4/4."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        linkedin_access_token="li-token", linkedin_author_urn="urn:li:person:42"))
    fake = _FakeHTTPX().install(monkeypatch)
    db = async_db_session

    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "linkedin", "content": f"seed {i}", "tweet_id": f"li-{i}"} for i in range(3)
    ]})
    assert report["posts_written"] == 3

    snapshot = await channel_sends_snapshot(db, ["linkedin"], now=NOW)
    assert snapshot["linkedin"] == {"today": 3, "hour": 0}

    agent = _stub_collection(SocialMediaAgent(), _post_social("linkedin", "real li send"))
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]   # firewall allowed, adapter ran
    assert out["posted"][0]["tweet_id"] == "urn:li:share:1"          # the provider's OWN id
    assert fake.calls[0]["url"] == "https://api.linkedin.com/rest/posts"

    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["linkedin"], now=NOW)
    assert at_cap == {"linkedin": {"today": 4, "hour": 1}}

    agent2 = _stub_collection(SocialMediaAgent(), _post_social("linkedin", "one too many"))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "4/4" in excinfo.value.message and "linkedin" in excinfo.value.message
    assert len(fake.calls) == 1                                      # the refusal sent nothing


# ── Meta Facebook: day cap 3 ─────────────────────────────────────────────────────────────────────

async def test_facebook_cap_fires_against_rows_the_real_producer_wrote(async_db_session, monkeypatch):
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        meta_page_access_token="meta-token", meta_facebook_page_id="page-123",
        meta_instagram_user_id="ig-456"))
    fake = _FakeHTTPX().install(monkeypatch)
    db = async_db_session

    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "facebook", "content": f"fb seed {i}", "tweet_id": f"page-{i}"} for i in range(2)
    ]})
    assert report["posts_written"] == 2

    snapshot = await channel_sends_snapshot(db, ["facebook"], now=NOW)
    assert snapshot["facebook"] == {"today": 2, "hour": 0}

    agent = _stub_collection(SocialMediaAgent(), _post_social("facebook", "fb send under cap"))
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]
    assert out["posted"][0]["tweet_id"] == "graph-1"
    assert fake.calls[0]["url"] == "https://graph.facebook.com/v21.0/page-123/feed"

    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["facebook"], now=NOW)
    assert at_cap == {"facebook": {"today": 3, "hour": 1}}

    agent2 = _stub_collection(SocialMediaAgent(), _post_social("facebook", "one too many"))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "3/3" in excinfo.value.message and "facebook" in excinfo.value.message
    assert len(fake.calls) == 1


# ── Meta Instagram: day cap 5, media REQUIRED and threaded end to end ────────────────────────────

async def test_instagram_cap_fires_with_the_image_threaded_from_the_payload(async_db_session, monkeypatch):
    """4 ig rows -> the 5th send carries `image_url` from the payload through the agent into the
    adapter's two-step publish -> 5 rows -> the 6th refused 5/5. Without the payload's image the
    adapter refuses by name and NO row would be written — the image leg is load-bearing."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        meta_page_access_token="meta-token", meta_facebook_page_id="page-123",
        meta_instagram_user_id="ig-456"))
    fake = _FakeHTTPX().install(monkeypatch)
    db = async_db_session

    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "instagram", "content": f"ig seed {i}", "tweet_id": f"ig-{i}"} for i in range(4)
    ]})
    assert report["posts_written"] == 4

    snapshot = await channel_sends_snapshot(db, ["instagram"], now=NOW)
    assert snapshot["instagram"] == {"today": 4, "hour": 0}

    agent = _stub_collection(SocialMediaAgent(), _post_social(
        "instagram", "ig caption", image_url="https://cdn.example.com/pic.jpg"))
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]
    published_id = out["posted"][0]["tweet_id"]
    assert published_id.startswith("media-")                        # the media_publish id, not a container
    assert [c["url"].rsplit("/", 1)[-1] for c in fake.calls] == ["media", "media_publish"]
    assert fake.calls[0]["data"]["image_url"] == "https://cdn.example.com/pic.jpg"

    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["instagram"], now=NOW)
    assert at_cap == {"instagram": {"today": 5, "hour": 1}}

    agent2 = _stub_collection(SocialMediaAgent(), _post_social(
        "instagram", "one too many", image_url="https://cdn.example.com/pic2.jpg"))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "5/5" in excinfo.value.message and "instagram" in excinfo.value.message
    assert len(fake.calls) == 2


async def test_instagram_post_without_an_image_is_a_named_refusal_and_no_row(
    async_db_session, monkeypatch
):
    """The agent-level form of the media rule: no image -> E_VALIDATION refusal recorded, no row."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        meta_page_access_token="meta-token", meta_facebook_page_id="page-123",
        meta_instagram_user_id="ig-456"))
    fake = _FakeHTTPX().install(monkeypatch)
    db = async_db_session

    agent = _stub_collection(SocialMediaAgent(), _post_social("instagram", "no image"))
    out = agent.run({"title": "post"}, {"channel_sends": {"instagram": {"today": 0, "hour": 0}}})
    assert out["posted"] == []
    assert fake.calls == []
    assert any(r.get("error", {}).get("code") == "E_VALIDATION" for r in out["refusals"])

    report = await social_service.record_published_posts(db, out)
    assert report["posts_written"] == 0
    assert await channel_sends_snapshot(db, ["instagram"], now=NOW) == {
        "instagram": {"today": 0, "hour": 0}
    }


# ── tiktok/youtube: caps configured, count UNKNOWN, refusal by name (unrun state, pinned) ───────

async def test_tiktok_and_youtube_counts_stay_unknown_and_the_firewall_refuses_by_name(
    async_db_session, monkeypatch
):
    """No adapter can write a tiktok/youtube row yet -> the count is `None` (unknown, never 0), and
    an attempted send is refused BY NAME (cap cannot be verified) instead of proceeding on a
    fabricated zero."""
    db = async_db_session

    snapshot = await channel_sends_snapshot(db, ["tiktok", "youtube"], now=NOW)
    assert snapshot == {
        "tiktok": {"today": None, "hour": None},
        "youtube": {"today": None, "hour": None},
    }

    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    with pytest.raises(PolsiaError) as excinfo:
        request_side_effect(
            "social_media", ActionClass.EXTERNAL_REVERSIBLE, "tiktok",
            action="post", payload={"action": "post", "content": "x", "ref": "post:0"},
            sends_today=None, sends_this_hour=None,
        )
    assert excinfo.value.code == E_POLICY_DENIED
    assert "no local record" in excinfo.value.message and "tiktok" in excinfo.value.message
