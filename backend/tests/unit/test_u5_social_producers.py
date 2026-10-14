"""U5/P1 — the new channels' producer legs: real send -> real producer -> real cap fires.

Same discipline as `test_send_cap_producer_e2e.py` (RD14): every leg goes through the REAL functions —
`SocialMediaAgent` -> the REAL dispatch table + adapter (provider SDK faked, never the adapter) ->
`social_service.record_published_posts` -> `channel_service.channel_sends_snapshot` -> the next send
is DENIED by the live `RateCaps` ("3/3", "4/4", "5/5"). The document numbers (FB 3/IG 5 day caps,
LinkedIn 4, tiktok 10, youtube 3) are the ones RateCaps now ships.

RED legs: pre-U5 the agent could not dispatch any of these platforms at all (the adapter refused), so
no row below could exist and every "cap fires" assertion fails on the old bytes.

The tiktok/youtube pin MOVED WITH THE FEATURE (V8-D3): pre-V8 their caps were configured while no
adapter could write a row, so their count stayed UNKNOWN (`None`, never a fabricated 0) and an
attempted send was refused BY NAME. The V8 adapters landed and joined `channel_service.
_SOCIAL_CHANNELS`, so the count is now MEASURABLE and the drills below prove a real (sandbox-off)
tiktok/youtube send writes a row through the REAL producer and the cap fires at the documented
numbers (tiktok 10/day, youtube 3/day).
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
from app.agents.social_media.agent import SocialMediaAgent
from app.core.errors import E_RATE_LIMITED, PolsiaError
from app.services import social_service
from app.services.channel_service import channel_sends_snapshot

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)     # inside the day window, outside the hour window


class _FakeHTTPX:
    """Provider double for the REAL adapters: records calls, serves scripted/permissive responses.

    Response shape mirrors the real client (`status_code`, `headers`, `.text`, `.content`,
    `.json()`); the permissive default mints a fresh provider id per call so a persisted row can
    never collide by accident. V8/T4: `get` serves the YouTube media fetch (the adapter downloads
    `image_url` before `videos.insert`). Named, test-only: the adapters themselves run unpatched.
    """

    def __init__(self, *responses: tuple[int, dict[str, str], str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeHTTPX:
        fake = self

        class _Response:
            def __init__(self, status_code: int, headers: dict[str, str], text: str,
                         content: bytes = b"") -> None:
                self.status_code = status_code
                self.headers = headers
                self.text = text
                self.content = content

            def json(self) -> Any:
                return json.loads(self.text)

        def post(url: str, **kwargs: Any) -> _Response:
            fake.calls.append({"method": "POST", "url": url, **kwargs})
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

        def get(url: str, **kwargs: Any) -> _Response:
            fake.calls.append({"method": "GET", "url": url, **kwargs})
            return _Response(200, {"content-type": "video/mp4"}, "", b"fake-video-bytes")

        module = types.ModuleType("httpx")
        module.post = post                              # type: ignore[attr-defined]
        module.get = get                                # type: ignore[attr-defined]
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


# ── tiktok/youtube: the count is MEASURABLE and the caps fire (V8-D3 pin move) ──────────────────

async def test_tiktok_cap_fires_against_rows_the_real_producer_wrote(async_db_session, monkeypatch):
    """9 tiktok rows -> one real Content Posting send -> 10 rows -> the 11th is refused 10/10."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        tiktok_access_token="tt-token"))
    fake = _FakeHTTPX((200, {}, json.dumps(
        {"data": {"publish_id": "tt-pub-1"}, "error": {"code": "ok", "message": ""}})
    )).install(monkeypatch)
    db = async_db_session

    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "tiktok", "content": f"tt seed {i}", "tweet_id": f"tt-{i}"} for i in range(9)
    ]})
    assert report["posts_written"] == 9

    snapshot = await channel_sends_snapshot(db, ["tiktok"], now=NOW)
    assert snapshot["tiktok"] == {"today": 9, "hour": 0}

    agent = _stub_collection(SocialMediaAgent(), _post_social(
        "tiktok", "real tt send", image_url="https://cdn.example.com/clip.mp4"))
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]
    assert out["posted"][0]["tweet_id"] == "tt-pub-1"                # the provider's OWN id
    assert fake.calls[0]["url"] == "https://open.tiktokapis.com/v2/post/publish/video/init/"

    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["tiktok"], now=NOW)
    assert at_cap == {"tiktok": {"today": 10, "hour": 1}}

    agent2 = _stub_collection(SocialMediaAgent(), _post_social(
        "tiktok", "one too many", image_url="https://cdn.example.com/clip2.mp4"))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "10/10" in excinfo.value.message and "tiktok" in excinfo.value.message
    assert len(fake.calls) == 1                                      # the refusal sent nothing


async def test_youtube_cap_fires_against_rows_the_real_producer_wrote(async_db_session, monkeypatch):
    """2 youtube rows -> one real videos.insert -> 3 rows -> the 4th is refused 3/3."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(integrations, "settings", types.SimpleNamespace(
        youtube_access_token="yt-token"))
    fake = _FakeHTTPX((200, {}, json.dumps({"id": "yt-vid-1"}))).install(monkeypatch)
    db = async_db_session

    monkeypatch.setattr(social_service, "utcnow", lambda: TWO_HOURS_AGO)
    report = await social_service.record_published_posts(db, {"posted": [
        {"platform": "youtube", "content": f"yt seed {i}", "tweet_id": f"yt-{i}"} for i in range(2)
    ]})
    assert report["posts_written"] == 2

    snapshot = await channel_sends_snapshot(db, ["youtube"], now=NOW)
    assert snapshot["youtube"] == {"today": 2, "hour": 0}

    agent = _stub_collection(SocialMediaAgent(), _post_social(
        "youtube", "real yt send", image_url="https://cdn.example.com/clip.mp4"))
    out = agent.run({"title": "post"}, {"channel_sends": snapshot})
    assert [r["performed"] for r in out["side_effects"]] == [True]
    assert out["posted"][0]["tweet_id"] == "yt-vid-1"
    assert [c["method"] for c in fake.calls] == ["GET", "POST"]      # media fetched, then inserted
    assert fake.calls[1]["url"] == "https://www.googleapis.com/upload/youtube/v3/videos"

    monkeypatch.setattr(social_service, "utcnow", lambda: NOW)
    persist = await social_service.record_published_posts(db, out)
    assert persist["posts_written"] == 1

    at_cap = await channel_sends_snapshot(db, ["youtube"], now=NOW)
    assert at_cap == {"youtube": {"today": 3, "hour": 1}}

    agent2 = _stub_collection(SocialMediaAgent(), _post_social(
        "youtube", "one too many", image_url="https://cdn.example.com/clip2.mp4"))
    with pytest.raises(PolsiaError) as excinfo:
        agent2.run({"title": "post"}, {"channel_sends": at_cap})
    assert excinfo.value.code == E_RATE_LIMITED
    assert "3/3" in excinfo.value.message and "youtube" in excinfo.value.message
    assert len(fake.calls) == 2                                      # the refusal sent nothing
